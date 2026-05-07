"""GET / shows landing for anonymous users; greets logged-in users."""

from __future__ import annotations

import httpx
import pytest
from fastapi import Response as _Response
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import OWNER_USER_ID, set_session_cookie
from trip_tracker.config import Settings
from trip_tracker.models.user import User


def _cookie_value(user: User, settings: Settings) -> str:
    r = _Response()
    set_session_cookie(r, user_id=user.id, settings=settings)
    return r.headers["set-cookie"].split(";")[0].split("=", 1)[1]


@pytest.mark.asyncio
async def test_home_anonymous_shows_login(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        r = await client.get("/")
    assert r.status_code == 200
    assert "Sign in" in r.text


@pytest.mark.asyncio
async def test_home_logged_in_greets_user(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()

    user = User(
        id=OWNER_USER_ID,
        email="o@example.com",
        display_name="Oliver",
    )
    db_session.add(user)
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={"tt_session": _cookie_value(user, settings)},
        ) as client,
    ):
        r = await client.get("/")
    assert r.status_code == 200
    assert "Hello, Oliver" in r.text
