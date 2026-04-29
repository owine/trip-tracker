"""Segments routes: per-type creation/edit/delete. Spec §6."""

from __future__ import annotations

import json
import uuid
import zoneinfo
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.deps import require_user
from trip_tracker.db import get_session
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User
from trip_tracker.schemas.segment_forms import (
    ActivitySegmentForm,
    CarSegmentForm,
    FlightSegmentForm,
    LodgingSegmentForm,
    TrainSegmentForm,
    TransferSegmentForm,
    TripSelector,
    _SegmentBase,
)

router = APIRouter(tags=["segments"])
_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Pre-load IANA tz list once.
_TZ_FIXTURE = Path(__file__).parent.parent / "static" / "iana_timezones.json"
TIMEZONES: list[str] = json.loads(_TZ_FIXTURE.read_text())

FORM_BY_TYPE: dict[str, type[_SegmentBase]] = {
    "flight": FlightSegmentForm,
    "lodging": LodgingSegmentForm,
    "car": CarSegmentForm,
    "train": TrainSegmentForm,
    "transfer": TransferSegmentForm,
    "activity": ActivitySegmentForm,
}

DESTINATION_FROM_END = {"flight", "train", "transfer"}


def _to_utc(local: datetime, tz: str) -> datetime:
    return local.replace(tzinfo=zoneinfo.ZoneInfo(tz)).astimezone(zoneinfo.ZoneInfo("UTC"))


def _user_trips(
    db: AsyncSession,  # noqa: ARG001
    user_id: uuid.UUID,
) -> Any:
    return (
        select(Trip)
        .join(TripTraveler, TripTraveler.trip_id == Trip.id)
        .where(TripTraveler.user_id == user_id)
        .order_by(Trip.start_date.desc())
    )


@router.get("/segments/new", response_class=HTMLResponse)
async def new_segment(
    request: Request,
    type: str | None = Query(default=None),
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    if type is None:
        return templates.TemplateResponse(request, "segments/type_picker.html", {"user": user})
    if type not in FORM_BY_TYPE:
        raise HTTPException(400, detail="unknown segment type")
    trips = (await db.execute(_user_trips(db, user.id))).scalars().all()
    return templates.TemplateResponse(
        request,
        f"segments/{type}_form.html",
        {
            "user": user,
            "trips": trips,
            "timezones": TIMEZONES,
            "values": {},
            "errors": {},
            "type": type,
        },
    )


@router.post("/segments", response_model=None)
async def create_segment(
    request: Request,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> RedirectResponse | HTMLResponse:
    form_data = await request.form()
    seg_type = form_data.get("type")
    if not isinstance(seg_type, str) or seg_type not in FORM_BY_TYPE:
        raise HTTPException(400, detail="unknown segment type")
    form_cls = FORM_BY_TYPE[seg_type]

    raw: dict[str, Any] = dict(form_data)
    raw["trip_selector"] = TripSelector(
        existing_trip_id=raw.pop("trip_selector_existing_trip_id", None) or None,
        new_trip_title=raw.pop("trip_selector_new_trip_title", None) or None,
    ).model_dump()

    try:
        form = form_cls.model_validate(raw)
    except ValidationError as e:
        trips = (await db.execute(_user_trips(db, user.id))).scalars().all()
        return templates.TemplateResponse(
            request,
            f"segments/{seg_type}_form.html",
            {
                "user": user,
                "trips": trips,
                "timezones": TIMEZONES,
                "values": raw,
                "errors": {"_form": str(e)},
                "type": seg_type,
            },
            status_code=200,
        )

    # Convert datetimes to UTC.
    start_at = _to_utc(form.start_local, form.start_tz)
    end_at = _to_utc(form.end_local, form.end_tz) if form.end_local and form.end_tz else None

    # Build location jsonb per type.
    start_loc, end_loc, details = _shape_payload(form)

    if form.trip_selector.existing_trip_id is not None:
        trip = (
            await db.execute(
                select(Trip)
                .join(TripTraveler, TripTraveler.trip_id == Trip.id)
                .where(
                    Trip.id == form.trip_selector.existing_trip_id,
                    TripTraveler.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if trip is None:
            raise HTTPException(404)
        seg_start_date: date = start_at.date()
        seg_end_date: date = (end_at or start_at).date()
        new_start = min(trip.start_date, seg_start_date)
        new_end = max(trip.end_date, seg_end_date)
        if (new_start, new_end) != (trip.start_date, trip.end_date):
            trip.start_date = new_start
            trip.end_date = new_end
    else:
        assert form.trip_selector.new_trip_title  # validated by Pydantic
        seg_end_date = (end_at or start_at).date()
        primary = _derive_destination(seg_type, start_loc, end_loc)
        trip = Trip(
            title=form.trip_selector.new_trip_title.strip(),
            start_date=start_at.date(),
            end_date=seg_end_date,
            primary_destination=primary,
            created_by=user.id,
        )
        db.add(trip)
        await db.flush()
        db.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))

    seg = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type=seg_type,
        status=form.status,
        confirmation_number=form.confirmation_number,
        provider=form.provider,
        start_at=start_at,
        start_tz=form.start_tz,
        end_at=end_at,
        end_tz=form.end_tz,
        start_location=start_loc,
        end_location=end_loc,
        details=details,
        parse_source="manual",
        parse_confidence=1.0,
    )
    db.add(seg)
    await db.commit()

    return RedirectResponse(f"/trips/{trip.id}", status_code=303)


def _shape_payload(
    form: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any]]:
    """Return (start_location, end_location, details) jsonb dicts per type."""
    details: dict[str, Any] = {}
    if form.notes:
        details["notes"] = form.notes
    t = form.type

    if t == "flight":
        start = _drop_none(iata=form.origin_iata, city=form.origin_city)
        end = _drop_none(iata=form.destination_iata, city=form.destination_city)
        if form.flight_number:
            details["flight_number"] = form.flight_number
        if form.seat:
            details["seat"] = form.seat
        return start or None, end or None, details

    if t == "lodging":
        loc = _drop_none(
            name=form.hotel_name, address=form.address, city=form.city, country=form.country
        )
        if form.room_type:
            details["room_type"] = form.room_type
        return loc or None, loc or None, details

    if t == "car":
        start = _drop_none(name=form.pickup_location, city=form.pickup_city)
        end = _drop_none(name=form.dropoff_location, city=form.dropoff_city)
        if form.car_class:
            details["car_class"] = form.car_class
        return start or None, end or None, details

    if t == "train":
        start = _drop_none(name=form.origin_station)
        end = _drop_none(name=form.destination_station)
        if form.train_number:
            details["train_number"] = form.train_number
        if form.seat:
            details["seat"] = form.seat
        return start or None, end or None, details

    if t == "transfer":
        return (
            {"name": form.pickup_location},
            {"name": form.dropoff_location},
            details,
        )

    if t == "activity":
        loc = _drop_none(name=form.venue_name, address=form.address, city=form.city)
        return loc or None, None, details

    raise AssertionError(f"unhandled type: {t}")


def _drop_none(**kw: Any) -> dict[str, Any]:
    return {k: v for k, v in kw.items() if v}


def _derive_destination(
    seg_type: str,
    start_loc: dict[str, Any] | None,
    end_loc: dict[str, Any] | None,
) -> str | None:
    primary_side = end_loc if seg_type in DESTINATION_FROM_END else start_loc
    fallback_side = start_loc if seg_type in DESTINATION_FROM_END else end_loc
    return (primary_side or {}).get("city") or (fallback_side or {}).get("city") or None
