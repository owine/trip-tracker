"""TripTraveler: composite PK + role check + cascade."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User


async def _trip_with_user(db: AsyncSession) -> tuple[Trip, User]:
    u = User(oidc_subject="x", email="x@example.com", display_name="X")
    db.add(u)
    await db.flush()
    t = Trip(title="T", start_date=date(2026, 1, 1), end_date=date(2026, 1, 2), created_by=u.id)
    db.add(t)
    await db.commit()
    return t, u


@pytest.mark.asyncio
async def test_create(db_session: AsyncSession) -> None:
    trip, user = await _trip_with_user(db_session)
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    await db_session.commit()


@pytest.mark.asyncio
async def test_role_check(db_session: AsyncSession) -> None:
    trip, user = await _trip_with_user(db_session)
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="bogus"))
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_cascade_on_trip_delete(db_session: AsyncSession) -> None:
    trip, user = await _trip_with_user(db_session)
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    await db_session.commit()
    await db_session.delete(trip)
    await db_session.commit()
    rows = (await db_session.execute(select(TripTraveler))).scalars().all()
    assert rows == []
