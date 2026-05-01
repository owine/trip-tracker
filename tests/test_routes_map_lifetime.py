"""GET /map: lifetime atlas — auth + all-trips JSON marshaling."""

from __future__ import annotations

from contextlib import asynccontextmanager
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


async def _seed(db: AsyncSession) -> User:
    u = User(oidc_subject="m1", email="m1@x.com", display_name="M1")
    db.add(u)
    await db.flush()
    t = Trip(
        title="Paris",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 7),
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
            provider="Air France",
            start_at=datetime(2026, 6, 1, 13, tzinfo=UTC),
            start_tz="UTC",
            end_at=datetime(2026, 6, 1, 22, tzinfo=UTC),
            end_tz="Europe/Paris",
            start_location={"iata": "JFK", "city": "New York"},
            end_location={"iata": "CDG", "city": "Paris"},
            details={"flight_number": "AF007"},
            parse_source="manual",
            parse_confidence=1.0,
        )
    )
    await db.commit()
    return u


@pytest.mark.asyncio
async def test_anonymous_request_401(db_url, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
    ):
        r = await c.get("/map", follow_redirects=False)
    assert r.status_code in (401, 302, 303)


@pytest.mark.asyncio
async def test_authed_request_200(db_session: AsyncSession, authenticated_client_factory) -> None:
    u = await _seed(db_session)
    async with authenticated_client_factory(u) as c:
        r = await c.get("/map")
    assert r.status_code == 200
    assert "leaflet" in r.text.lower()
    assert 'id="map-data"' in r.text


@pytest.mark.asyncio
async def test_map_data_includes_jfk_and_cdg_markers(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    u = await _seed(db_session)
    async with authenticated_client_factory(u) as c:
        r = await c.get("/map")
    # JSON blob is in <script id="map-data" type="application/json">...</script>
    # JFK is at lat 40.64, CDG at lat 49.01.
    assert "40.6" in r.text  # JFK lat (approx)
    assert "49.0" in r.text  # CDG lat (approx)


@pytest.mark.asyncio
async def test_other_users_segments_excluded(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    u_self = await _seed(db_session)
    other = User(oidc_subject="m2", email="m2@x.com", display_name="M2")
    db_session.add(other)
    await db_session.flush()
    other_trip = Trip(
        title="Berlin",
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 5),
        created_by=other.id,
    )
    db_session.add(other_trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=other_trip.id, user_id=other.id, role="owner"))
    db_session.add(
        Segment(
            trip_id=other_trip.id,
            owner_user_id=other.id,
            type="flight",
            status="confirmed",
            provider="Lufthansa",
            start_at=datetime(2026, 7, 1, 9, tzinfo=UTC),
            start_tz="UTC",
            start_location={"iata": "JFK"},
            end_location={"iata": "BER"},
            details={"flight_number": "LH401"},
            parse_source="manual",
            parse_confidence=1.0,
        )
    )
    await db_session.commit()

    async with authenticated_client_factory(u_self) as c:
        r = await c.get("/map")
    assert "LH401" not in r.text
    assert "BER" not in r.text  # The other user's destination shouldn't leak
