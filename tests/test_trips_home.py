"""Tests for trip_tracker.trips.home.infer_home (spec §3.2)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User

# ---------------------------------------------------------------------------
# Inline helpers
# ---------------------------------------------------------------------------


async def _seed_user_and_trip(db: AsyncSession) -> tuple[User, Trip]:
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
        start_date=datetime(2026, 1, 1).date(),
        end_date=datetime(2026, 12, 31).date(),
        created_by=user.id,
    )
    db.add(trip)
    await db.flush()

    return user, trip


async def _seed_segments(
    db: AsyncSession,
    *,
    trip_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    endpoints: list[tuple[str, str]],
    status: str = "confirmed",
    base_dt: datetime | None = None,
) -> None:
    """Insert one Segment per (start_city, end_city) pair in *endpoints*.

    *base_dt* is the start_at for the first segment; each subsequent segment
    is 1 day later so ordering is deterministic.
    """
    if base_dt is None:
        base_dt = datetime.now(UTC)

    for i, (s, e) in enumerate(endpoints):
        seg = Segment(
            trip_id=trip_id,
            owner_user_id=owner_user_id,
            type="flight",
            status=status,
            start_at=base_dt + timedelta(days=i),
            start_tz="UTC",
            start_location={"city": s},
            end_location={"city": e},
            details={},
            parse_source="test",
            parse_confidence=0.9,
        )
        db.add(seg)

    await db.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_top_endpoint_at_30pct_returns_city(db_session: AsyncSession) -> None:
    """6 out of 20 endpoints being 'NYC' (30 %) satisfies the dominance floor."""
    from trip_tracker.trips.home import infer_home

    user, trip = await _seed_user_and_trip(db_session)

    # 6 segments with "NYC" as start_city, 4 segments with "LHR" as start_city.
    # Each segment contributes 2 endpoint observations so totals per city:
    #   NYC: 6 (from start) + 6 (from end, same below)
    # Build it so NYC appears exactly 12/40 = 30 % of total observations.
    # Simpler: 10 segments — NYC as start+end on 6 of them, "LHR" start+end on 4.
    # total observations = 20; NYC = 12 (6*2); 12/20 = 60 % — fine.
    # Better: mix so NYC = exactly 30 %:
    #   6 segments: NYC→LHR  → NYC:6, LHR:6 (12 obs)
    #   7 segments: CDG→MIA  → CDG:7, MIA:7 (14 obs)
    # total = 26 obs; NYC = 6 / 26 ≈ 23 % — below 30 %. Not what we want.
    #
    # Easiest: make NYC both start+end on N segments and nothing else.
    #   6 segments NYC→NYC, 4 segments LHR→LHR
    #   total = 20 obs; NYC = 12, LHR = 8; 12/20 = 60 % ≥ 30 % → "NYC" ✓
    # But let's use a more realistic mix that hits exactly 30 %:
    #   3 segments NYC→NYC (6 obs NYC), rest are unique
    #   total = 20 obs; NYC = 6 / 20 = 30 % → "NYC" ✓
    #   Achieved with: 3 NYC→NYC + 7 unique pairs (7*2 = 14 other obs).
    endpoints = [("NYC", "NYC")] * 3 + [
        ("LHR", "CDG"),
        ("CDG", "FCO"),
        ("FCO", "MAD"),
        ("MAD", "BCN"),
        ("BCN", "AMS"),
        ("AMS", "FRA"),
        ("FRA", "ZRH"),
    ]  # 10 segments, 20 endpoint observations; NYC = 6/20 = 30 %

    await _seed_segments(
        db_session,
        trip_id=trip.id,
        owner_user_id=user.id,
        endpoints=endpoints,
    )

    result = await infer_home(db_session, user.id)
    assert result == "NYC"


@pytest.mark.asyncio
async def test_below_30pct_returns_none(db_session: AsyncSession) -> None:
    """4 out of 20 endpoint observations (20 %) is below the dominance floor."""
    from trip_tracker.trips.home import infer_home

    user, trip = await _seed_user_and_trip(db_session)

    # 2 segments NYC→NYC (4 NYC obs), 8 segments with unique city pairs (16 obs)
    # total = 20; NYC = 4/20 = 20 % < 30 % → None
    endpoints = [("NYC", "NYC")] * 2 + [
        ("LHR", "CDG"),
        ("CDG", "FCO"),
        ("FCO", "MAD"),
        ("MAD", "BCN"),
        ("BCN", "AMS"),
        ("AMS", "FRA"),
        ("FRA", "ZRH"),
        ("ZRH", "VIE"),
    ]  # 10 segments, 20 obs; NYC = 4/20 = 20 %

    await _seed_segments(
        db_session,
        trip_id=trip.id,
        owner_user_id=user.id,
        endpoints=endpoints,
    )

    result = await infer_home(db_session, user.id)
    assert result is None


@pytest.mark.asyncio
async def test_last_20_window_respected(db_session: AsyncSession) -> None:
    """Only the 20 most-recent segments count; older NYC-heavy history is ignored."""
    from trip_tracker.trips.home import infer_home

    user, trip = await _seed_user_and_trip(db_session)

    now = datetime.now(UTC)

    # Older batch (30 segments, all NYC→NYC): these should be pushed out of the
    # 20-segment window by the newer batch.
    await _seed_segments(
        db_session,
        trip_id=trip.id,
        owner_user_id=user.id,
        endpoints=[("NYC", "NYC")] * 30,
        base_dt=now - timedelta(days=60),
    )

    # Newer batch (20 segments, all PARIS→PARIS): these are the 20 most recent.
    # PARIS = 40/40 = 100 % → "PARIS"
    await _seed_segments(
        db_session,
        trip_id=trip.id,
        owner_user_id=user.id,
        endpoints=[("PARIS", "PARIS")] * 20,
        base_dt=now,  # more recent than the NYC batch
    )

    result = await infer_home(db_session, user.id)
    # If the window is respected, only PARIS shows up → "PARIS".
    # If the window were missing, NYC would dominate (30 segments vs 20).
    assert result == "PARIS"


@pytest.mark.asyncio
async def test_cancelled_excluded(db_session: AsyncSession) -> None:
    """Cancelled segments must not contribute to the home inference."""
    from trip_tracker.trips.home import infer_home

    user, trip = await _seed_user_and_trip(db_session)

    now = datetime.now(UTC)

    # 10 cancelled segments with NYC: should NOT count.
    await _seed_segments(
        db_session,
        trip_id=trip.id,
        owner_user_id=user.id,
        endpoints=[("NYC", "NYC")] * 10,
        status="cancelled",
        base_dt=now - timedelta(days=20),
    )

    # 10 confirmed segments with PARIS: these are the only confirmed ones.
    # PARIS = 20/20 = 100 % → "PARIS"
    await _seed_segments(
        db_session,
        trip_id=trip.id,
        owner_user_id=user.id,
        endpoints=[("PARIS", "PARIS")] * 10,
        status="confirmed",
        base_dt=now,
    )

    result = await infer_home(db_session, user.id)
    assert result == "PARIS"


@pytest.mark.asyncio
async def test_empty_history_returns_none(db_session: AsyncSession) -> None:
    """A user with no segments at all should get None."""
    from trip_tracker.trips.home import infer_home

    user, _ = await _seed_user_and_trip(db_session)

    result = await infer_home(db_session, user.id)
    assert result is None


@pytest.mark.asyncio
async def test_both_endpoints_contribute(db_session: AsyncSession) -> None:
    """Both start_location and end_location city values count toward home."""
    from trip_tracker.trips.home import infer_home

    user, trip = await _seed_user_and_trip(db_session)

    # 5 segments: NYC→LHR
    # Without both endpoints counting: NYC=5 obs, LHR=5 obs, total=5 (only start).
    # If only start counted: NYC = 5/5 = 100 % → "NYC".
    # With both endpoints:    NYC = 5, LHR = 5, total = 10; NYC = 50 % → "NYC".
    # To verify BOTH contribute, make a city appear *only* as the end endpoint
    # and confirm it can win if it has sufficient share.
    #
    # 4 segments: CDG→HOME  (HOME appears only as end_location, CDG only as start)
    # 2 segments: BKK→SYD   (HOME never appears)
    # Observations: CDG=4, HOME=4, BKK=2, SYD=2 → total=12; HOME=4/12≈33% ✓
    # CDG also = 4/12 ≈ 33 %, so they tie — Counter.most_common picks CDG.
    #
    # Use 5 CDG→HOME + 5 BKK→SYD:
    #   CDG=5, HOME=5, BKK=5, SYD=5 → total=20; HOME=5/20=25% → None. Not what we want.
    #
    # Use 4 LHR→HOME + 2 LHR→BKK:
    #   LHR=6 (start on all 6), HOME=4 (end only), BKK=2 (end only) → total=12
    #   LHR=6/12=50% → "LHR". HOME does contribute but LHR wins.
    #
    # Key design: make HOME appear ONLY as end_location and beat all start cities.
    # 4 segments CDG→HOME + 1 segment BKK→LHR:
    #   CDG=4, HOME=4, BKK=1, LHR=1 → total=10; CDG=HOME=4/10=40% (tie).
    #
    # Make HOME appear 5x as end, and each start city is unique (no repeats):
    # 5 segments: A→HOME, B→HOME, C→HOME, D→HOME, E→HOME
    #   A=B=C=D=E=1 (start), HOME=5 (end) → total=10; HOME=5/10=50% → "HOME" ✓
    # If end_location were ignored: each start city = 1/5 = 20% → None
    endpoints = [
        ("A", "HOME"),
        ("B", "HOME"),
        ("C", "HOME"),
        ("D", "HOME"),
        ("E", "HOME"),
    ]

    await _seed_segments(
        db_session,
        trip_id=trip.id,
        owner_user_id=user.id,
        endpoints=endpoints,
    )

    result = await infer_home(db_session, user.id)
    assert result == "HOME"
