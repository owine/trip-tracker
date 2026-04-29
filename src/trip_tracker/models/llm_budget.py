"""LlmBudget — daily Haiku spend tracking for the soft budget cap."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import TIMESTAMP, Date, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

from trip_tracker.models.base import Base


class LlmBudget(Base):
    __tablename__ = "llm_budget"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    cost_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
