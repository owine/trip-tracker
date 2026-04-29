"""Segments routes: type picker, per-type forms, create with implicit trip."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
from sqlalchemy import select
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


def _cookie(user: User, settings: Settings) -> dict[str, str]:
    return {
        "tt_session": encode_session(
            SessionPayload(user_id=user.id, oidc_subject=user.oidc_subject),
            secret=settings.session_secret.get_secret_value(),
            max_age=3600,
        )
    }


@pytest.mark.asyncio
async def test_type_picker(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = await _user(db_session)
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
        r = await c.get("/segments/new")
    assert r.status_code == 200
    for t in ["Flight", "Lodging", "Car", "Train", "Transfer", "Activity"]:
        assert t in r.text


@pytest.mark.asyncio
async def test_create_flight_with_new_trip(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = await _user(db_session)
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
        r = await c.post(
            "/segments",
            data={
                "type": "flight",
                "trip_selector_new_trip_title": "Paris May 2026",
                "status": "confirmed",
                "provider": "Delta",
                "confirmation_number": "ABC123",
                "flight_number": "DL44",
                "origin_iata": "JFK",
                "origin_city": "New York",
                "destination_iata": "CDG",
                "destination_city": "Paris",
                "start_local": "2026-06-01T09:00",
                "start_tz": "America/New_York",
                "end_local": "2026-06-01T22:00",
                "end_tz": "Europe/Paris",
                "seat": "12A",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/trips/")

    trip = (await db_session.execute(select(Trip))).scalar_one()
    assert trip.title == "Paris May 2026"
    assert trip.primary_destination == "Paris"  # end_location.city for flights

    seg = (await db_session.execute(select(Segment))).scalar_one()
    assert seg.type == "flight"
    assert seg.start_location["iata"] == "JFK"
    assert seg.details["flight_number"] == "DL44"
    assert seg.parse_source == "manual"
    assert seg.parse_confidence == 1.0


@pytest.mark.asyncio
async def test_create_lodging_destination_from_start(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = await _user(db_session)
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
        r = await c.post(
            "/segments",
            data={
                "type": "lodging",
                "trip_selector_new_trip_title": "Hotel Trip",
                "status": "confirmed",
                "hotel_name": "Le Marais Hotel",
                "city": "Paris",
                "country": "France",
                "start_local": "2026-06-01T15:00",
                "start_tz": "Europe/Paris",
                "end_local": "2026-06-05T11:00",
                "end_tz": "Europe/Paris",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303
    trip = (await db_session.execute(select(Trip))).scalar_one()
    assert trip.primary_destination == "Paris"  # start_location.city for lodging


@pytest.mark.asyncio
async def test_create_segment_existing_trip_widens_dates(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = await _user(db_session)
    trip = Trip(
        title="T", start_date=date(2026, 6, 5), end_date=date(2026, 6, 7), created_by=user.id
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
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
        r = await c.post(
            "/segments",
            data={
                "type": "flight",
                "trip_selector_existing_trip_id": str(trip.id),
                "status": "confirmed",
                "start_local": "2026-06-01T09:00",  # before existing trip start
                "start_tz": "UTC",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303
    await db_session.refresh(trip)
    assert trip.start_date == date(2026, 6, 1)  # widened
    assert trip.end_date == date(2026, 6, 7)  # unchanged
