"""Settings page — generate / regenerate ICS token, one-time URL flash."""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import Response as _Response
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import OWNER_USER_ID, set_session_cookie
from trip_tracker.config import Settings
from trip_tracker.models.user import User


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
        # Rebuild Settings + app per call so tests can monkeypatch env BEFORE
        # invoking the factory and have the changes take effect.
        settings = Settings()
        app = create_app(settings=settings)
        return _ctx(app, settings, user)

    return _make


@pytest.mark.asyncio
async def test_get_settings_no_token_shows_generate_button(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    u = User(id=OWNER_USER_ID, email="s1@x.com", display_name="S1")
    db_session.add(u)
    await db_session.commit()
    async with authenticated_client_factory(u) as client:
        r = await client.get("/settings")
    assert r.status_code == 200
    assert "Generate calendar feed URL" in r.text
    assert "Regenerate" not in r.text


@pytest.mark.asyncio
async def test_get_settings_with_token_shows_regenerate(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    u = User(
        id=OWNER_USER_ID,
        email="s2@x.com",
        display_name="S2",
        ics_token_hash="a" * 64,
    )
    db_session.add(u)
    await db_session.commit()
    async with authenticated_client_factory(u) as client:
        r = await client.get("/settings")
    assert r.status_code == 200
    assert "Regenerate" in r.text
    # Hash suffix shown
    assert "aaaaa" in r.text or "●●" in r.text


@pytest.mark.asyncio
async def test_post_regenerate_flashes_plaintext_url_once(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    u = User(id=OWNER_USER_ID, email="s3@x.com", display_name="S3")
    db_session.add(u)
    await db_session.commit()
    async with authenticated_client_factory(u) as client:
        r = await client.post("/settings/ics/regenerate", follow_redirects=False)
        assert r.status_code == 303
        # Follow the redirect — flash is consumed on the GET that follows
        r2 = await client.get("/settings")
    assert r2.status_code == 200
    # Flash includes the URL with the plaintext token
    assert "/ics/" in r2.text
    assert ".ics" in r2.text


@pytest.mark.asyncio
async def test_regenerate_overwrites_existing_token(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    """Old hash is replaced; a stale URL would 404."""
    u = User(
        id=OWNER_USER_ID,
        email="s4@x.com",
        display_name="S4",
        ics_token_hash="b" * 64,
    )
    db_session.add(u)
    await db_session.commit()
    old_hash = u.ics_token_hash
    async with authenticated_client_factory(u) as client:
        await client.post("/settings/ics/regenerate")
    await db_session.refresh(u)
    assert u.ics_token_hash is not None
    assert u.ics_token_hash != old_hash


@pytest.mark.asyncio
async def test_settings_requires_session(db_url, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
    ):
        r = await c.get("/settings", follow_redirects=False)
    assert r.status_code in (401, 302, 303)


@pytest.mark.asyncio
async def test_regenerate_requires_session(db_url, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
    ):
        r = await c.post("/settings/ics/regenerate", follow_redirects=False)
    assert r.status_code in (401, 302, 303)
