"""Public GET /ics/<token>.ics: auth, content, 404 shapes, UID stability."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime

import httpx
import pytest
from icalendar import Calendar
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.config import Settings
from trip_tracker.ics.tokens import generate_token
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User


@asynccontextmanager
async def _client(settings: Settings):
    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
    ):
        yield c


async def _seed_user_with_token(
    db: AsyncSession, *, with_segment: bool = True
) -> tuple[User, str, Trip | None, Segment | None]:
    u = User(oidc_subject="ics1", email="ics1@x.com", display_name="ICS Tester")
    db.add(u)
    await db.flush()
    plaintext, h = generate_token()
    u.ics_token_hash = h
    trip = seg = None
    if with_segment:
        trip = Trip(
            title="Paris",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 7),
            created_by=u.id,
        )
        db.add(trip)
        await db.flush()
        db.add(TripTraveler(trip_id=trip.id, user_id=u.id, role="owner"))
        seg = Segment(
            trip_id=trip.id,
            owner_user_id=u.id,
            type="flight",
            status="confirmed",
            confirmation_number="K8YH3M",
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
        db.add(seg)
    await db.commit()
    return u, plaintext, trip, seg


@pytest.mark.asyncio
async def test_valid_token_returns_calendar(
    db_url: str, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    _, plaintext, _, _ = await _seed_user_with_token(db_session)
    async with _client(settings) as c:
        r = await c.get(f"/ics/{plaintext}.ics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    assert "attachment" in r.headers.get("content-disposition", "")
    cal = Calendar.from_ical(r.text)
    events = [c for c in cal.subcomponents if c.name == "VEVENT"]
    assert len(events) == 1
    assert "AF007" in str(events[0]["SUMMARY"])


@pytest.mark.asyncio
async def test_invalid_token_returns_404(
    db_url: str, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    async with _client(settings) as c:
        r = await c.get("/ics/totally-bogus-token.ics")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_user_with_null_token_returns_404(
    db_url: str, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user exists but has ics_token_hash=NULL: any token → 404."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    u = User(oidc_subject="ics-null", email="null@x.com", display_name="NoToken")
    db_session.add(u)
    await db_session.commit()
    plaintext, _ = generate_token()
    async with _client(settings) as c:
        r = await c.get(f"/ics/{plaintext}.ics")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_uid_stable_across_two_fetches(
    db_url: str, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    _, plaintext, _, _ = await _seed_user_with_token(db_session)
    async with _client(settings) as c:
        body1 = (await c.get(f"/ics/{plaintext}.ics")).text
        body2 = (await c.get(f"/ics/{plaintext}.ics")).text
    cal1 = Calendar.from_ical(body1)
    cal2 = Calendar.from_ical(body2)
    uid1 = str(next(c for c in cal1.subcomponents if c.name == "VEVENT")["UID"])
    uid2 = str(next(c for c in cal2.subcomponents if c.name == "VEVENT")["UID"])
    assert uid1 == uid2


@pytest.mark.asyncio
async def test_no_session_required(
    db_url: str, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The /ics/ route must NOT redirect or 401 when there's no session cookie."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    _, plaintext, _, _ = await _seed_user_with_token(db_session)
    async with _client(settings) as c:
        r = await c.get(f"/ics/{plaintext}.ics", follow_redirects=False)
    assert r.status_code == 200  # NOT 302/401


@pytest.mark.asyncio
async def test_segments_filtered_by_traveler_ids(
    db_url: str, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another user's segments don't leak into the feed."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    _, plaintext, _, _ = await _seed_user_with_token(db_session)
    other = User(oidc_subject="ics-other", email="other@x.com", display_name="Other")
    db_session.add(other)
    await db_session.flush()
    other_trip = Trip(
        title="OtherTrip",
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
            provider="Other Airline",
            start_at=datetime(2026, 7, 1, 9, tzinfo=UTC),
            start_tz="UTC",
            parse_source="manual",
            parse_confidence=1.0,
        )
    )
    await db_session.commit()
    async with _client(settings) as c:
        body = (await c.get(f"/ics/{plaintext}.ics")).text
    assert "Other Airline" not in body
    assert "AF007" in body
