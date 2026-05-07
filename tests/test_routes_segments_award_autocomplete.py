"""Award-program autocomplete endpoint tests."""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest
from fastapi import Response as _Response
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import OWNER_USER_ID, set_session_cookie
from trip_tracker.config import Settings
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User


async def _user(db: AsyncSession) -> User:
    u = User(id=OWNER_USER_ID, email="u@x.com", display_name="U")
    db.add(u)
    await db.commit()
    return u


def _cookie(user: User, settings: Settings) -> dict[str, str]:
    r = _Response()
    set_session_cookie(r, user_id=user.id, settings=settings)
    return {"tt_session": r.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


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
    )
    db_session.add(trip)
    await db_session.flush()

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

    assert r.status_code == 401
