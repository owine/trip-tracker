"""Award-program autocomplete endpoint tests."""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User


async def _user(db: AsyncSession) -> User:
    u = User(oidc_subject="s", email="u@x.com", display_name="U")
    db.add(u)
    await db.commit()
    return u


async def _other_user(db: AsyncSession) -> User:
    u = User(oidc_subject="other", email="other@x.com", display_name="Other")
    db.add(u)
    await db.commit()
    return u


def _cookie(user: User, settings: Settings) -> dict[str, str]:
    return {
        "tt_session": encode_session(
            SessionPayload(user_id=user.id, oidc_subject=user.oidc_subject),
            secret=settings.session_secret.get_secret_value(),
            max_age=3600,
        )
    }


@pytest.mark.asyncio
async def test_autocomplete_returns_distinct_recent_programs(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Seed 3 award segments with duplicate program. Assert endpoint returns
    distinct programs ordered by recency."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = await _user(db_session)

    # Seed trip for segments.
    trip = Trip(
        title="Test Trip",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        created_by=user.id,
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))

    # Seed 3 segments with awards. Intentional duplicate of program.
    programs = ["Chase Ultimate Rewards", "Marriott Bonvoy", "Chase Ultimate Rewards"]
    for i, prog in enumerate(programs):
        seg = Segment(
            trip_id=trip.id,
            owner_user_id=user.id,
            type="flight",
            status="confirmed",
            start_at=datetime(2026, 6, 1 + i, 9, 0, tzinfo=UTC),
            start_tz="America/New_York",
            end_at=datetime(2026, 6, 1 + i, 22, 0, tzinfo=UTC),
            end_tz="Europe/Paris",
            start_location={"iata": "JFK", "city": "New York"},
            end_location={"iata": "CDG", "city": "Paris"},
            details={
                "flight_number": "DL44",
                "award": {
                    "program": prog,
                    "points_spent": 75000,
                    "cash_copay_minor": 0,
                    "cash_copay_currency": "USD",
                },
            },
            parse_source="manual",
            parse_confidence=1.0,
        )
        db_session.add(seg)
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies=_cookie(user, settings),
        ) as c,
    ):
        r = await c.get("/segments/award-programs.json")

    assert r.status_code == 200
    programs_result = r.json()
    # Should have 2 distinct programs (duplicate filtered out).
    assert len(programs_result) == 2
    assert "Chase Ultimate Rewards" in programs_result
    assert "Marriott Bonvoy" in programs_result
    # Most recent first: segment with index 2 has "Chase Ultimate Rewards" (latest).
    assert programs_result[0] == "Chase Ultimate Rewards"
    assert programs_result[1] == "Marriott Bonvoy"


@pytest.mark.asyncio
async def test_autocomplete_only_returns_owned_segments(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Seed 1 award segment for test user and 1 for another user. Assert
    endpoint only returns the test user's programs."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = await _user(db_session)
    other_user = await _other_user(db_session)

    # Seed trips for both users.
    trip = Trip(
        title="User Trip",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        created_by=user.id,
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))

    other_trip = Trip(
        title="Other Trip",
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 5),
        created_by=other_user.id,
    )
    db_session.add(other_trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=other_trip.id, user_id=other_user.id, role="owner"))

    # Segment for test user.
    seg1 = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="flight",
        status="confirmed",
        start_at=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        start_tz="America/New_York",
        end_at=datetime(2026, 6, 1, 22, 0, tzinfo=UTC),
        end_tz="Europe/Paris",
        start_location={"iata": "JFK", "city": "New York"},
        end_location={"iata": "CDG", "city": "Paris"},
        details={
            "flight_number": "DL44",
            "award": {
                "program": "Chase Ultimate Rewards",
                "points_spent": 75000,
                "cash_copay_minor": 0,
                "cash_copay_currency": "USD",
            },
        },
        parse_source="manual",
        parse_confidence=1.0,
    )
    db_session.add(seg1)

    # Segment for other user.
    seg2 = Segment(
        trip_id=other_trip.id,
        owner_user_id=other_user.id,
        type="flight",
        status="confirmed",
        start_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
        start_tz="America/New_York",
        end_at=datetime(2026, 7, 1, 22, 0, tzinfo=UTC),
        end_tz="Europe/Paris",
        start_location={"iata": "ORD", "city": "Chicago"},
        end_location={"iata": "LHR", "city": "London"},
        details={
            "flight_number": "AA100",
            "award": {
                "program": "Amex Membership Rewards",
                "points_spent": 100000,
                "cash_copay_minor": 500,
                "cash_copay_currency": "USD",
            },
        },
        parse_source="manual",
        parse_confidence=1.0,
    )
    db_session.add(seg2)
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies=_cookie(user, settings),
        ) as c,
    ):
        r = await c.get("/segments/award-programs.json")

    assert r.status_code == 200
    programs_result = r.json()
    # Should only contain the test user's program.
    assert len(programs_result) == 1
    assert programs_result[0] == "Chase Ultimate Rewards"
    # Ensure other user's program is not included.
    assert "Amex Membership Rewards" not in programs_result


@pytest.mark.asyncio
async def test_autocomplete_unauthenticated_redirects(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Hit the endpoint without auth. Assert 401 or redirect."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={},
        ) as c,
    ):
        r = await c.get("/segments/award-programs.json", follow_redirects=False)

    # Depending on require_user impl, expect 401 or redirect.
    # Check the response behavior of other endpoints for consistency.
    assert r.status_code in (401, 307, 302)
