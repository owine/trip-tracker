"""HMAC + replay cache primitives."""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.ingest.hmac_verify import (
    PruneGate,
    prune_replay_cache,
    record_nonce,
    verify_signature,
)
from trip_tracker.models.webhook_replay import WebhookReplay

SECRET = b"x" * 32


def test_verify_signature_match() -> None:
    body = b"hello"
    sig = "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    assert verify_signature(body, sig, SECRET) is True


def test_verify_signature_mismatch() -> None:
    body = b"hello"
    sig = "sha256=" + ("0" * 64)
    assert verify_signature(body, sig, SECRET) is False


def test_verify_signature_missing_prefix() -> None:
    body = b"hello"
    bare = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    assert verify_signature(body, bare, SECRET) is False


def test_verify_signature_empty_header() -> None:
    assert verify_signature(b"hello", "", SECRET) is False


@pytest.mark.asyncio
async def test_record_nonce_first_succeeds(db_session: AsyncSession) -> None:
    ok = await record_nonce(db_session, ts_seconds=1, nonce="a")
    await db_session.commit()
    assert ok is True


@pytest.mark.asyncio
async def test_record_nonce_conflict_returns_false(db_session: AsyncSession) -> None:
    await record_nonce(db_session, ts_seconds=1, nonce="b")
    await db_session.commit()
    ok = await record_nonce(db_session, ts_seconds=1, nonce="b")
    await db_session.commit()
    assert ok is False


@pytest.mark.asyncio
async def test_prune_replay_cache_removes_expired(db_session: AsyncSession) -> None:
    past = datetime.now(UTC) - timedelta(seconds=1)
    db_session.add(WebhookReplay(ts_seconds=10, nonce="old", expires_at=past))
    db_session.add(
        WebhookReplay(
            ts_seconds=11,
            nonce="fresh",
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
    )
    await db_session.commit()

    deleted = await prune_replay_cache(db_session)
    await db_session.commit()
    assert deleted == 1

    rows = (await db_session.execute(select(WebhookReplay))).scalars().all()
    assert {r.nonce for r in rows} == {"fresh"}


def test_prune_gate_skip_within_window() -> None:
    gate = PruneGate(interval_seconds=60.0)
    t0 = time.monotonic()
    assert gate.should_prune(now=t0) is True
    assert gate.should_prune(now=t0 + 30) is False
    assert gate.should_prune(now=t0 + 61) is True
