"""Daily LLM budget tracker — read/write LlmBudget."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.llm_budget import LlmBudget
from trip_tracker.parsers.budget import (
    cost_cents_for_usage,
    is_over_budget,
    record_usage,
)


def test_cost_cents_for_usage_haiku_pricing() -> None:
    """Haiku 4.5 pricing: $1/Mtok input, $5/Mtok output."""
    # 1000 input + 1000 output tokens → 0.0001 + 0.0005 USD = 0.06 cents → ceil to 1 cent.
    cents = cost_cents_for_usage(input_tokens=1000, output_tokens=1000)
    assert cents == 1


def test_cost_cents_zero_for_zero_usage() -> None:
    assert cost_cents_for_usage(input_tokens=0, output_tokens=0) == 0


@pytest.mark.asyncio
async def test_is_over_budget_initially_false(db_session: AsyncSession) -> None:
    over = await is_over_budget(db_session, cap_cents=100)
    assert over is False


@pytest.mark.asyncio
async def test_is_over_budget_at_or_above_cap(db_session: AsyncSession) -> None:
    today = datetime.now(tz=UTC).date()
    db_session.add(LlmBudget(day=today, cost_cents=150, request_count=3))
    await db_session.commit()
    assert await is_over_budget(db_session, cap_cents=100) is True


@pytest.mark.asyncio
async def test_record_usage_upserts(db_session: AsyncSession) -> None:
    today = datetime.now(tz=UTC).date()
    await record_usage(db_session, cost_cents=5)
    await record_usage(db_session, cost_cents=7)
    row = (await db_session.execute(select(LlmBudget).where(LlmBudget.day == today))).scalar_one()
    assert row.cost_cents == 12
    assert row.request_count == 2
