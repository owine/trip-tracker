"""GET /trips/<id>/map: per-trip view + weather card overlay."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, patch

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
from trip_tracker.weather.client import DailyForecast, Forecast


def _cookie(user, settings):
    return {
        "tt_session": encode_session(
            SessionPayload(user_id=user.id, oidc_subject=user.oidc_subject),
            secret=settings.session_secret.get_secret_value(),
            max_age=3600,
        )
    }


@asynccontextmanager
async def _ctx(app, settings, user):
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies=_cookie(user, settings),
        ) as c,
    ):
        yield c


@pytest.fixture
def authenticated_client_factory(db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)

    def _make(user):
        settings = Settings()
        app = create_app(settings=settings)
        return _ctx(app, settings, user)

    return _make


async def _seed_with_future_trip(db: AsyncSession) -> tuple[User, Trip]:
    u = User(oidc_subject="t1", email="t1@x.com", display_name="T1")
    db.add(u)
    await db.flush()
    soon = date.today() + timedelta(days=5)
    t = Trip(
        title="Paris",
        start_date=soon,
        end_date=soon + timedelta(days=6),
        created_by=u.id,
    )
    db.add(t)
    await db.flush()
    db.add(TripTraveler(trip_id=t.id, user_id=u.id, role="owner"))
    db.add(
        Segment(
            trip_id=t.id,
            owner_user_id=u.id,
            type="flight",
            status="confirmed",
            start_at=datetime.combine(soon, datetime.min.time()).replace(hour=13, tzinfo=UTC),
            start_tz="UTC",
            start_location={"iata": "JFK"},
            end_location={"iata": "CDG"},
            details={"flight_number": "AF007"},
            parse_source="manual",
            parse_confidence=1.0,
        )
    )
    await db.commit()
    return u, t


@pytest.mark.asyncio
async def test_per_trip_renders_with_cached_weather(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    u, t = await _seed_with_future_trip(db_session)
    cached = Forecast(
        lat=49.01,
        lon=2.55,
        timezone="Europe/Paris",
        days=[
            DailyForecast(
                date=date.today(), temp_max_c=22.0, temp_min_c=14.0, weather_code=1, precip_prob=10
            )
        ],
    )
    with patch("trip_tracker.routes.map.get_cached", AsyncMock(return_value=cached)):
        async with authenticated_client_factory(u) as c:
            r = await c.get(f"/trips/{t.id}/map")
    assert r.status_code == 200
    assert "AF007" in r.text or "JFK" in r.text
    # Weather card: explicit temperature value (NOT "Paris" — that's also the trip title)
    assert (
        "22.0" in r.text
        or "22°C" in r.text
        or '"temp_max_c": 22' in r.text
        or '"temp_max_c":22' in r.text
    )


@pytest.mark.asyncio
async def test_per_trip_cold_cache_enqueues_refresh(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    u, t = await _seed_with_future_trip(db_session)
    enq = AsyncMock()
    with (
        patch("trip_tracker.routes.map.get_cached", AsyncMock(return_value=None)),
        patch("trip_tracker.routes.map._enqueue_weather_refresh", enq),
    ):
        async with authenticated_client_factory(u) as c:
            r = await c.get(f"/trips/{t.id}/map")
    assert r.status_code == 200
    enq.assert_awaited()  # one or more destinations triggered a refresh


@pytest.mark.asyncio
async def test_per_trip_404_for_non_traveler(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    _u, t = await _seed_with_future_trip(db_session)
    other = User(oidc_subject="t2", email="t2@x.com", display_name="T2")
    db_session.add(other)
    await db_session.commit()
    async with authenticated_client_factory(other) as c:
        r = await c.get(f"/trips/{t.id}/map")
    assert r.status_code in (403, 404)


@pytest.mark.asyncio
async def test_past_trip_skips_weather_fetch(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    """Trip end > today → no weather card, no enqueue, no get_cached."""
    u = User(oidc_subject="t3", email="t3@x.com", display_name="T3")
    db_session.add(u)
    await db_session.flush()
    long_ago = date.today() - timedelta(days=100)
    t = Trip(
        title="OldTrip",
        start_date=long_ago,
        end_date=long_ago + timedelta(days=3),
        created_by=u.id,
    )
    db_session.add(t)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=t.id, user_id=u.id, role="owner"))
    db_session.add(
        Segment(
            trip_id=t.id,
            owner_user_id=u.id,
            type="flight",
            status="confirmed",
            start_at=datetime.combine(long_ago, datetime.min.time()).replace(tzinfo=UTC),
            start_tz="UTC",
            start_location={"iata": "JFK"},
            end_location={"iata": "CDG"},
            details={"flight_number": "AF1"},
            parse_source="manual",
            parse_confidence=1.0,
        )
    )
    await db_session.commit()

    enq = AsyncMock()
    get_c = AsyncMock(return_value=None)
    with (
        patch("trip_tracker.routes.map.get_cached", get_c),
        patch("trip_tracker.routes.map._enqueue_weather_refresh", enq),
    ):
        async with authenticated_client_factory(u) as c:
            r = await c.get(f"/trips/{t.id}/map")
    assert r.status_code == 200
    enq.assert_not_awaited()
    get_c.assert_not_awaited()
