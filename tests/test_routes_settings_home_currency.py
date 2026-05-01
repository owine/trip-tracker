"""Settings page — home_currency dropdown + POST handler."""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.models.user import User


def _cookie(user, settings):
    return {
        "tt_session": encode_session(
            SessionPayload(user_id=user.id, oidc_subject=user.oidc_subject),
            secret=settings.session_secret.get_secret_value(),
            max_age=3600,
        )
    }


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
    u = User(oidc_subject="hc1", email="hc1@x.com", display_name="HC1")
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
    u = User(oidc_subject="hc3", email="hc3@x.com", display_name="HC3")
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
    u = User(oidc_subject="hc2", email="hc2@x.com", display_name="HC2", home_currency="USD")
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
    u = User(oidc_subject="hc3", email="hc3@x.com", display_name="HC3", home_currency="JPY")
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
