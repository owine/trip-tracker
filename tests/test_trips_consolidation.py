"""Tests for trip_tracker.trips.consolidation.ConsolidationTarget (spec §B4)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User
from trip_tracker.parsers.base import SegmentDraft
from trip_tracker.trips.consolidation import ConsolidationTarget

# ---------------------------------------------------------------------------
# Inline helpers
# ---------------------------------------------------------------------------


async def _seed_user_and_trip(
    db: AsyncSession,
    *,
    start_date: date,
    end_date: date,
) -> tuple[User, Trip]:
    """Create a User + one Trip; flush so both have PKs."""
    user = User(
        oidc_subject=f"sub-{uuid.uuid4()}",
        email=f"{uuid.uuid4()}@test.example",
        display_name="Tester",
    )
    db.add(user)
    await db.flush()

    trip = Trip(
        title="Test Trip",
        start_date=start_date,
        end_date=end_date,
        created_by=user.id,
    )
    db.add(trip)
    await db.flush()

    return user, trip


# ---------------------------------------------------------------------------
# Tests: from_drafts
# ---------------------------------------------------------------------------


def test_from_drafts_extracts_endpoints() -> None:
    """from_drafts normalizes a flight + lodging draft into the expected shape."""
    flight = SegmentDraft(
        type="flight",
        start_at=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
        start_tz="UTC",
        end_at=datetime(2026, 6, 1, 22, 0, tzinfo=UTC),
        end_tz="Europe/Paris",
        start_location={"city": "JFK", "iata": "JFK"},
        end_location={"city": "CDG", "iata": "CDG"},
    )
    lodging = SegmentDraft(
        type="lodging",
        start_at=datetime(2026, 6, 2, 14, 0, tzinfo=UTC),
        start_tz="Europe/Paris",
        end_at=datetime(2026, 6, 7, 11, 0, tzinfo=UTC),
        end_tz="Europe/Paris",
        start_location={"city": "Paris"},
        end_location={"city": "Paris"},
    )

    target = ConsolidationTarget.from_drafts([flight, lodging])

    # Earliest draft is the flight → start_city = JFK
    assert target.start_city == "JFK"
    # Latest draft is the lodging → end_city = Paris
    assert target.end_city == "Paris"
    # IATA codes extracted from both locations
    assert "JFK" in target.endpoint_iatas
    assert "CDG" in target.endpoint_iatas
    # No trip row yet
    assert target.trip_id is None
    # Dates derived from draft start_at / end_at
    assert target.start_date == date(2026, 6, 1)
    assert target.end_date == date(2026, 6, 7)


def test_from_drafts_empty_returns_today_dates() -> None:
    """Empty draft list falls back to today for both dates; cities and IATAs empty."""
    today = date.today()
    target = ConsolidationTarget.from_drafts([])

    assert target.start_date == today
    assert target.end_date == today
    assert target.start_city is None
    assert target.end_city is None
    assert target.endpoint_iatas == frozenset()
    assert target.trip_id is None


# ---------------------------------------------------------------------------
# Tests: from_trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_from_trip_extracts_endpoints(db_session: AsyncSession) -> None:
    """from_trip reads Trip + Segment rows and produces the expected shape."""
    trip_start = date(2026, 7, 10)
    trip_end = date(2026, 7, 20)
    user, trip = await _seed_user_and_trip(db_session, start_date=trip_start, end_date=trip_end)

    base_dt = datetime(2026, 7, 10, 9, 0, tzinfo=UTC)

    seg1 = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="flight",
        status="confirmed",
        start_at=base_dt,
        start_tz="UTC",
        end_at=base_dt + timedelta(hours=10),
        end_tz="America/New_York",
        start_location={"city": "LHR", "iata": "LHR"},
        end_location={"city": "JFK", "iata": "JFK"},
        details={},
        parse_source="test",
        parse_confidence=0.9,
    )
    seg2 = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="lodging",
        status="confirmed",
        start_at=base_dt + timedelta(days=1),
        start_tz="America/New_York",
        end_at=base_dt + timedelta(days=5),
        end_tz="America/New_York",
        start_location={"city": "New York"},
        end_location={"city": "New York"},
        details={},
        parse_source="test",
        parse_confidence=0.9,
    )
    db_session.add(seg1)
    db_session.add(seg2)
    await db_session.flush()

    target = ConsolidationTarget.from_trip(trip, [seg1, seg2])

    # Ordered by start_at: seg1 is first
    assert target.start_city == "LHR"
    # seg2 is last → end_city from seg2's end_location
    assert target.end_city == "New York"
    # IATA codes from both segments' locations
    assert "LHR" in target.endpoint_iatas
    assert "JFK" in target.endpoint_iatas
    # trip_id wired through
    assert target.trip_id == trip.id
    # Dates come from Trip row, not segments
    assert target.start_date == trip_start
    assert target.end_date == trip_end
