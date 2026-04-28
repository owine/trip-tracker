"""WebhookReplay: composite PK enforcement."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.webhook_replay import WebhookReplay


@pytest.mark.asyncio
async def test_create(db_session: AsyncSession) -> None:
    db_session.add(
        WebhookReplay(
            ts_seconds=1_777_300_000,
            nonce="abc",
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_composite_pk_conflict(db_session: AsyncSession) -> None:
    expires = datetime.now(UTC) + timedelta(hours=24)
    db_session.add(WebhookReplay(ts_seconds=1, nonce="n", expires_at=expires))
    await db_session.commit()
    db_session.add(WebhookReplay(ts_seconds=1, nonce="n", expires_at=expires))
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_same_ts_different_nonce_ok(db_session: AsyncSession) -> None:
    expires = datetime.now(UTC) + timedelta(hours=24)
    db_session.add(WebhookReplay(ts_seconds=2, nonce="a", expires_at=expires))
    db_session.add(WebhookReplay(ts_seconds=2, nonce="b", expires_at=expires))
    await db_session.commit()
