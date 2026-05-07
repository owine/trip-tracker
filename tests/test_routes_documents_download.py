"""GET /documents/{id}/download — auth + X-Accel + fallback."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import Response as _Response

from tests.test_routes_documents_link import _seed_with_segment_and_doc  # seed helper
from trip_tracker.app import create_app
from trip_tracker.auth.session import set_session_cookie
from trip_tracker.config import Settings

PDF_BODY = b"%PDF-1.4\nfake\n"


def _cookie(user, settings):
    r = _Response()
    set_session_cookie(r, user_id=user.id, settings=settings)
    return {"tt_session": r.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


@asynccontextmanager
async def _ctx(app, settings, user):
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies=_cookie(user, settings),
        ) as client,
    ):
        yield client


@pytest.fixture
def authenticated_client_factory(db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)

    def _make(user):
        # Rebuild Settings + app per call so monkeypatch.setenv inside the
        # test body is observed.
        settings = Settings()
        app = create_app(settings=settings)
        return _ctx(app, settings, user)

    return _make


@pytest.mark.asyncio
async def test_download_streams_in_dev_mode(
    db_session, authenticated_client_factory, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DOCUMENTS_DIR", str(tmp_path))
    monkeypatch.delenv("DOCUMENTS_X_ACCEL_PREFIX", raising=False)
    u, _t, _s, d = await _seed_with_segment_and_doc(db_session)
    # Place a real file matching the doc's storage_key.
    full = tmp_path / d.storage_key
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(PDF_BODY)

    async with authenticated_client_factory(u) as client:
        r = await client.get(f"/documents/{d.id}/download")
    assert r.status_code == 200
    assert r.content == PDF_BODY
    assert "attachment" in r.headers["content-disposition"]
    assert r.headers["content-type"] == "application/pdf"


@pytest.mark.asyncio
async def test_download_x_accel_emits_redirect_header(
    db_session, authenticated_client_factory, monkeypatch
) -> None:
    monkeypatch.setenv("DOCUMENTS_X_ACCEL_PREFIX", "/internal-documents")
    u, _t, _s, d = await _seed_with_segment_and_doc(db_session)
    async with authenticated_client_factory(u) as client:
        r = await client.get(f"/documents/{d.id}/download")
    assert r.status_code == 204
    assert r.headers["x-accel-redirect"] == f"/internal-documents/{d.storage_key}"
    assert "attachment" in r.headers["content-disposition"]
    assert r.content == b""


@pytest.mark.asyncio
async def test_download_401_anonymous(db_session, db_url, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    _u, _t, _s, d = await _seed_with_segment_and_doc(db_session)
    settings = Settings()
    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        r = await client.get(f"/documents/{d.id}/download")
    assert r.status_code == 401
