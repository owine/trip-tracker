"""require_admin and require_traveler dependencies."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.deps import require_admin, require_traveler
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User


def _user(*, admin: bool = False) -> User:
    return User(
        id=uuid.uuid4(),
        oidc_subject="s",
        email="x@y.com",
        display_name="X",
        is_admin=admin,
    )


@pytest.mark.asyncio
async def test_require_admin_allows_admin() -> None:
    user = _user(admin=True)
    out = await require_admin(user=user)
    assert out is user


@pytest.mark.asyncio
async def test_require_admin_blocks_non_admin() -> None:
    user = _user(admin=False)
    with pytest.raises(HTTPException) as ei:
        await require_admin(user=user)
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_require_traveler_allows_member(db_session: AsyncSession) -> None:
    creator = User(oidc_subject="c", email="c@x.com", display_name="C")
    db_session.add(creator)
    await db_session.flush()
    trip = Trip(
        title="T", start_date=date(2026, 1, 1), end_date=date(2026, 1, 2), created_by=creator.id
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=creator.id, role="owner"))
    await db_session.commit()

    out = await require_traveler(trip_id=trip.id, user=creator, db=db_session)
    assert out.id == trip.id


@pytest.mark.asyncio
async def test_require_traveler_404_for_non_member(db_session: AsyncSession) -> None:
    creator = User(oidc_subject="c2", email="c2@x.com", display_name="C")
    other = User(oidc_subject="o", email="o@x.com", display_name="O")
    db_session.add_all([creator, other])
    await db_session.flush()
    trip = Trip(
        title="T", start_date=date(2026, 1, 1), end_date=date(2026, 1, 2), created_by=creator.id
    )
    db_session.add(trip)
    await db_session.commit()

    with pytest.raises(HTTPException) as ei:
        await require_traveler(trip_id=trip.id, user=other, db=db_session)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_require_traveler_404_for_unknown_trip(db_session: AsyncSession) -> None:
    user = _user()
    with pytest.raises(HTTPException) as ei:
        await require_traveler(trip_id=uuid.uuid4(), user=user, db=db_session)
    assert ei.value.status_code == 404
