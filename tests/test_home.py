"""GET / shows landing for anonymous users; greets logged-in users."""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.models.user import User


@pytest.mark.asyncio
async def test_home_anonymous_shows_login(
    db_url: str, monkeypatch: pytest.MonkeyPatch,
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
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession,
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()

    user = User(
        id=uuid.uuid4(), oidc_subject="s", email="o@example.com",
        display_name="Oliver", is_admin=False,
    )
    db_session.add(user)
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    cookie = encode_session(
        SessionPayload(user_id=user.id, oidc_subject="s"),
        secret=settings.session_secret.get_secret_value(),
        max_age=3600,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={"tt_session": cookie},
        ) as client,
    ):
        r = await client.get("/")
    assert r.status_code == 200
    assert "Hello, Oliver" in r.text
