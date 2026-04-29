"""Pydantic forms for trip CRUD."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, model_validator


class TripForm(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    start_date: date
    end_date: date
    primary_destination: str | None = Field(default=None, max_length=255)
    notes: str | None = None
    cover_color: str | None = Field(default=None, max_length=16)

    @model_validator(mode="after")
    def _date_range(self) -> TripForm:
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self
