"""Tests for auth/deps.py — current_user, require_user (single-user cookie auth)."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.deps import current_user, require_user
from trip_tracker.auth.session import OWNER_USER_ID, set_session_cookie
from trip_tracker.config import Settings
from trip_tracker.models.user import User

# ---------------------------------------------------------------------------
# Local fixtures — NOT touching conftest.py
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(_set_required_env: None, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Return a Settings instance with the new single-owner fields injected."""
    monkeypatch.setenv("OWNER_EMAIL", "owner@example.com")
    monkeypatch.setenv("OWNER_SESSION_TOKEN", "y" * 32)
    return Settings()


@pytest_asyncio.fixture
async def owner_user(db_session: AsyncSession) -> User:
    """Seed the DB with the canonical owner user and return it."""
    user = User(
        id=OWNER_USER_ID,
        email="owner@example.com",
        display_name="Owner",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
def signed_session_cookie(settings: Settings) -> str:
    """Return the cookie *value* (string, not Set-Cookie header) for OWNER_USER_ID."""
    response = Response()
    set_session_cookie(response, user_id=OWNER_USER_ID, settings=settings)
    # Extract the value portion: "tt_session=<value>; ..."
    set_cookie_header = response.headers["set-cookie"]
    # format is "tt_session=<value>; Path=/; ..."
    cookie_kv = set_cookie_header.split(";")[0]  # "tt_session=<value>"
    return cookie_kv.split("=", 1)[1]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_current_user_returns_owner_with_valid_cookie(
    db_session: AsyncSession,
    owner_user: User,
    signed_session_cookie: str,
    settings: Settings,
) -> None:
    user = await current_user(tt_session=signed_session_cookie, db=db_session, settings=settings)
    assert user is not None
    assert user.id == owner_user.id
    assert user.email == owner_user.email


@pytest.mark.asyncio
async def test_current_user_returns_none_with_no_cookie(
    db_session: AsyncSession, settings: Settings
) -> None:
    user = await current_user(tt_session=None, db=db_session, settings=settings)
    assert user is None


@pytest.mark.asyncio
async def test_current_user_returns_none_with_tampered_cookie(
    db_session: AsyncSession, settings: Settings
) -> None:
    user = await current_user(tt_session="invalid.payload", db=db_session, settings=settings)
    assert user is None


@pytest.mark.asyncio
async def test_current_user_returns_none_with_uuid_for_unseeded_user(
    db_session: AsyncSession, settings: Settings
) -> None:
    """Cookie validates but the user_id doesn't exist in DB -> None."""
    response = Response()
    set_session_cookie(response, user_id=uuid.UUID(int=999), settings=settings)
    set_cookie_header = response.headers["set-cookie"]
    cookie_value = set_cookie_header.split(";")[0].split("=", 1)[1]

    user = await current_user(tt_session=cookie_value, db=db_session, settings=settings)
    assert user is None


@pytest.mark.asyncio
async def test_require_user_raises_401_without_cookie() -> None:
    with pytest.raises(HTTPException) as exc:
        await require_user(user=None)
    assert exc.value.status_code == 401
