"""Pydantic forms for per-type segment creation/edit. Spec §6."""

from __future__ import annotations

import uuid
import zoneinfo
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

SegmentType = Literal["flight", "lodging", "car", "train", "transfer", "activity"]
SegmentStatus = Literal["confirmed", "cancelled", "tentative"]


def _validate_iana(tz: str) -> str:
    if tz not in zoneinfo.available_timezones():
        raise ValueError(f"unknown IANA timezone: {tz!r}")
    return tz


# Trip selection: either an existing trip ID OR a new-trip title.
class TripSelector(BaseModel):
    existing_trip_id: uuid.UUID | None = None
    new_trip_title: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def _exactly_one(self) -> TripSelector:
        has_existing = self.existing_trip_id is not None
        has_new = bool(self.new_trip_title and self.new_trip_title.strip())
        if has_existing == has_new:
            raise ValueError("provide exactly one of existing_trip_id or new_trip_title")
        return self


class _SegmentBase(BaseModel):
    """Common fields for all segment types."""

    trip_selector: TripSelector
    status: SegmentStatus = "confirmed"
    confirmation_number: str | None = Field(default=None, max_length=64)
    provider: str | None = Field(default=None, max_length=128)
    start_local: datetime
    start_tz: str
    end_local: datetime | None = None
    end_tz: str | None = None
    notes: str | None = None

    @field_validator("start_tz")
    @classmethod
    def _validate_start_tz(cls, v: str) -> str:
        return _validate_iana(v)

    @field_validator("end_tz")
    @classmethod
    def _validate_end_tz(cls, v: str | None) -> str | None:
        return None if v is None else _validate_iana(v)


class FlightSegmentForm(_SegmentBase):
    type: Literal["flight"] = "flight"
    flight_number: str | None = Field(default=None, max_length=16)
    origin_iata: str | None = Field(default=None, min_length=3, max_length=4)
    origin_city: str | None = Field(default=None, max_length=128)
    destination_iata: str | None = Field(default=None, min_length=3, max_length=4)
    destination_city: str | None = Field(default=None, max_length=128)
    seat: str | None = Field(default=None, max_length=16)


class LodgingSegmentForm(_SegmentBase):
    type: Literal["lodging"] = "lodging"
    hotel_name: str = Field(min_length=1, max_length=255)
    address: str | None = Field(default=None, max_length=512)
    city: str | None = Field(default=None, max_length=128)
    country: str | None = Field(default=None, max_length=128)
    room_type: str | None = Field(default=None, max_length=64)


class CarSegmentForm(_SegmentBase):
    type: Literal["car"] = "car"
    pickup_location: str = Field(min_length=1, max_length=255)
    pickup_city: str | None = Field(default=None, max_length=128)
    dropoff_location: str = Field(min_length=1, max_length=255)
    dropoff_city: str | None = Field(default=None, max_length=128)
    car_class: str | None = Field(default=None, max_length=64)


class TrainSegmentForm(_SegmentBase):
    type: Literal["train"] = "train"
    train_number: str | None = Field(default=None, max_length=32)
    origin_station: str = Field(min_length=1, max_length=255)
    destination_station: str = Field(min_length=1, max_length=255)
    seat: str | None = Field(default=None, max_length=32)


class TransferSegmentForm(_SegmentBase):
    type: Literal["transfer"] = "transfer"
    pickup_location: str = Field(min_length=1, max_length=255)
    dropoff_location: str = Field(min_length=1, max_length=255)


class ActivitySegmentForm(_SegmentBase):
    type: Literal["activity"] = "activity"
    venue_name: str = Field(min_length=1, max_length=255)
    address: str | None = Field(default=None, max_length=512)
    city: str | None = Field(default=None, max_length=128)


SegmentForm = Annotated[
    FlightSegmentForm
    | LodgingSegmentForm
    | CarSegmentForm
    | TrainSegmentForm
    | TransferSegmentForm
    | ActivitySegmentForm,
    Field(discriminator="type"),
]
