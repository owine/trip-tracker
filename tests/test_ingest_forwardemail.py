"""ForwardEmail webhook adapter — token auth + persistence + dedup."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.models.raw_email import RawEmail

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "forwardemail_payload.json").read_text()
)
_FE_TOKEN = "x" * 32  # Matches _set_required_env fixture


@asynccontextmanager
async def _client_ctx(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    """Create an AsyncClient for testing."""
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
    ):
        yield c


@pytest.fixture
def client_factory(db_url: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Factory to create an AsyncClient for the test app."""
    monkeypatch.setenv("DATABASE_URL", db_url)

    def _make() -> Any:
        app = create_app()
        return _client_ctx(app)

    return _make


@pytest.fixture
def fe_token() -> str:
    """The ForwardEmail relay token set by conftest."""
    return _FE_TOKEN


@pytest.fixture
def mock_enqueue_parse(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock enqueue_parse at the forwardemail adapter module's import site."""
    mock = AsyncMock()
    monkeypatch.setattr("trip_tracker.ingest.forwardemail.enqueue_parse", mock)
    return mock


@pytest.mark.asyncio
async def test_forwardemail_no_token_rejected(client_factory: Any) -> None:
    """POST without ?token= returns 401."""
    async with client_factory() as client:
        r = await client.post("/api/ingest/forwardemail", json=_FIXTURE)
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_forwardemail_wrong_token_rejected(client_factory: Any) -> None:
    """POST with ?token=wrong returns 401."""
    async with client_factory() as client:
        r = await client.post("/api/ingest/forwardemail?token=wrong", json=_FIXTURE)
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_forwardemail_missing_raw_rejected(client_factory: Any, fe_token: str) -> None:
    """POST with valid token but missing 'raw' field returns 400."""
    payload = {**_FIXTURE}
    del payload["raw"]
    async with client_factory() as client:
        r = await client.post(f"/api/ingest/forwardemail?token={fe_token}", json=payload)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_forwardemail_happy_path_persists_and_enqueues(
    client_factory: Any, db_session: AsyncSession, fe_token: str, mock_enqueue_parse: MagicMock
) -> None:
    """POST with valid token + fixture persists RawEmail and enqueues parse."""
    async with client_factory() as client:
        r = await client.post(f"/api/ingest/forwardemail?token={fe_token}", json=_FIXTURE)
    assert r.status_code == 202
    # Non-empty JSON body so FE's undici HTTP client can parse it without
    # raising UND_ERR_RESPONSE — empty 202s caused a cosmetic dashboard
    # error in production smoke even though delivery succeeded.
    assert r.json() == {"accepted": True}

    rows = (await db_session.execute(select(RawEmail))).scalars().all()
    assert len(rows) == 1
    assert rows[0].to_address == "me@trips.example.com"
    assert rows[0].message_id == "<fe-test-001@example.com>"

    # Tighten: not just "was called", but "called with the right args".
    # First positional arg is the Settings object; second is the inserted RawEmail UUID.
    mock_enqueue_parse.assert_called_once()
    args, _kwargs = mock_enqueue_parse.call_args
    assert args[1] == rows[0].id, "enqueue_parse must receive the inserted RawEmail.id"
