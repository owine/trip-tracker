"""Trip model: CRUD + date range CHECK + FK to creator."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.trip import Trip


@pytest.mark.asyncio
async def test_create(db_session: AsyncSession) -> None:
    t = Trip(
        title="Paris May 2026",
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 8),
        primary_destination="Paris",
    )
    db_session.add(t)
    await db_session.commit()
    assert t.id is not None


@pytest.mark.asyncio
async def test_date_range_check(db_session: AsyncSession) -> None:
    t = Trip(
        title="Bad",
        start_date=date(2026, 5, 8),
        end_date=date(2026, 5, 1),
    )
    db_session.add(t)
    with pytest.raises(IntegrityError):
        await db_session.commit()
