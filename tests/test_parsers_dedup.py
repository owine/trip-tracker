"""Dedup gate: find existing Segment matching a SegmentDraft (strong match)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User
from trip_tracker.parsers.base import SegmentDraft
from trip_tracker.parsers.dedup import find_existing_segment


async def _make_user(db: AsyncSession, oidc: str = "u1") -> User:
    user = User(oidc_subject=oidc, email=f"{oidc}@x.com", display_name=oidc)
    db.add(user)
    await db.flush()
    return user


async def _make_trip(db: AsyncSession, user: User) -> Trip:
    trip = Trip(
        title="Test Trip",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 7),
        created_by=user.id,
    )
    db.add(trip)
    await db.flush()
    return trip


async def _seed_segment(
    db: AsyncSession,
    user: User,
    trip: Trip,
    *,
    type_: str = "flight",
    provider: str | None = "Air France",
    confirmation: str | None = "XM8SK3",
    start_at: datetime | None = None,
    start_iata: str | None = "JFK",
    end_iata: str | None = "CDG",
    status: str = "confirmed",
) -> Segment:
    seg = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type=type_,
        status=status,
        confirmation_number=confirmation,
        provider=provider,
        start_at=start_at or datetime(2026, 6, 4, 16, 55, tzinfo=UTC),
        start_tz="UTC",
        start_location={"iata": start_iata} if start_iata else None,
        end_location={"iata": end_iata} if end_iata else None,
        details={},
        parse_source="test",
        parse_confidence=0.9,
    )
    db.add(seg)
    await db.flush()
    return seg


def _draft(
    *,
    type_: str = "flight",
    provider: str | None = "Air France",
    confirmation: str | None = "XM8SK3",
    start_at: datetime | None = None,
    start_iata: str | None = "LAX",
    end_iata: str | None = "ORD",
) -> SegmentDraft:
    return SegmentDraft(
        type=type_,  # type: ignore[arg-type]
        confirmation_number=confirmation,
        provider=provider,
        start_at=start_at or datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        start_tz="UTC",
        start_location={"iata": start_iata} if start_iata else None,
        end_location={"iata": end_iata} if end_iata else None,
    )


@pytest.mark.asyncio
async def test_strong_match_same_conf_same_provider(db_session: AsyncSession) -> None:
    user = await _make_user(db_session)
    trip = await _make_trip(db_session, user)
    seeded = await _seed_segment(db_session, user, trip)

    hit = await find_existing_segment(db_session, user.id, _draft())
    assert hit is not None
    assert hit.id == seeded.id


@pytest.mark.asyncio
async def test_strong_match_case_insensitive_provider(db_session: AsyncSession) -> None:
    user = await _make_user(db_session)
    trip = await _make_trip(db_session, user)
    seeded = await _seed_segment(db_session, user, trip, provider="AIR FRANCE")

    hit = await find_existing_segment(db_session, user.id, _draft(provider="  air france  "))
    assert hit is not None
    assert hit.id == seeded.id


@pytest.mark.asyncio
async def test_strong_match_different_provider_returns_none(
    db_session: AsyncSession,
) -> None:
    user = await _make_user(db_session)
    trip = await _make_trip(db_session, user)
    await _seed_segment(db_session, user, trip, provider="Air France")

    hit = await find_existing_segment(db_session, user.id, _draft(provider="Delta"))
    assert hit is None


@pytest.mark.asyncio
async def test_strong_match_null_conf_returns_none(db_session: AsyncSession) -> None:
    """Both sides must be non-null for a strong match."""
    user = await _make_user(db_session)
    trip = await _make_trip(db_session, user)

    # Case 1: seed has confirmation, draft has None → no strong match.
    await _seed_segment(db_session, user, trip, confirmation="XM8SK3")
    hit = await find_existing_segment(db_session, user.id, _draft(confirmation=None))
    assert hit is None

    # Case 2: seed has None, draft has a unique value → no strong match against
    # the null-conf seed. (We use a unique conf# so the previous seed can't match.)
    user2 = await _make_user(db_session, oidc="u2-nullconf")
    trip2 = await _make_trip(db_session, user2)
    await _seed_segment(db_session, user2, trip2, confirmation=None)
    hit2 = await find_existing_segment(
        db_session, user2.id, _draft(confirmation="UNIQUE-NEVER-SEEDED")
    )
    assert hit2 is None


@pytest.mark.asyncio
async def test_owner_scope_excludes_other_users(db_session: AsyncSession) -> None:
    user_a = await _make_user(db_session, oidc="user-a")
    user_b = await _make_user(db_session, oidc="user-b")
    trip_b = await _make_trip(db_session, user_b)
    await _seed_segment(db_session, user_b, trip_b)

    hit = await find_existing_segment(db_session, user_a.id, _draft())
    assert hit is None


@pytest.mark.asyncio
async def test_cancelled_segments_excluded(db_session: AsyncSession) -> None:
    user = await _make_user(db_session)
    trip = await _make_trip(db_session, user)
    await _seed_segment(db_session, user, trip, status="cancelled")

    hit = await find_existing_segment(db_session, user.id, _draft())
    assert hit is None
