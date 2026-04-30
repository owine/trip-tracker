"""Segments form prefill path: ?from_raw_email=<id> shows ✨ indicators."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.models.forwarding_alias import ForwardingAlias
from trip_tracker.models.raw_email import RawEmail
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User


def _cookie(user: User, settings: Settings) -> dict[str, str]:
    return {
        "tt_session": encode_session(
            SessionPayload(user_id=user.id, oidc_subject=user.oidc_subject),
            secret=settings.session_secret.get_secret_value(),
            max_age=3600,
        )
    }


@pytest.mark.asyncio
async def test_edit_with_parse_source_shows_sparkle(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """A segment with parse_source != 'manual' renders with ✨ on the edit form."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = User(oidc_subject="p", email="p@x.com", display_name="P")
    db_session.add(user)
    await db_session.flush()
    db_session.add(ForwardingAlias(local_part="oliver", user_id=user.id))
    raw = RawEmail(
        id=uuid.uuid4(),
        received_at=datetime.now(tz=UTC),
        to_address="oliver@trips.example.com",
        from_address="x@y.com",
        subject="t",
        message_id=f"<{uuid.uuid4()}@x>",
        mime_blob=b"",
        headers={},
        parse_status="review",
    )
    trip = Trip(
        title="T",
        start_date=datetime(2026, 6, 1).date(),
        end_date=datetime(2026, 6, 5).date(),
        created_by=user.id,
    )
    db_session.add_all([raw, trip])
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    seg = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="flight",
        status="confirmed",
        provider="Air France",
        confirmation_number="ABC123",
        start_at=datetime(2026, 6, 1, 13, tzinfo=UTC),
        start_tz="UTC",
        start_location={"iata": "JFK"},
        end_location={"iata": "CDG"},
        details={"flight_number": "AF44"},
        parse_source="rules:air_france",
        parse_confidence=0.9,
        raw_email_id=raw.id,
    )
    db_session.add(seg)
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(user, settings)
        ) as c,
    ):
        r = await c.get(f"/trips/{trip.id}/segments/{seg.id}/edit?from_raw_email={raw.id}")
    assert r.status_code == 200
    assert "✨" in r.text


@pytest.mark.asyncio
async def test_edit_manual_segment_no_sparkle(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Manual segment (parse_source='manual') — no ✨."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = User(oidc_subject="m", email="m@x.com", display_name="M")
    db_session.add(user)
    await db_session.flush()
    trip = Trip(
        title="T",
        start_date=datetime(2026, 6, 1).date(),
        end_date=datetime(2026, 6, 5).date(),
        created_by=user.id,
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    seg = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="flight",
        status="confirmed",
        start_at=datetime(2026, 6, 1, 13, tzinfo=UTC),
        start_tz="UTC",
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
            transport=transport, base_url="http://test", cookies=_cookie(user, settings)
        ) as c,
    ):
        r = await c.get(f"/trips/{trip.id}/segments/{seg.id}/edit")
    assert r.status_code == 200
    assert "✨" not in r.text
