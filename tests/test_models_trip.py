"""Trip model: CRUD + date range CHECK + FK to creator."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User


async def _user(db: AsyncSession) -> User:
    u = User(oidc_subject="creator", email="c@example.com", display_name="C")
    db.add(u)
    await db.commit()
    return u


@pytest.mark.asyncio
async def test_create(db_session: AsyncSession) -> None:
    user = await _user(db_session)
    t = Trip(
        title="Paris May 2026",
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 8),
        primary_destination="Paris",
        created_by=user.id,
    )
    db_session.add(t)
    await db_session.commit()
    assert t.id is not None


@pytest.mark.asyncio
async def test_date_range_check(db_session: AsyncSession) -> None:
    user = await _user(db_session)
    t = Trip(
        title="Bad",
        start_date=date(2026, 5, 8),
        end_date=date(2026, 5, 1),
        created_by=user.id,
    )
    db_session.add(t)
    with pytest.raises(IntegrityError):
        await db_session.commit()
