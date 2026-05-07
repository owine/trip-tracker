"""Settings page — home_currency dropdown + POST handler."""

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
        settings = Settings()
        app = create_app(settings=settings)
        return _ctx(app, settings, user)

    return _make


@pytest.mark.asyncio
async def test_post_home_currency_persists(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    """POST /settings/home_currency updates user.home_currency and persists."""
    u = User(id=OWNER_USER_ID, email="hc1@x.com", display_name="HC1")
    db_session.add(u)
    await db_session.commit()
    # Verify default is USD
    assert u.home_currency == "USD"

    async with authenticated_client_factory(u) as client:
        r = await client.post(
            "/settings/home_currency", data={"home_currency": "EUR"}, follow_redirects=False
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/settings"

    # Re-fetch user from DB and verify the change persisted
    await db_session.refresh(u)
    assert u.home_currency == "EUR"


@pytest.mark.asyncio
async def test_post_home_currency_lowercase_normalized_and_accepted(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    """POST with lowercase code is uppercased and accepted (not rejected as invalid)."""
    u = User(id=OWNER_USER_ID, email="hc3@x.com", display_name="HC3")
    db_session.add(u)
    await db_session.commit()

    async with authenticated_client_factory(u) as client:
        r = await client.post(
            "/settings/home_currency", data={"home_currency": "eur"}, follow_redirects=False
        )
    assert r.status_code == 303
    await db_session.refresh(u)
    assert u.home_currency == "EUR"


@pytest.mark.asyncio
async def test_post_home_currency_invalid_code_rejected(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    """POST with invalid code (not 3 chars or contains digits) is rejected."""
    u = User(id=OWNER_USER_ID, email="hc2@x.com", display_name="HC2", home_currency="USD")
    db_session.add(u)
    await db_session.commit()

    async with authenticated_client_factory(u) as client:
        # Test 4-char code
        r = await client.post(
            "/settings/home_currency", data={"home_currency": "ZZZZ"}, follow_redirects=False
        )
    assert r.status_code == 303

    # User's home_currency should still be USD
    await db_session.refresh(u)
    assert u.home_currency == "USD"

    async with authenticated_client_factory(u) as client:
        # Test code with digit
        r = await client.post(
            "/settings/home_currency", data={"home_currency": "12X"}, follow_redirects=False
        )
    assert r.status_code == 303

    # User's home_currency should still be USD
    await db_session.refresh(u)
    assert u.home_currency == "USD"


@pytest.mark.asyncio
async def test_settings_page_renders_current_home_currency(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    """GET /settings renders the user's current home_currency as selected in the dropdown."""
    u = User(id=OWNER_USER_ID, email="hc4@x.com", display_name="HC4", home_currency="JPY")
    db_session.add(u)
    await db_session.commit()

    async with authenticated_client_factory(u) as client:
        r = await client.get("/settings")
    assert r.status_code == 200
    # Normalize whitespace and check for the selected option
    text_normalized = " ".join(r.text.split())
    assert 'value="JPY"' in text_normalized
    # Check that JPY option has the selected attribute
    assert 'value="JPY" selected' in text_normalized or 'JPY" selected' in text_normalized
