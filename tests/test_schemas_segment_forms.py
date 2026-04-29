"""Pydantic segment form schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from pydantic import ValidationError

from trip_tracker.schemas.segment_forms import (
    FlightSegmentForm,
    LodgingSegmentForm,
    TripSelector,
)


def test_flight_form_minimal() -> None:
    f = FlightSegmentForm(
        trip_selector=TripSelector(new_trip_title="Trip"),
        start_local=datetime(2026, 6, 1, 9, 0),
        start_tz="America/New_York",
    )
    assert f.type == "flight"


def test_unknown_tz_rejected() -> None:
    with pytest.raises(ValidationError):
        FlightSegmentForm(
            trip_selector=TripSelector(new_trip_title="T"),
            start_local=datetime(2026, 6, 1),
            start_tz="Mars/Olympus",
        )


def test_trip_selector_requires_one() -> None:
    with pytest.raises(ValidationError):
        TripSelector()  # neither
    with pytest.raises(ValidationError):
        TripSelector(existing_trip_id=uuid.uuid4(), new_trip_title="X")  # both


def test_lodging_requires_hotel_name() -> None:
    with pytest.raises(ValidationError):
        LodgingSegmentForm(
            trip_selector=TripSelector(new_trip_title="T"),
            start_local=datetime(2026, 6, 1),
            start_tz="UTC",
            hotel_name="",
        )
