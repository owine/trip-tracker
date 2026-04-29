"""LlmBudget read/write helpers + Haiku 4.5 cost calculation."""

from __future__ import annotations

import math
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.llm_budget import LlmBudget

# Haiku 4.5 pricing (USD per Mtok), per Anthropic docs.
_HAIKU_INPUT_USD_PER_MTOK = 1.0
_HAIKU_OUTPUT_USD_PER_MTOK = 5.0


def cost_cents_for_usage(*, input_tokens: int, output_tokens: int) -> int:
    """Round-up cents cost for a Haiku call.

    Returns 0 only when both counts are 0; any nonzero usage rounds up to ≥1 cent.
    """
    if input_tokens == 0 and output_tokens == 0:
        return 0
    usd = (
        input_tokens * _HAIKU_INPUT_USD_PER_MTOK / 1_000_000
        + output_tokens * _HAIKU_OUTPUT_USD_PER_MTOK / 1_000_000
    )
    return max(1, math.ceil(usd * 100))


async def is_over_budget(db: AsyncSession, *, cap_cents: int) -> bool:
    today = datetime.now(tz=UTC).date()
    row = (
        await db.execute(select(LlmBudget.cost_cents).where(LlmBudget.day == today))
    ).scalar_one_or_none()
    return (row or 0) >= cap_cents


async def record_usage(db: AsyncSession, *, cost_cents: int) -> None:
    """Upsert today's row: cost_cents += delta, request_count += 1."""
    today = datetime.now(tz=UTC).date()
    stmt = (
        pg_insert(LlmBudget)
        .values(day=today, cost_cents=cost_cents, request_count=1)
        .on_conflict_do_update(
            index_elements=["day"],
            set_={
                "cost_cents": LlmBudget.cost_cents + cost_cents,
                "request_count": LlmBudget.request_count + 1,
            },
        )
    )
    await db.execute(stmt)
    await db.commit()
