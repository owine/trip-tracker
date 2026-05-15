"""Tests for purge_merged_trips: the 7-day soft-merge sweeper."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_merge_dismissal import TripMergeDismissal
from trip_tracker.models.user import User


async def _user(db: AsyncSession, suffix: str) -> User:
    u = User(
        oidc_subject=f"sweep-{suffix}",
        email=f"sweep-{suffix}@example.com",
        display_name=f"S{suffix}",
    )
    db.add(u)
    await db.commit()
    return u


async def _trip(
    db: AsyncSession,
    user: User,
    *,
    merged_into_id: uuid.UUID | None,
    merged_at: datetime | None,
    title: str = "Trip",
) -> Trip:
    t = Trip(
        title=title,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 2),
        created_by=user.id,
        merged_into_id=merged_into_id,
        merged_at=merged_at,
    )
    db.add(t)
    await db.commit()
    return t


@pytest.mark.asyncio
async def test_sweeper_deletes_past_window(db_url: str, db_session: AsyncSession) -> None:
    user = await _user(db_session, "expired")
    target = await _trip(db_session, user, merged_into_id=None, merged_at=None, title="Target")
    expired = await _trip(
        db_session,
        user,
        merged_into_id=target.id,
        merged_at=datetime.now(UTC) - timedelta(days=8),
        title="Expired",
    )

    from trip_tracker.worker import purge_merged_trips

    engine = create_async_engine(db_url)
    try:
        await purge_merged_trips({"engine": engine})
    finally:
        await engine.dispose()

    res = (await db_session.execute(select(Trip).where(Trip.id == expired.id))).scalar_one_or_none()
    assert res is None
    # Target trip is untouched.
    survivor = (
        await db_session.execute(select(Trip).where(Trip.id == target.id))
    ).scalar_one_or_none()
    assert survivor is not None


@pytest.mark.asyncio
async def test_sweeper_preserves_within_window(db_url: str, db_session: AsyncSession) -> None:
    user = await _user(db_session, "fresh")
    target = await _trip(db_session, user, merged_into_id=None, merged_at=None, title="Target")
    fresh = await _trip(
        db_session,
        user,
        merged_into_id=target.id,
        merged_at=datetime.now(UTC) - timedelta(days=3),
        title="Fresh",
    )

    from trip_tracker.worker import purge_merged_trips

    engine = create_async_engine(db_url)
    try:
        await purge_merged_trips({"engine": engine})
    finally:
        await engine.dispose()

    res = (await db_session.execute(select(Trip).where(Trip.id == fresh.id))).scalar_one_or_none()
    assert res is not None


@pytest.mark.asyncio
async def test_sweeper_cascades_dismissals(db_url: str, db_session: AsyncSession) -> None:
    """Hard-delete cascades trip_merge_dismissals via FK on both trip_a_id and trip_b_id."""
    user = await _user(db_session, "cascade")
    target = await _trip(db_session, user, merged_into_id=None, merged_at=None, title="Target")
    other = await _trip(db_session, user, merged_into_id=None, merged_at=None, title="Other")
    expired = await _trip(
        db_session,
        user,
        merged_into_id=target.id,
        merged_at=datetime.now(UTC) - timedelta(days=10),
        title="Expired",
    )

    # Dismissal where the expired trip appears in either position must cascade.
    # Canonical ordering (LEAST/GREATEST) is enforced by the DB unique index,
    # but the per-row FKs cascade on each column independently.
    a_id, b_id = sorted([expired.id, other.id], key=str)
    db_session.add(
        TripMergeDismissal(user_id=user.id, trip_a_id=a_id, trip_b_id=b_id),
    )
    await db_session.commit()

    from trip_tracker.worker import purge_merged_trips

    engine = create_async_engine(db_url)
    try:
        await purge_merged_trips({"engine": engine})
    finally:
        await engine.dispose()

    remaining = (
        await db_session.execute(
            select(TripMergeDismissal).where(
                (TripMergeDismissal.trip_a_id == expired.id)
                | (TripMergeDismissal.trip_b_id == expired.id)
            )
        )
    ).all()
    assert remaining == []
