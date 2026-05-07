"""Expense ORM: multi-FK cascade and column constraints."""

from __future__ import annotations

import uuid
from datetime import UTC, date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.session import OWNER_USER_ID
from trip_tracker.models.expense import Expense
from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User


@pytest.mark.asyncio
async def test_expense_cascades_with_trip(db_session: AsyncSession) -> None:
    """Trip delete -> expense rows gone (CASCADE)."""
    user = User(id=OWNER_USER_ID, email="e1@x.com", display_name="E1")
    db_session.add(user)
    await db_session.flush()
    trip = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5))
    db_session.add(trip)
    await db_session.flush()

    exp = Expense(
        trip_id=trip.id,
        owner_user_id=user.id,
        amount_minor=3800,
        currency="EUR",
        fx_rate=Decimal("1.0700000000"),
        amount_home_minor=4066,
        home_currency="USD",
        category="food",
        incurred_on=date(2026, 6, 4),
        status="paid",
    )
    db_session.add(exp)
    await db_session.commit()

    await db_session.delete(trip)
    await db_session.commit()

    rows = (await db_session.execute(select(Expense).where(Expense.id == exp.id))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_expense_segment_set_null_on_segment_delete(db_session: AsyncSession) -> None:
    """Segment delete -> expense.segment_id becomes NULL but row survives."""
    from datetime import datetime as _dt

    user = User(id=OWNER_USER_ID, email="e2@x.com", display_name="E2")
    db_session.add(user)
    await db_session.flush()
    trip = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5))
    db_session.add(trip)
    await db_session.flush()
    from trip_tracker.models.segment import Segment

    seg = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="lodging",
        status="confirmed",
        provider="Hotel X",
        start_at=_dt(2026, 6, 2, 15, 0, tzinfo=UTC),
        start_tz="UTC",
        end_at=_dt(2026, 6, 3, 11, 0, tzinfo=UTC),
        end_tz="UTC",
        details={},
        parse_source="manual",
        parse_confidence=1.0,
    )
    db_session.add(seg)
    await db_session.flush()
    exp = Expense(
        trip_id=trip.id,
        owner_user_id=user.id,
        segment_id=seg.id,
        amount_minor=20000,
        currency="USD",
        fx_rate=Decimal("1.0000000000"),
        amount_home_minor=20000,
        home_currency="USD",
        category="lodging",
        incurred_on=date(2026, 6, 2),
        status="paid",
    )
    db_session.add(exp)
    await db_session.commit()

    await db_session.delete(seg)
    await db_session.commit()
    await db_session.refresh(exp)
    assert exp.segment_id is None
    assert (await db_session.execute(select(Expense).where(Expense.id == exp.id))).scalar_one()


@pytest.mark.asyncio
async def test_expense_document_set_null_on_document_delete(db_session: AsyncSession) -> None:
    """Document delete -> expense.document_id becomes NULL but row survives."""
    from trip_tracker.models.document import Document

    user = User(id=OWNER_USER_ID, email="e3@x.com", display_name="E3")
    db_session.add(user)
    await db_session.flush()
    trip = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5))
    db_session.add(trip)
    await db_session.flush()
    doc = Document(
        owner_user_id=user.id,
        filename="receipt.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
        sha256="a" * 64,
        storage_key="docs/x.pdf",
    )
    db_session.add(doc)
    await db_session.flush()
    exp = Expense(
        trip_id=trip.id,
        owner_user_id=user.id,
        document_id=doc.id,
        amount_minor=3800,
        currency="EUR",
        fx_rate=Decimal("1.0700000000"),
        amount_home_minor=4066,
        home_currency="USD",
        category="food",
        incurred_on=date(2026, 6, 4),
        status="paid",
    )
    db_session.add(exp)
    await db_session.commit()

    await db_session.delete(doc)
    await db_session.commit()
    await db_session.refresh(exp)
    assert exp.document_id is None


@pytest.mark.asyncio
async def test_expense_owner_cascade_on_user_delete(db_session: AsyncSession) -> None:
    """User delete -> expense rows gone (CASCADE)."""
    # Use a distinct secondary user as expense owner to test the cascade.
    owner = User(id=uuid.UUID(int=999), email="e4o@x.com", display_name="E4O")
    db_session.add(owner)
    await db_session.flush()
    trip = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5))
    db_session.add(trip)
    await db_session.flush()
    exp = Expense(
        trip_id=trip.id,
        owner_user_id=owner.id,
        amount_minor=3800,
        currency="EUR",
        fx_rate=Decimal("1.0700000000"),
        amount_home_minor=4066,
        home_currency="USD",
        category="food",
        incurred_on=date(2026, 6, 4),
        status="paid",
    )
    db_session.add(exp)
    await db_session.commit()
    exp_id = exp.id

    await db_session.delete(owner)
    await db_session.commit()
    rows = (await db_session.execute(select(Expense).where(Expense.id == exp_id))).scalars().all()
    assert rows == []
