"""Segments routes: type picker, per-type forms, create with implicit trip."""

from __future__ import annotations

from datetime import UTC, date, datetime

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


async def _seed_flight(db: AsyncSession, owner: User, trip: Trip) -> Segment:
    seg = Segment(
        trip_id=trip.id,
        owner_user_id=owner.id,
        type="flight",
        status="confirmed",
        provider="Delta",
        confirmation_number="ABC123",
        start_at=datetime(2026, 6, 1, 13, 0, tzinfo=UTC),  # 09:00 EDT
        start_tz="America/New_York",
        end_at=datetime(2026, 6, 2, 2, 0, tzinfo=UTC),  # 22:00 CEST
        end_tz="Europe/Paris",
        start_location={"iata": "JFK", "city": "New York"},
        end_location={"iata": "CDG", "city": "Paris"},
        details={"flight_number": "DL44", "seat": "12A"},
        parse_source="manual",
        parse_confidence=1.0,
    )
    db.add(seg)
    await db.commit()
    return seg


@pytest.mark.asyncio
async def test_edit_segment_renders_prefilled_form(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = await _user(db_session)
    trip = Trip(
        title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5), created_by=user.id
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    seg = await _seed_flight(db_session, user, trip)

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
        r = await c.get(f"/trips/{trip.id}/segments/{seg.id}/edit")

    assert r.status_code == 200
    # Prefilled values from the segment:
    assert "Delta" in r.text
    assert "ABC123" in r.text
    assert "DL44" in r.text
    assert "JFK" in r.text
    assert "CDG" in r.text
    # The local datetime display (note: 13:00 UTC → 09:00 in America/New_York):
    assert "2026-06-01T09:00" in r.text
    # Regression: the edit-form must POST to the update endpoint, not the bare
    # /segments create endpoint (or every "edit" silently creates a duplicate).
    # Caught in v0.8.0 smoke.
    assert f'action="/trips/{trip.id}/segments/{seg.id}"' in r.text
    assert 'action="/segments"' not in r.text
    # Title + button reflect edit mode, not "New flight" / "Create".
    # `r.text` may have whitespace from Jinja's multi-line if/else; collapse before checking.
    collapsed = " ".join(r.text.split())
    assert "Edit flight" in collapsed
    assert "Save changes" in collapsed
    assert "New flight" not in collapsed
    assert ">Create<" not in collapsed


@pytest.mark.asyncio
async def test_edit_segment_round_trip_updates_db(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = await _user(db_session)
    trip = Trip(
        title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5), created_by=user.id
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    seg = await _seed_flight(db_session, user, trip)

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
            f"/trips/{trip.id}/segments/{seg.id}",
            data={
                "type": "flight",
                "trip_selector_existing_trip_id": str(trip.id),
                "status": "confirmed",
                "provider": "Delta",
                "confirmation_number": "ABC123",
                "flight_number": "DL44",
                "origin_iata": "JFK",
                "origin_city": "New York",
                "destination_iata": "ORY",  # changed
                "destination_city": "Paris",
                "start_local": "2026-06-01T09:00",
                "start_tz": "America/New_York",
                "end_local": "2026-06-01T22:00",
                "end_tz": "Europe/Paris",
                "seat": "1A",  # changed
            },
            follow_redirects=False,
        )
    assert r.status_code == 303

    await db_session.refresh(seg)
    assert seg.end_location["iata"] == "ORY"
    assert seg.details["seat"] == "1A"
    # confirmation/provider/flight_number unchanged:
    assert seg.confirmation_number == "ABC123"


@pytest.mark.asyncio
async def test_delete_segment_removes_row(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = await _user(db_session)
    trip = Trip(
        title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5), created_by=user.id
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    seg = await _seed_flight(db_session, user, trip)

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
            f"/trips/{trip.id}/segments/{seg.id}/delete",
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == f"/trips/{trip.id}"

    rows = (await db_session.execute(select(Segment))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_edit_segment_404_for_non_traveler(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    creator = await _user(db_session)
    other = User(oidc_subject="other", email="other@x.com", display_name="O")
    db_session.add(other)
    await db_session.flush()
    trip = Trip(
        title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5), created_by=creator.id
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=creator.id, role="owner"))
    seg = await _seed_flight(db_session, creator, trip)

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies=_cookie(other, settings),
        ) as c,
    ):
        r_edit = await c.get(f"/trips/{trip.id}/segments/{seg.id}/edit")
        r_post = await c.post(
            f"/trips/{trip.id}/segments/{seg.id}",
            data={"type": "flight"},
            follow_redirects=False,
        )
        r_del = await c.post(
            f"/trips/{trip.id}/segments/{seg.id}/delete",
            follow_redirects=False,
        )

    # All three must 404 — non-member can't see the trip exists.
    assert r_edit.status_code == 404
    assert r_post.status_code == 404
    assert r_del.status_code == 404
