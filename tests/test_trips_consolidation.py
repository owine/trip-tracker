"""Tests for trip_tracker.trips.consolidation (spec §B4 + §B5)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User
from trip_tracker.parsers.base import SegmentDraft
from trip_tracker.trips.consolidation import (
    ConsolidationTarget,
    consolidation_candidates,
)

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
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@test.example",
        display_name="Tester",
    )
    db.add(user)
    await db.flush()

    trip = Trip(
        title="Test Trip",
        start_date=start_date,
        end_date=end_date,
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


def test_from_drafts_end_date_uses_max_end_at() -> None:
    """end_date must be max(end_at) across drafts, not last-by-start_at.

    A return flight starting after a long lodging draft can have an earlier
    end_at than the lodging's check-out — picking ordered[-1] would shrink
    the consolidation window.
    """
    lodging = SegmentDraft(
        type="lodging",
        start_at=datetime(2026, 8, 10, 14, 0, tzinfo=UTC),
        start_tz="Europe/Paris",
        end_at=datetime(2026, 8, 20, 11, 0, tzinfo=UTC),
        end_tz="Europe/Paris",
        start_location={"city": "Paris"},
        end_location={"city": "Paris"},
    )
    return_flight = SegmentDraft(
        type="flight",
        start_at=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        start_tz="Europe/Paris",
        end_at=datetime(2026, 8, 17, 13, 0, tzinfo=UTC),
        end_tz="America/New_York",
        start_location={"city": "CDG", "iata": "CDG"},
        end_location={"city": "JFK", "iata": "JFK"},
    )

    target = ConsolidationTarget.from_drafts([lodging, return_flight])
    assert target.end_date == date(2026, 8, 20)


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


@pytest.mark.asyncio
async def test_from_trip_empty_segments(db_session: AsyncSession) -> None:
    """from_trip with an empty segment list still returns a valid target."""
    user, trip = await _seed_user_and_trip(
        db_session, start_date=date(2026, 9, 1), end_date=date(2026, 9, 5)
    )
    _ = user

    target = ConsolidationTarget.from_trip(trip, [])

    assert target.start_city is None
    assert target.end_city is None
    assert target.endpoint_iatas == frozenset()
    assert target.trip_id == trip.id
    assert target.start_date == date(2026, 9, 1)
    assert target.end_date == date(2026, 9, 5)


# ---------------------------------------------------------------------------
# Inline helpers for B5 tests
# ---------------------------------------------------------------------------


async def _make_user(db: AsyncSession) -> User:
    """Create and flush a User with a unique identity."""
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@test.example",
        display_name="Tester",
    )
    db.add(user)
    await db.flush()
    return user


async def _make_trip(
    db: AsyncSession,
    *,
    user: User,
    start_date: date,
    end_date: date,
    title: str = "Trip",
) -> Trip:
    """Create and flush a Trip."""
    trip = Trip(
        title=title,
        start_date=start_date,
        end_date=end_date,
    )
    db.add(trip)
    await db.flush()
    return trip


def _make_segment(
    *,
    trip_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    start_at: datetime,
    start_city: str | None = None,
    end_city: str | None = None,
    start_iata: str | None = None,
    end_iata: str | None = None,
    status: str = "confirmed",
) -> Segment:
    """Build a Segment row (not yet added to session)."""
    start_loc: dict = {}
    end_loc: dict = {}
    if start_city:
        start_loc["city"] = start_city
    if start_iata:
        start_loc["iata"] = start_iata
    if end_city:
        end_loc["city"] = end_city
    if end_iata:
        end_loc["iata"] = end_iata
    return Segment(
        trip_id=trip_id,
        owner_user_id=owner_user_id,
        type="flight",
        status=status,
        start_at=start_at,
        start_tz="UTC",
        end_at=start_at + timedelta(hours=3),
        end_tz="UTC",
        start_location=start_loc,
        end_location=end_loc,
        details={},
        parse_source="test",
        parse_confidence=0.9,
    )


async def _seed_home_history(
    db: AsyncSession,
    *,
    user: User,
    trip: Trip,
    home_city: str,
) -> None:
    """Seed ≥30% endpoint dominance for *home_city* so infer_home returns it.

    Strategy: 5 segments <unique>->HOME (HOME as end only, starts unique).
    In isolation that's 5/10 = 50% endpoint share. In test contexts that
    seed extra trip-specific segments, the real share is lower (~35-43%
    observed) but still well above the 30% floor.
    """
    now = datetime(2025, 1, 1, 0, 0, tzinfo=UTC)
    for i, prefix in enumerate(["A_", "B_", "C_", "D_", "E_"]):
        seg = _make_segment(
            trip_id=trip.id,
            owner_user_id=user.id,
            start_at=now + timedelta(days=i),
            start_city=f"{prefix}{uuid.uuid4().hex[:4]}",
            end_city=home_city,
        )
        db.add(seg)
    await db.flush()


# ---------------------------------------------------------------------------
# Tests: consolidation_candidates (spec §6.2 / B5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_home_anchored_HIGH_on_last_endpoint_match(db_session: AsyncSession) -> None:
    """target.start_city == trip_view.end_city → HIGH (next-leg continuation)."""
    user = await _make_user(db_session)
    home_trip = await _make_trip(
        db_session, user=user, start_date=date(2025, 1, 1), end_date=date(2025, 12, 31)
    )
    await _seed_home_history(db_session, user=user, trip=home_trip, home_city="NYC")

    # Existing trip: NYC → PARIS (open — end_city != home)
    existing = await _make_trip(
        db_session, user=user, start_date=date(2026, 6, 1), end_date=date(2026, 6, 10)
    )
    seg_e = _make_segment(
        trip_id=existing.id,
        owner_user_id=user.id,
        start_at=datetime(2026, 6, 1, 10, tzinfo=UTC),
        start_city="NYC",
        end_city="PARIS",
    )
    db_session.add(seg_e)
    await db_session.flush()

    # Target trip: PARIS → ROME (start_city=PARIS == existing.end_city=PARIS)
    target_trip = await _make_trip(
        db_session, user=user, start_date=date(2026, 6, 11), end_date=date(2026, 6, 15)
    )
    seg_t = _make_segment(
        trip_id=target_trip.id,
        owner_user_id=user.id,
        start_at=datetime(2026, 6, 11, 10, tzinfo=UTC),
        start_city="PARIS",
        end_city="ROME",
    )
    db_session.add(seg_t)
    await db_session.flush()

    target_segs = [seg_t]
    target = ConsolidationTarget.from_trip(target_trip, target_segs)

    results = await consolidation_candidates(db_session, user, target)

    assert len(results) == 1
    assert results[0].trip.id == existing.id
    assert results[0].weight.name == "HIGH"


@pytest.mark.asyncio
async def test_home_anchored_HIGH_closing_leg(db_session: AsyncSession) -> None:
    """target.end_city == home AND trip has outbound from home → HIGH (closing leg)."""
    user = await _make_user(db_session)
    home_trip = await _make_trip(
        db_session, user=user, start_date=date(2025, 1, 1), end_date=date(2025, 12, 31)
    )
    await _seed_home_history(db_session, user=user, trip=home_trip, home_city="NYC")

    # Existing trip: NYC → PARIS (start=NYC=home, end=PARIS≠home → open with outbound)
    existing = await _make_trip(
        db_session, user=user, start_date=date(2026, 7, 1), end_date=date(2026, 7, 10)
    )
    seg_e = _make_segment(
        trip_id=existing.id,
        owner_user_id=user.id,
        start_at=datetime(2026, 7, 1, 10, tzinfo=UTC),
        start_city="NYC",
        end_city="PARIS",
    )
    db_session.add(seg_e)
    await db_session.flush()

    # Target: PARIS → NYC (end_city=NYC=home)
    target_trip = await _make_trip(
        db_session, user=user, start_date=date(2026, 7, 11), end_date=date(2026, 7, 12)
    )
    seg_t = _make_segment(
        trip_id=target_trip.id,
        owner_user_id=user.id,
        start_at=datetime(2026, 7, 11, 10, tzinfo=UTC),
        start_city="PARIS",
        end_city="NYC",
    )
    db_session.add(seg_t)
    await db_session.flush()

    target = ConsolidationTarget.from_trip(target_trip, [seg_t])
    results = await consolidation_candidates(db_session, user, target)

    assert len(results) == 1
    assert results[0].trip.id == existing.id
    assert results[0].weight.name == "HIGH"


@pytest.mark.asyncio
async def test_geometric_MEDIUM_on_shared_endpoint_when_home_unset(
    db_session: AsyncSession,
) -> None:
    """No home inferred → geometric fallback; shared city → MEDIUM."""
    user = await _make_user(db_session)
    # No home history seeded → infer_home returns None.

    # Existing trip: BERLIN → VIENNA
    existing = await _make_trip(
        db_session, user=user, start_date=date(2026, 8, 1), end_date=date(2026, 8, 8)
    )
    seg_e = _make_segment(
        trip_id=existing.id,
        owner_user_id=user.id,
        start_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
        start_city="BERLIN",
        end_city="VIENNA",
    )
    db_session.add(seg_e)
    await db_session.flush()

    # Target: VIENNA → ROME (shares VIENNA with existing)
    target_trip = await _make_trip(
        db_session, user=user, start_date=date(2026, 8, 9), end_date=date(2026, 8, 14)
    )
    seg_t = _make_segment(
        trip_id=target_trip.id,
        owner_user_id=user.id,
        start_at=datetime(2026, 8, 9, 10, tzinfo=UTC),
        start_city="VIENNA",
        end_city="ROME",
    )
    db_session.add(seg_t)
    await db_session.flush()

    target = ConsolidationTarget.from_trip(target_trip, [seg_t])
    results = await consolidation_candidates(db_session, user, target)

    assert len(results) == 1
    assert results[0].trip.id == existing.id
    assert results[0].weight.name == "MEDIUM"


@pytest.mark.asyncio
async def test_geometric_LOW_on_under_500km_when_home_unset(db_session: AsyncSession) -> None:
    """No home; endpoints share no city but are within 500km via haversine → LOW.

    LHR (London) ↔ AMS (Amsterdam) ≈ 358 km — both resolve via get_airport.
    Endpoint cities are different strings so MEDIUM doesn't fire.
    """
    user = await _make_user(db_session)
    # No home history seeded.

    # Existing trip: JFK → LHR  (IATA codes so haversine resolves)
    existing = await _make_trip(
        db_session, user=user, start_date=date(2026, 9, 1), end_date=date(2026, 9, 8)
    )
    seg_e = _make_segment(
        trip_id=existing.id,
        owner_user_id=user.id,
        start_at=datetime(2026, 9, 1, 10, tzinfo=UTC),
        start_city="JFK",
        start_iata="JFK",
        end_city="LHR",
        end_iata="LHR",
    )
    db_session.add(seg_e)
    await db_session.flush()

    # Target: AMS → CDG  — AMS is ~358 km from LHR → LOW
    # (no shared city name: LHR ≠ AMS, JFK ≠ CDG)
    target_trip = await _make_trip(
        db_session, user=user, start_date=date(2026, 9, 9), end_date=date(2026, 9, 12)
    )
    seg_t = _make_segment(
        trip_id=target_trip.id,
        owner_user_id=user.id,
        start_at=datetime(2026, 9, 9, 10, tzinfo=UTC),
        start_city="AMS",
        start_iata="AMS",
        end_city="CDG",
        end_iata="CDG",
    )
    db_session.add(seg_t)
    await db_session.flush()

    target = ConsolidationTarget.from_trip(target_trip, [seg_t])
    results = await consolidation_candidates(db_session, user, target)

    assert len(results) == 1
    assert results[0].trip.id == existing.id
    assert results[0].weight.name == "LOW"


@pytest.mark.asyncio
async def test_gap_over_3_days_excludes_geometric(db_session: AsyncSession) -> None:
    """Window pre-filter (±3 days) excludes trips outside the date window."""
    user = await _make_user(db_session)

    # Existing trip ends 2026-06-01; target starts 2026-06-10 → 9-day gap > 3 days.
    existing = await _make_trip(
        db_session, user=user, start_date=date(2026, 5, 25), end_date=date(2026, 6, 1)
    )
    seg_e = _make_segment(
        trip_id=existing.id,
        owner_user_id=user.id,
        start_at=datetime(2026, 5, 25, 10, tzinfo=UTC),
        start_city="BERLIN",
        end_city="VIENNA",
    )
    db_session.add(seg_e)
    await db_session.flush()

    # Target: VIENNA → ROME — starts 2026-06-10 (9 days after existing ends)
    target_trip = await _make_trip(
        db_session, user=user, start_date=date(2026, 6, 10), end_date=date(2026, 6, 15)
    )
    seg_t = _make_segment(
        trip_id=target_trip.id,
        owner_user_id=user.id,
        start_at=datetime(2026, 6, 10, 10, tzinfo=UTC),
        start_city="VIENNA",
        end_city="ROME",
    )
    db_session.add(seg_t)
    await db_session.flush()

    target = ConsolidationTarget.from_trip(target_trip, [seg_t])
    results = await consolidation_candidates(db_session, user, target)

    # Existing trip is outside the ±3-day window → excluded.
    assert results == []


@pytest.mark.asyncio
async def test_multi_country_chain_via_home_anchored(db_session: AsyncSession) -> None:
    """home='NYC'; trip NYC→TOKYO (open); target TOKYO→SINGAPORE → HIGH continuation."""
    user = await _make_user(db_session)
    home_trip = await _make_trip(
        db_session, user=user, start_date=date(2025, 1, 1), end_date=date(2025, 12, 31)
    )
    await _seed_home_history(db_session, user=user, trip=home_trip, home_city="NYC")

    existing = await _make_trip(
        db_session, user=user, start_date=date(2026, 10, 1), end_date=date(2026, 10, 15)
    )
    seg_e = _make_segment(
        trip_id=existing.id,
        owner_user_id=user.id,
        start_at=datetime(2026, 10, 1, 10, tzinfo=UTC),
        start_city="NYC",
        end_city="TOKYO",
    )
    db_session.add(seg_e)
    await db_session.flush()

    # Target: TOKYO → SINGAPORE (continuation from TOKYO)
    target_trip = await _make_trip(
        db_session, user=user, start_date=date(2026, 10, 16), end_date=date(2026, 10, 18)
    )
    seg_t = _make_segment(
        trip_id=target_trip.id,
        owner_user_id=user.id,
        start_at=datetime(2026, 10, 16, 10, tzinfo=UTC),
        start_city="TOKYO",
        end_city="SINGAPORE",
    )
    db_session.add(seg_t)
    await db_session.flush()

    target = ConsolidationTarget.from_trip(target_trip, [seg_t])
    results = await consolidation_candidates(db_session, user, target)

    assert len(results) == 1
    assert results[0].trip.id == existing.id
    assert results[0].weight.name == "HIGH"


@pytest.mark.asyncio
async def test_top_3_cap(db_session: AsyncSession) -> None:
    """5 matching candidate trips → only top 3 returned."""
    user = await _make_user(db_session)

    base = date(2026, 11, 1)
    for i in range(5):
        trip_i = await _make_trip(
            db_session,
            user=user,
            start_date=base + timedelta(days=i),
            end_date=base + timedelta(days=i + 3),
            title=f"Trip {i}",
        )
        seg = _make_segment(
            trip_id=trip_i.id,
            owner_user_id=user.id,
            start_at=datetime(2026, 11, 1 + i, 10, tzinfo=UTC),
            start_city="BERLIN",
            end_city="VIENNA",
        )
        db_session.add(seg)
    await db_session.flush()

    # Target: VIENNA → ROME — shares VIENNA with all 5 → all MEDIUM
    target_trip = await _make_trip(
        db_session,
        user=user,
        start_date=date(2026, 11, 6),
        end_date=date(2026, 11, 8),
    )
    seg_t = _make_segment(
        trip_id=target_trip.id,
        owner_user_id=user.id,
        start_at=datetime(2026, 11, 6, 10, tzinfo=UTC),
        start_city="VIENNA",
        end_city="ROME",
    )
    db_session.add(seg_t)
    await db_session.flush()

    target = ConsolidationTarget.from_trip(target_trip, [seg_t])
    results = await consolidation_candidates(db_session, user, target)

    assert len(results) == 3


@pytest.mark.asyncio
async def test_consolidation_candidates_no_longer_filters_by_dismissal(
    db_session: AsyncSession,
) -> None:
    """Phase 11: dismissal table is gone; candidates surface regardless."""
    user = await _make_user(db_session)

    # Existing trip: BERLIN → VIENNA (within the ±3-day window of target)
    existing = await _make_trip(
        db_session, user=user, start_date=date(2026, 12, 1), end_date=date(2026, 12, 8)
    )
    seg_e = _make_segment(
        trip_id=existing.id,
        owner_user_id=user.id,
        start_at=datetime(2026, 12, 1, 10, tzinfo=UTC),
        start_city="BERLIN",
        end_city="VIENNA",
    )
    db_session.add(seg_e)
    await db_session.flush()

    # Target: VIENNA → ROME (start_city=VIENNA shared with existing.end_city → MEDIUM)
    target_trip = await _make_trip(
        db_session, user=user, start_date=date(2026, 12, 9), end_date=date(2026, 12, 14)
    )
    seg_t = _make_segment(
        trip_id=target_trip.id,
        owner_user_id=user.id,
        start_at=datetime(2026, 12, 9, 10, tzinfo=UTC),
        start_city="VIENNA",
        end_city="ROME",
    )
    db_session.add(seg_t)
    await db_session.flush()

    target = ConsolidationTarget.from_trip(target_trip, [seg_t])
    results = await consolidation_candidates(db_session, user, target)

    # No dismissal filtering — existing trip must appear as a candidate.
    assert any(c.trip.id == existing.id for c in results), (
        "existing trip should surface as a candidate; dismissal filter has been removed"
    )


@pytest.mark.asyncio
async def test_sort_order_high_then_medium_then_low(db_session: AsyncSession) -> None:
    """Sort: HIGH (newer) > HIGH (older) > MEDIUM > LOW."""
    user = await _make_user(db_session)
    home_trip = await _make_trip(
        db_session, user=user, start_date=date(2025, 1, 1), end_date=date(2025, 12, 31)
    )
    await _seed_home_history(db_session, user=user, trip=home_trip, home_city="NYC")

    # Target: PARIS → ROME, start 2026-03-10
    target_trip = await _make_trip(
        db_session, user=user, start_date=date(2026, 3, 10), end_date=date(2026, 3, 15)
    )
    seg_t = _make_segment(
        trip_id=target_trip.id,
        owner_user_id=user.id,
        start_at=datetime(2026, 3, 10, 10, tzinfo=UTC),
        start_city="PARIS",
        end_city="ROME",
    )
    db_session.add(seg_t)
    await db_session.flush()

    # HIGH trip 1 (newer): NYC→PARIS, ends 2026-03-09 — target.start_city=PARIS matches
    high1 = await _make_trip(
        db_session, user=user, start_date=date(2026, 3, 5), end_date=date(2026, 3, 9)
    )
    db_session.add(
        _make_segment(
            trip_id=high1.id,
            owner_user_id=user.id,
            start_at=datetime(2026, 3, 5, 10, tzinfo=UTC),
            start_city="NYC",
            end_city="PARIS",
        )
    )

    # HIGH trip 2 (older): NYC→PARIS, ends 2026-03-07
    high2 = await _make_trip(
        db_session, user=user, start_date=date(2026, 3, 1), end_date=date(2026, 3, 7)
    )
    db_session.add(
        _make_segment(
            trip_id=high2.id,
            owner_user_id=user.id,
            start_at=datetime(2026, 3, 1, 10, tzinfo=UTC),
            start_city="NYC",
            end_city="PARIS",
        )
    )

    # MEDIUM trip: ROME→BERLIN — shares ROME (target.end_city)
    medium = await _make_trip(
        db_session, user=user, start_date=date(2026, 3, 8), end_date=date(2026, 3, 9)
    )
    db_session.add(
        _make_segment(
            trip_id=medium.id,
            owner_user_id=user.id,
            start_at=datetime(2026, 3, 8, 10, tzinfo=UTC),
            start_city="ROME",
            end_city="BERLIN",
        )
    )

    # LOW trip: JFK→LHR vs target PARIS/ROME — no shared city.
    # LHR (51.5°N, -0.5°W) ↔ Paris city (~48.9°N, 2.3°E) ≈ 340 km → LOW fires.
    low = await _make_trip(
        db_session, user=user, start_date=date(2026, 3, 8), end_date=date(2026, 3, 9)
    )
    db_session.add(
        _make_segment(
            trip_id=low.id,
            owner_user_id=user.id,
            start_at=datetime(2026, 3, 8, 10, tzinfo=UTC),
            start_city="LHR",
            start_iata="LHR",
            end_city="JFK",
            end_iata="JFK",
        )
    )

    await db_session.flush()

    target = ConsolidationTarget.from_trip(target_trip, [seg_t])
    results = await consolidation_candidates(db_session, user, target)

    # Cap is 3; we have HIGH1, HIGH2, MEDIUM, LOW — so top 3 = HIGH1, HIGH2, MEDIUM
    assert len(results) == 3
    assert results[0].weight.name == "HIGH"
    assert results[1].weight.name == "HIGH"
    assert results[2].weight.name == "MEDIUM"
    # Within HIGH: newer start_date first (high1 start=2026-03-05 > high2 start=2026-03-01)
    assert results[0].trip.id == high1.id
    assert results[1].trip.id == high2.id
