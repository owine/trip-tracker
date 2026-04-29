"""Segment: per-type CRUD, jsonb roundtrip, generated search_text, FK + check constraints."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.raw_email import RawEmail
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User


async def _trip_with_user(db: AsyncSession) -> tuple[Trip, User]:
    u = User(oidc_subject="s", email="s@example.com", display_name="S")
    db.add(u)
    await db.flush()
    t = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5), created_by=u.id)
    db.add(t)
    await db.commit()
    return t, u


@pytest.mark.asyncio
async def test_create_flight(db_session: AsyncSession) -> None:
    trip, user = await _trip_with_user(db_session)
    seg = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="flight",
        status="confirmed",
        provider="Delta",
        confirmation_number="ABC123",
        start_at=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        start_tz="America/New_York",
        end_at=datetime(2026, 6, 1, 21, 0, tzinfo=UTC),
        end_tz="Europe/Paris",
        start_location={"iata": "JFK", "name": "JFK Airport", "city": "New York"},
        end_location={"iata": "CDG", "name": "Charles de Gaulle", "city": "Paris"},
        details={"flight_number": "DL44", "seat": "12A"},
        parse_source="manual",
        parse_confidence=1.0,
    )
    db_session.add(seg)
    await db_session.commit()
    await db_session.refresh(seg)

    assert seg.id is not None
    assert seg.start_location["iata"] == "JFK"
    assert seg.details["flight_number"] == "DL44"


@pytest.mark.asyncio
async def test_search_text_generated(db_session: AsyncSession) -> None:
    trip, user = await _trip_with_user(db_session)
    seg = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="flight",
        status="confirmed",
        provider="Delta",
        confirmation_number="ABC123",
        start_at=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        start_tz="UTC",
        start_location={"name": "JFK", "city": "New York"},
        end_location={"name": "CDG", "city": "Paris"},
        parse_source="manual",
        parse_confidence=1.0,
    )
    db_session.add(seg)
    await db_session.commit()

    row = await db_session.execute(
        text("SELECT search_text::text FROM segments WHERE id = :id"),
        {"id": seg.id},
    )
    sv = row.scalar_one()
    assert "delta" in sv.lower()
    assert "abc123" in sv.lower()
    assert "paris" in sv.lower()


@pytest.mark.asyncio
async def test_type_check(db_session: AsyncSession) -> None:
    trip, user = await _trip_with_user(db_session)
    seg = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="bogus",
        status="confirmed",
        start_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_tz="UTC",
        parse_source="manual",
        parse_confidence=1.0,
    )
    db_session.add(seg)
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_confidence_range(db_session: AsyncSession) -> None:
    trip, user = await _trip_with_user(db_session)
    seg = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="flight",
        status="confirmed",
        start_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_tz="UTC",
        parse_source="manual",
        parse_confidence=1.5,
    )
    db_session.add(seg)
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_raw_email_id_roundtrip(db_session: AsyncSession) -> None:
    trip, user = await _trip_with_user(db_session)
    raw = RawEmail(
        to_address="trips@example.com",
        from_address="test@example.com",
        message_id="msg@example.com",
        mime_blob=b"test mime",
        headers={"Content-Type": "text/plain"},
    )
    db_session.add(raw)
    await db_session.flush()

    seg = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="flight",
        status="confirmed",
        start_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_tz="UTC",
        parse_source="email",
        parse_confidence=0.85,
        raw_email_id=raw.id,
    )
    db_session.add(seg)
    await db_session.commit()
    await db_session.refresh(seg)

    assert seg.raw_email_id == raw.id
