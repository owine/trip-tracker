"""trip_to_doc and segment_to_doc — pure mappers from ORM to Meili doc."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.session import OWNER_USER_ID
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User
from trip_tracker.search.sync import segment_to_doc, trip_to_doc


@pytest.mark.asyncio
async def test_trip_to_doc_basic_fields(db_session: AsyncSession) -> None:
    user = User(id=OWNER_USER_ID, email="u@x.com", display_name="U")
    db_session.add(user)
    await db_session.flush()
    trip = Trip(
        title="Paris May 2026",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        primary_destination="Paris",
    )
    db_session.add(trip)
    await db_session.commit()

    doc = await trip_to_doc(trip, db=db_session)
    assert doc["id"] == str(trip.id)
    assert doc["title"] == "Paris May 2026"
    assert doc["primary_destination"] == "Paris"
    assert doc["start_date"] == (trip.start_date - date(1970, 1, 1)).days
    assert doc["end_date"] == (trip.end_date - date(1970, 1, 1)).days
    assert doc["traveler_ids"] == [str(OWNER_USER_ID)]


@pytest.mark.asyncio
async def test_segment_to_doc_flight(db_session: AsyncSession) -> None:
    user = User(id=OWNER_USER_ID, email="u2@x.com", display_name="U2")
    db_session.add(user)
    await db_session.flush()
    trip = Trip(
        title="Trip",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
    )
    db_session.add(trip)
    await db_session.flush()
    seg = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="flight",
        status="confirmed",
        provider="Air France",
        confirmation_number="ABC123",
        start_at=datetime(2026, 6, 1, 13, tzinfo=UTC),
        start_tz="UTC",
        start_location={"iata": "JFK", "city": "New York"},
        end_location={"iata": "CDG", "city": "Paris"},
        details={"flight_number": "AF44", "notes": "anniversary trip", "seat": "12A"},
        parse_source="manual",
        parse_confidence=1.0,
    )
    db_session.add(seg)
    await db_session.commit()

    doc = await segment_to_doc(seg, db=db_session)
    assert doc["id"] == str(seg.id)
    assert doc["trip_id"] == str(trip.id)
    assert doc["traveler_ids"] == [str(OWNER_USER_ID)]
    assert doc["type"] == "flight"
    assert doc["provider"] == "Air France"
    assert doc["confirmation_number"] == "ABC123"
    assert doc["start_at_unix"] == int(seg.start_at.timestamp())
    assert doc["start_city"] == "New York"
    assert doc["end_city"] == "Paris"
    assert doc["vehicle_number"] == "AF44"
    assert doc["notes"] == "anniversary trip"


@pytest.mark.asyncio
async def test_segment_to_doc_lodging_no_vehicle_number(
    db_session: AsyncSession,
) -> None:
    """Lodging segments don't have a vehicle number — should be None."""
    user = User(id=OWNER_USER_ID, email="u3@x.com", display_name="U3")
    db_session.add(user)
    await db_session.flush()
    trip = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5))
    db_session.add(trip)
    await db_session.flush()
    seg = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="lodging",
        status="confirmed",
        start_at=datetime(2026, 6, 1, 15, tzinfo=UTC),
        start_tz="UTC",
        start_location={"name": "Le Marais Hotel", "city": "Paris"},
        details={"room_type": "Deluxe Suite"},
        parse_source="manual",
        parse_confidence=1.0,
    )
    db_session.add(seg)
    await db_session.commit()

    doc = await segment_to_doc(seg, db=db_session)
    assert doc["vehicle_number"] is None
    assert doc["start_city"] == "Paris"
    assert doc["notes"] is None


@pytest.mark.asyncio
async def test_segment_to_doc_car_no_vehicle_number(
    db_session: AsyncSession,
) -> None:
    """Car rental segments don't have vehicle_number even when car_class is set."""
    user = User(id=OWNER_USER_ID, email="ucar@x.com", display_name="UCAR")
    db_session.add(user)
    await db_session.flush()
    trip = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5))
    db_session.add(trip)
    await db_session.flush()
    seg = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="car",
        status="confirmed",
        start_at=datetime(2026, 6, 1, 14, tzinfo=UTC),
        start_tz="UTC",
        details={"car_class": "Compact"},
        parse_source="manual",
        parse_confidence=1.0,
    )
    db_session.add(seg)
    await db_session.commit()

    doc = await segment_to_doc(seg, db=db_session)
    assert doc["vehicle_number"] is None


@pytest.mark.asyncio
async def test_segment_to_doc_train_uses_train_number(
    db_session: AsyncSession,
) -> None:
    user = User(id=OWNER_USER_ID, email="u4@x.com", display_name="U4")
    db_session.add(user)
    await db_session.flush()
    trip = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5))
    db_session.add(trip)
    await db_session.flush()
    seg = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="train",
        status="confirmed",
        start_at=datetime(2026, 6, 2, 9, tzinfo=UTC),
        start_tz="UTC",
        start_location={"name": "Paris Gare de Lyon"},
        end_location={"name": "Lyon Part Dieu"},
        details={"train_number": "9573"},
        parse_source="manual",
        parse_confidence=1.0,
    )
    db_session.add(seg)
    await db_session.commit()

    doc = await segment_to_doc(seg, db=db_session)
    assert doc["vehicle_number"] == "9573"
