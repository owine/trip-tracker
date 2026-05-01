"""ExpenseForm Pydantic v2 model — POST validation."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field, field_validator

from trip_tracker.expenses.categories import Category


class ExpenseForm(BaseModel):
    amount_minor: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    category: Category
    notes: str | None = None
    incurred_on: date
    status: str = Field(default="paid", pattern="^(paid|pending)$")
    segment_id: str | None = None
    document_id: str | None = None
    deposit_minor: int | None = Field(default=None, ge=0)
    cancellation_deadline: date | None = None
    cancellation_fee_minor: int | None = Field(default=None, ge=0)
    home_currency_at_load: str = Field(min_length=3, max_length=3)

    @field_validator("currency", "home_currency_at_load")
    @classmethod
    def upper(cls, v: str) -> str:
        return v.upper()

    @field_validator(
        "notes",
        "segment_id",
        "document_id",
        "deposit_minor",
        "cancellation_deadline",
        "cancellation_fee_minor",
        mode="before",
    )
    @classmethod
    def empty_string_to_none(cls, v: Any) -> Any:
        # Browsers submit unfilled optional fields as `name=` (empty string).
        # Coerce to None so int/date/UUID validators don't choke on "".
        if isinstance(v, str) and v == "":
            return None
        return v
