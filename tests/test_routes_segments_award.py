"""Award fields on flight + lodging forms + clear-award path."""

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
async def test_create_flight_with_award_writes_details(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """POST a flight create with all award fields filled. Assert details.award is written."""
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
                "award_program": "Chase Ultimate Rewards",
                "award_points_spent": "75000",
                "award_cash_copay_minor": "560",
                "award_cash_copay_currency": "USD",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303

    seg = (await db_session.execute(select(Segment))).scalar_one()
    await db_session.refresh(seg)
    assert seg.details is not None
    assert "award" in seg.details
    award = seg.details["award"]
    assert award["program"] == "Chase Ultimate Rewards"
    assert award["points_spent"] == 75000
    assert award["cash_copay_minor"] == 560
    assert award["cash_copay_currency"] == "USD"


@pytest.mark.asyncio
async def test_edit_flight_clear_award_removes_key(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Clear award checkbox removes award key while preserving other details."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = await _user(db_session)

    # Seed trip and segment with award.
    trip = Trip(
        title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5), created_by=user.id
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))

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
                "cash_copay_minor": 560,
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
        r = await c.post(
            f"/trips/{trip.id}/segments/{seg.id}",
            data={
                "type": "flight",
                "status": "confirmed",
                "start_local": "2026-06-01T09:00",
                "start_tz": "America/New_York",
                "end_local": "2026-06-01T22:00",
                "end_tz": "Europe/Paris",
                "origin_iata": "JFK",
                "origin_city": "New York",
                "destination_iata": "CDG",
                "destination_city": "Paris",
                "flight_number": "DL44",
                "seat": "12A",
                "clear_award": "1",
                # Award fields blank — clear_award short-circuits validation.
            },
            follow_redirects=False,
        )
    assert r.status_code == 303

    await db_session.refresh(seg)
    assert seg.details is not None
    assert "award" not in seg.details
    # Details are rebuilt from form on update, so award is gone.
    # Flight-specific fields (flight_number, seat) are preserved via _shape_payload.
    assert seg.details.get("flight_number") == "DL44"
    assert seg.details.get("seat") == "12A"


@pytest.mark.asyncio
async def test_edit_flight_clear_award_with_prefilled_fields_still_clears(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Regression (v0.8.0 smoke): browsers send clear_award=1 ALONG WITH the
    prefilled award fields (templates pre-populate from existing_award). Helper
    must short-circuit on the checkbox unconditionally, not fall through to
    re-create the award from the still-populated fields."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = await _user(db_session)

    trip = Trip(
        title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5), created_by=user.id
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))

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
                "cash_copay_minor": 560,
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
        r = await c.post(
            f"/trips/{trip.id}/segments/{seg.id}",
            data={
                "type": "flight",
                "status": "confirmed",
                "start_local": "2026-06-01T09:00",
                "start_tz": "America/New_York",
                "end_local": "2026-06-01T22:00",
                "end_tz": "Europe/Paris",
                "origin_iata": "JFK",
                "origin_city": "New York",
                "destination_iata": "CDG",
                "destination_city": "Paris",
                "flight_number": "DL44",
                "seat": "12A",
                "clear_award": "1",
                # Browser-realistic: award fields ARE submitted (prefilled from existing_award).
                "award_program": "Chase Ultimate Rewards",
                "award_points_spent": "75000",
                "award_cash_copay_minor": "560",
                "award_cash_copay_currency": "USD",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303

    await db_session.refresh(seg)
    assert seg.details is not None
    assert "award" not in seg.details, "clear_award=1 with prefilled fields must still clear"


@pytest.mark.asyncio
async def test_award_zero_points_rejected_with_form_error(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """POST flight create with award_points_spent=0. Assert 200 form re-render with error."""
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
                "award_program": "Chase Ultimate Rewards",
                "award_points_spent": "0",  # Invalid: points_spent must be >= 1
                "award_cash_copay_minor": "560",
                "award_cash_copay_currency": "USD",
            },
            follow_redirects=False,
        )
    assert r.status_code == 200
    assert "points_spent" in r.text  # Field name surfaced in error
    assert "https://errors.pydantic.dev" not in r.text  # Pydantic doc URL not leaked


@pytest.mark.asyncio
async def test_lodging_award_writes_details(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """POST a lodging create with all award fields. Assert details.award is written."""
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
                "award_program": "Marriott Bonvoy",
                "award_points_spent": "50000",
                "award_cash_copay_minor": "250",
                "award_cash_copay_currency": "EUR",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303

    seg = (await db_session.execute(select(Segment))).scalar_one()
    await db_session.refresh(seg)
    assert seg.details is not None
    assert "award" in seg.details
    award = seg.details["award"]
    assert award["program"] == "Marriott Bonvoy"
    assert award["points_spent"] == 50000
    assert award["cash_copay_minor"] == 250
    assert award["cash_copay_currency"] == "EUR"
