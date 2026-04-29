"""LlmBudget ORM model + Postgres CHECK constraints."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.llm_budget import LlmBudget


@pytest.mark.asyncio
async def test_insert_and_read_back(db_session: AsyncSession) -> None:
    row = LlmBudget(day=date(2026, 4, 30), cost_cents=42, request_count=5)
    db_session.add(row)
    await db_session.commit()

    fetched = (
        await db_session.execute(select(LlmBudget).where(LlmBudget.day == date(2026, 4, 30)))
    ).scalar_one()
    assert fetched.cost_cents == 42
    assert fetched.request_count == 5
    assert fetched.updated_at is not None


@pytest.mark.asyncio
async def test_default_values(db_session: AsyncSession) -> None:
    row = LlmBudget(day=date(2026, 5, 1))
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    assert row.cost_cents == 0
    assert row.request_count == 0
