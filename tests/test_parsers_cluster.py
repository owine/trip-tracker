"""Trip clustering rule: geo + ±1d adjacency + 20% gap → /inbox."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User
from trip_tracker.parsers.base import SegmentDraft
from trip_tracker.parsers.cluster import (
    cluster_for_user,
    derive_destination,
)


@pytest.fixture
async def user(db_session: AsyncSession) -> User:
    u = User(oidc_subject="cluster-test", email="c@x.com", display_name="C")
    db_session.add(u)
    await db_session.commit()
    return u


def _flight_draft(start: datetime, end: datetime, origin_iata: str, dest_iata: str) -> SegmentDraft:
    return SegmentDraft(
        type="flight",
        start_at=start,
        start_tz="UTC",
        end_at=end,
        end_tz="UTC",
        start_location={"iata": origin_iata, "city": "Origin"},
        end_location={"iata": dest_iata, "city": "Dest"},
    )


@pytest.mark.asyncio
async def test_no_existing_trips_creates_new(db_session: AsyncSession, user: User) -> None:
    draft = _flight_draft(
        datetime(2026, 6, 1, 9, tzinfo=UTC),
        datetime(2026, 6, 1, 22, tzinfo=UTC),
        "JFK",
        "CDG",
    )
    decision = await cluster_for_user(db_session, user.id, draft)
    assert decision.kind == "create_new"
    assert decision.auto_title == "Paris June 2026"


@pytest.mark.asyncio
async def test_single_overlapping_trip_attaches(db_session: AsyncSession, user: User) -> None:
    trip = Trip(
        title="Paris trip",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        primary_destination="Paris",
        created_by=user.id,
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    await db_session.commit()

    draft = _flight_draft(
        datetime(2026, 6, 3, 9, tzinfo=UTC),
        datetime(2026, 6, 3, 22, tzinfo=UTC),
        "CDG",
        "JFK",
    )
    decision = await cluster_for_user(db_session, user.id, draft)
    assert decision.kind == "attach"
    assert decision.trip_id == trip.id


@pytest.mark.asyncio
async def test_adjacent_plus_one_day_attaches(db_session: AsyncSession, user: User) -> None:
    trip = Trip(
        title="Paris",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        primary_destination="Paris",
        created_by=user.id,
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    await db_session.commit()

    # +1 day after trip end
    draft = _flight_draft(
        datetime(2026, 6, 6, 9, tzinfo=UTC),
        datetime(2026, 6, 6, 22, tzinfo=UTC),
        "CDG",
        "JFK",
    )
    decision = await cluster_for_user(db_session, user.id, draft)
    assert decision.kind == "attach"
    assert decision.trip_id == trip.id


@pytest.mark.asyncio
async def test_two_day_gap_does_not_cluster(db_session: AsyncSession, user: User) -> None:
    trip = Trip(
        title="Paris",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        primary_destination="Paris",
        created_by=user.id,
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    await db_session.commit()

    # +2 days after trip end → outside ±1d adjacency
    draft = _flight_draft(
        datetime(2026, 6, 7, 9, tzinfo=UTC),
        datetime(2026, 6, 7, 22, tzinfo=UTC),
        "JFK",
        "CDG",
    )
    decision = await cluster_for_user(db_session, user.id, draft)
    assert decision.kind == "create_new"


@pytest.mark.asyncio
async def test_close_score_routes_to_inbox(db_session: AsyncSession, user: User) -> None:
    """Two trips overlap with the same dates → ambiguous → /inbox."""
    for label in ("Paris", "Paris2"):
        t = Trip(
            title=label,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 5),
            primary_destination="Paris",
            created_by=user.id,
        )
        db_session.add(t)
        await db_session.flush()
        db_session.add(TripTraveler(trip_id=t.id, user_id=user.id, role="owner"))
    await db_session.commit()

    draft = _flight_draft(
        datetime(2026, 6, 3, 9, tzinfo=UTC),
        datetime(2026, 6, 3, 22, tzinfo=UTC),
        "JFK",
        "CDG",
    )
    decision = await cluster_for_user(db_session, user.id, draft)
    assert decision.kind == "ambiguous"


def test_derive_destination_flight_uses_end_city() -> None:
    draft = _flight_draft(
        datetime(2026, 6, 1, tzinfo=UTC),
        datetime(2026, 6, 1, tzinfo=UTC),
        "JFK",
        "CDG",
    )
    assert derive_destination(draft) == "Dest"


def test_derive_destination_lodging_uses_start_city() -> None:
    draft = SegmentDraft(
        type="lodging",
        start_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_tz="UTC",
        start_location={"city": "Paris"},
    )
    assert derive_destination(draft) == "Paris"
