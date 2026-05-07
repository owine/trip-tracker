"""/api/search/<index> proxy: auth, filter injection, response shape."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import Response as _Response
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import OWNER_USER_ID, set_session_cookie
from trip_tracker.config import Settings
from trip_tracker.models.user import User


def _cookie(user: User, settings: Settings) -> dict[str, str]:
    r = _Response()
    set_session_cookie(r, user_id=user.id, settings=settings)
    return {"tt_session": r.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


@pytest.mark.asyncio
async def test_search_segments_filters_by_user(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Server injects traveler_ids = '<user.id>' regardless of client input."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = User(id=OWNER_USER_ID, email="s1@x.com", display_name="S1")
    db_session.add(user)
    await db_session.commit()

    fake_index = MagicMock()
    fake_index.search = AsyncMock(return_value={"hits": [], "estimatedTotalHits": 0})
    fake_meili = MagicMock()
    fake_meili.index = MagicMock(return_value=fake_index)

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(user, settings)
        ) as c,
    ):
        # Inject fake after lifespan starts (lifespan sets app.state.meili).
        app.state.meili = fake_meili
        r = await c.post(
            "/api/search/segments",
            json={"q": "Paris", "limit": 10},
        )
    assert r.status_code == 200
    body: dict[str, Any] = r.json()
    assert "hits" in body
    fake_index.search.assert_awaited_once()
    call_kwargs = fake_index.search.call_args.kwargs
    assert f"traveler_ids = '{user.id}'" in call_kwargs["filter"]
    assert call_kwargs["limit"] == 10


@pytest.mark.asyncio
async def test_search_requires_session(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """No session cookie → 401."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
    ):
        r = await c.post("/api/search/segments", json={"q": "Paris"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_search_invalid_index_returns_422(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Path param is constrained to {trips, segments}; other values rejected."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = User(id=OWNER_USER_ID, email="s2@x.com", display_name="S2")
    db_session.add(user)
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(user, settings)
        ) as c,
    ):
        r = await c.post("/api/search/widgets", json={"q": "x"})
    assert r.status_code == 422
