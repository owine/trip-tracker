"""ICS token helpers: generate, hash, resolve round-trip."""

from __future__ import annotations

import re

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.ics.tokens import (
    generate_token,
    hash_token,
    resolve_token,
)
from trip_tracker.models.user import User


def test_generate_token_returns_plaintext_and_hash() -> None:
    plaintext, h = generate_token()
    assert re.fullmatch(r"[A-Za-z0-9_-]{40,50}", plaintext)
    assert re.fullmatch(r"[0-9a-f]{64}", h)


def test_generate_token_yields_distinct_values() -> None:
    p1, h1 = generate_token()
    p2, h2 = generate_token()
    assert p1 != p2
    assert h1 != h2


def test_hash_token_deterministic() -> None:
    plaintext, h = generate_token()
    assert hash_token(plaintext) == h
    assert hash_token(plaintext) == hash_token(plaintext)


def test_hash_token_distinguishes_inputs() -> None:
    assert hash_token("abc") != hash_token("abd")


@pytest.mark.asyncio
async def test_resolve_token_returns_user(db_session: AsyncSession) -> None:
    u = User(oidc_subject="t1", email="t1@x.com", display_name="T1")
    db_session.add(u)
    await db_session.flush()
    plaintext, h = generate_token()
    u.ics_token_hash = h
    await db_session.commit()

    found = await resolve_token(plaintext, db_session)
    assert found is not None
    assert found.id == u.id


@pytest.mark.asyncio
async def test_resolve_token_returns_none_for_unknown(
    db_session: AsyncSession,
) -> None:
    plaintext, _h = generate_token()
    assert await resolve_token(plaintext, db_session) is None


@pytest.mark.asyncio
async def test_resolve_token_returns_none_for_user_without_token(
    db_session: AsyncSession,
) -> None:
    u = User(oidc_subject="t2", email="t2@x.com", display_name="T2")
    db_session.add(u)
    await db_session.commit()
    plaintext, _h = generate_token()
    assert await resolve_token(plaintext, db_session) is None
