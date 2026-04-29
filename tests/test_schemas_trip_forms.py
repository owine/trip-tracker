"""Pydantic trip form schemas."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from trip_tracker.schemas.trip_forms import TripForm


def test_trip_form_same_date() -> None:
    """Single-day trip: end_date == start_date is valid."""
    form = TripForm(
        title="Day Trip",
        start_date=date(2026, 6, 15),
        end_date=date(2026, 6, 15),
    )
    assert form.start_date == form.end_date


def test_trip_form_end_after_start() -> None:
    """Multi-day trip: end_date > start_date is valid."""
    form = TripForm(
        title="Week-long vacation",
        start_date=date(2026, 6, 15),
        end_date=date(2026, 6, 22),
    )
    assert form.end_date > form.start_date


def test_trip_form_end_before_start_raises() -> None:
    """Invalid: end_date < start_date raises ValidationError."""
    with pytest.raises(ValidationError):
        TripForm(
            title="Invalid trip",
            start_date=date(2026, 6, 22),
            end_date=date(2026, 6, 15),
        )
