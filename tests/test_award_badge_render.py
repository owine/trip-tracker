"""Award badge rendering on trip detail segment rows."""

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
async def test_badge_renders_for_award_segment(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Trip detail page renders award badge with points_spent and program_short."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = await _user(db_session)

    # Seed trip and segment with award details.
    trip = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5))
    db_session.add(trip)
    await db_session.flush()

    seg = Segment(
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
            "seat": "12A",
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
        r = await c.get(f"/trips/{trip.id}")

    assert r.status_code == 200
    # Badge should contain "75k" (k_format of 75000).
    assert "75k" in r.text
    # Badge should contain "Chase UR" (program_short).
    assert "Chase UR" in r.text
    # Verify the airplane glyph is present.
    assert "&#9992;" in r.text


@pytest.mark.asyncio
async def test_badge_omits_saved_when_no_equivalent(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Badge omits 'saved ~' line when cash_equivalent_minor is None."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = await _user(db_session)

    trip = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5))
    db_session.add(trip)
    await db_session.flush()

    seg = Segment(
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
            "seat": "12A",
            "award": {
                "program": "Chase Ultimate Rewards",
                "points_spent": 75000,
                "cash_copay_minor": 0,
                "cash_copay_currency": "USD",
                # No cash_equivalent_minor — saved line should be omitted.
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
        r = await c.get(f"/trips/{trip.id}")

    assert r.status_code == 200
    # Badge should render.
    assert "75k" in r.text
    # But "saved" line should be absent.
    assert "saved" not in r.text


@pytest.mark.asyncio
async def test_badge_omits_copay_when_zero(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Badge omits '+ USD 0.00' line when cash_copay_minor is 0."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = await _user(db_session)

    trip = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5))
    db_session.add(trip)
    await db_session.flush()

    seg = Segment(
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
            "seat": "12A",
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
        r = await c.get(f"/trips/{trip.id}")

    assert r.status_code == 200
    # Badge should render.
    assert "75k" in r.text
    # But copay line should not contain "USD" with "0.00" due to the check:
    # {% if award.cash_copay_minor and award.cash_copay_minor > 0 %}
    assert "+ USD" not in r.text
