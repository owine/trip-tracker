"""Segments routes: per-type creation/edit/delete. Spec §6."""

from __future__ import annotations

import json
import uuid
import zoneinfo
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from trip_tracker.auth.deps import require_user
from trip_tracker.db import get_session
from trip_tracker.expenses.awards import AwardDetails
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
from trip_tracker.search.sync import enqueue_meili_sync

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


def _apply_award_from_form(seg: Segment, form: dict[str, str]) -> dict[str, str] | None:
    """Mutate seg.details based on award fields in form. Returns errors dict or None."""
    if form.get("clear_award") == "1" and (seg.details or {}).get("award"):
        seg.details = {k: v for k, v in (seg.details or {}).items() if k != "award"}
        flag_modified(seg, "details")
        return None
    if not form.get("award_points_spent"):
        return None  # award fields blank → no-op (don't touch existing if present)
    try:
        award = AwardDetails(
            program=form.get("award_program", ""),
            points_spent=int(form["award_points_spent"]),
            cash_copay_minor=int(form.get("award_cash_copay_minor") or 0),
            cash_copay_currency=form.get("award_cash_copay_currency", "USD"),
            cash_equivalent_minor=int(form["award_cash_equivalent_minor"])
            if form.get("award_cash_equivalent_minor")
            else None,
            cash_equivalent_currency=form.get("award_cash_equivalent_currency") or None,
        )
    except (ValidationError, ValueError) as exc:
        return {"award": str(exc)}
    details = dict(seg.details or {})
    details["award"] = award.model_dump(exclude_none=True)
    seg.details = details
    flag_modified(seg, "details")
    return None


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
            "existing_award": None,
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
        assert form.trip_selector.new_trip_title, "validated by Pydantic"  # nosec B101
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

    # Apply award metadata for flight + lodging only (Spec §4.6).
    if seg_type in ("flight", "lodging"):
        award_errors = _apply_award_from_form(seg, raw)
        if award_errors:
            trips = (await db.execute(_user_trips(db, user.id))).scalars().all()
            return templates.TemplateResponse(
                request,
                f"segments/{seg_type}_form.html",
                {
                    "user": user,
                    "trips": trips,
                    "timezones": TIMEZONES,
                    "values": raw,
                    "errors": award_errors,
                    "type": seg_type,
                    "existing_award": (seg.details or {}).get("award"),
                },
                status_code=200,
            )

    await db.commit()
    await enqueue_meili_sync(request.app.state.settings, entity="trip", entity_id=trip.id)
    await enqueue_meili_sync(request.app.state.settings, entity="segment", entity_id=seg.id)

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


@router.get("/trips/{trip_id}/segments/{segment_id}/edit", response_class=HTMLResponse)
async def edit_segment_form(
    request: Request,
    trip_id: uuid.UUID,
    segment_id: uuid.UUID,
    from_raw_email: uuid.UUID | None = Query(default=None),  # noqa: B008
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    seg = await _load_segment_for_user(db, trip_id, segment_id, user.id)
    trips = (await db.execute(_user_trips(db, user.id))).scalars().all()
    ai_suggested = seg.parse_source != "manual"
    return templates.TemplateResponse(
        request,
        f"segments/{seg.type}_form.html",
        {
            "user": user,
            "trips": trips,
            "timezones": TIMEZONES,
            "values": _segment_to_form_values(seg),
            "errors": {},
            "type": seg.type,
            "edit_segment_id": str(seg.id),
            "ai_suggested": ai_suggested,
            "from_raw_email": from_raw_email,
            "existing_award": (seg.details or {}).get("award"),
        },
    )


@router.post("/trips/{trip_id}/segments/{segment_id}", response_model=None)
async def update_segment(
    request: Request,
    trip_id: uuid.UUID,
    segment_id: uuid.UUID,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Update an existing segment in place.

    Phase 2 scope: type and trip CANNOT change. The form re-uses the per-type
    template, so the `type` field is hidden and immutable; the `trip_selector`
    is forced to the current trip (we ignore any new-trip submission). This
    keeps the diff small — moving a segment between trips can land later.

    Auto-widening of trip dates DOES re-run on update (the new datetime may
    extend or shrink the range).
    """
    seg = await _load_segment_for_user(db, trip_id, segment_id, user.id)
    # Re-load the trip so we can widen its dates after recomputing.
    trip = (await db.execute(select(Trip).where(Trip.id == trip_id))).scalar_one()

    form_data = await request.form()
    seg_type = form_data.get("type")
    if not isinstance(seg_type, str) or seg_type != seg.type:
        # Defensive: form posts the hidden `type` field; mismatch is tampering.
        raise HTTPException(400, detail="segment type immutable")
    form_cls = FORM_BY_TYPE[seg_type]

    raw: dict[str, Any] = dict(form_data)
    # Force the trip selector to the current trip — edits never re-route trips.
    raw["trip_selector"] = TripSelector(existing_trip_id=trip.id, new_trip_title=None).model_dump()
    raw.pop("trip_selector_existing_trip_id", None)
    raw.pop("trip_selector_new_trip_title", None)

    try:
        form = form_cls.model_validate(raw)
    except ValidationError as e:
        return templates.TemplateResponse(
            request,
            f"segments/{seg_type}_form.html",
            {
                "user": user,
                "trips": [trip],
                "timezones": TIMEZONES,
                "values": raw,
                "errors": {"_form": str(e)},
                "type": seg_type,
                "edit_segment_id": str(seg.id),
                "existing_award": (seg.details or {}).get("award"),
            },
            status_code=200,
        )

    start_at = _to_utc(form.start_local, form.start_tz)
    end_at = _to_utc(form.end_local, form.end_tz) if form.end_local and form.end_tz else None
    start_loc, end_loc, details = _shape_payload(form)

    seg.status = form.status
    seg.confirmation_number = form.confirmation_number
    seg.provider = form.provider
    seg.start_at = start_at
    seg.start_tz = form.start_tz
    seg.end_at = end_at
    seg.end_tz = form.end_tz
    seg.start_location = start_loc
    seg.end_location = end_loc
    seg.details = details

    # Re-widen trip dates against the new segment timing.
    seg_start_date: date = start_at.date()
    seg_end_date: date = (end_at or start_at).date()
    new_start = min(trip.start_date, seg_start_date)
    new_end = max(trip.end_date, seg_end_date)
    if (new_start, new_end) != (trip.start_date, trip.end_date):
        trip.start_date = new_start
        trip.end_date = new_end

    # Apply award metadata for flight + lodging only (Spec §4.6).
    if seg_type in ("flight", "lodging"):
        award_errors = _apply_award_from_form(seg, raw)
        if award_errors:
            return templates.TemplateResponse(
                request,
                f"segments/{seg_type}_form.html",
                {
                    "user": user,
                    "trips": [trip],
                    "timezones": TIMEZONES,
                    "values": raw,
                    "errors": award_errors,
                    "type": seg_type,
                    "edit_segment_id": str(seg.id),
                    "existing_award": (seg.details or {}).get("award"),
                },
                status_code=200,
            )

    await db.commit()
    await enqueue_meili_sync(request.app.state.settings, entity="trip", entity_id=trip.id)
    await enqueue_meili_sync(request.app.state.settings, entity="segment", entity_id=seg.id)
    return RedirectResponse(f"/trips/{trip_id}", status_code=303)


@router.post("/trips/{trip_id}/segments/{segment_id}/delete", response_model=None)
async def delete_segment(
    request: Request,
    trip_id: uuid.UUID,
    segment_id: uuid.UUID,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> RedirectResponse:
    seg = await _load_segment_for_user(db, trip_id, segment_id, user.id)
    await db.delete(seg)
    await db.commit()
    await enqueue_meili_sync(request.app.state.settings, entity="segment", entity_id=seg.id)
    return RedirectResponse(f"/trips/{trip_id}", status_code=303)


async def _load_segment_for_user(
    db: AsyncSession, trip_id: uuid.UUID, segment_id: uuid.UUID, user_id: uuid.UUID
) -> Segment:
    stmt = (
        select(Segment)
        .join(Trip, Trip.id == Segment.trip_id)
        .join(TripTraveler, TripTraveler.trip_id == Trip.id)
        .where(
            Trip.id == trip_id,
            Segment.id == segment_id,
            TripTraveler.user_id == user_id,
        )
    )
    seg = (await db.execute(stmt)).scalar_one_or_none()
    if seg is None:
        raise HTTPException(404)
    return seg


def _segment_to_form_values(seg: Segment) -> dict[str, Any]:
    """Flatten a Segment row into the dict shape templates expect."""
    sl = seg.start_location or {}
    el = seg.end_location or {}
    d = seg.details or {}
    base: dict[str, Any] = {
        "trip_selector_existing_trip_id": str(seg.trip_id),
        "status": seg.status,
        "provider": seg.provider or "",
        "confirmation_number": seg.confirmation_number or "",
        "start_local": seg.start_at.astimezone(zoneinfo.ZoneInfo(seg.start_tz)).strftime(
            "%Y-%m-%dT%H:%M"
        ),
        "start_tz": seg.start_tz,
        "end_local": (
            seg.end_at.astimezone(zoneinfo.ZoneInfo(seg.end_tz)).strftime("%Y-%m-%dT%H:%M")
            if seg.end_at and seg.end_tz
            else ""
        ),
        "end_tz": seg.end_tz or "",
        "notes": d.get("notes", ""),
    }
    if seg.type == "flight":
        base.update(
            flight_number=d.get("flight_number", ""),
            seat=d.get("seat", ""),
            origin_iata=sl.get("iata", ""),
            origin_city=sl.get("city", ""),
            destination_iata=el.get("iata", ""),
            destination_city=el.get("city", ""),
        )
    elif seg.type == "lodging":
        base.update(
            hotel_name=sl.get("name", ""),
            address=sl.get("address", ""),
            city=sl.get("city", ""),
            country=sl.get("country", ""),
            room_type=d.get("room_type", ""),
        )
    elif seg.type == "car":
        base.update(
            pickup_location=sl.get("name", ""),
            pickup_city=sl.get("city", ""),
            dropoff_location=el.get("name", ""),
            dropoff_city=el.get("city", ""),
            car_class=d.get("car_class", ""),
        )
    elif seg.type == "train":
        base.update(
            origin_station=sl.get("name", ""),
            destination_station=el.get("name", ""),
            train_number=d.get("train_number", ""),
            seat=d.get("seat", ""),
        )
    elif seg.type == "transfer":
        base.update(
            pickup_location=sl.get("name", ""),
            dropoff_location=el.get("name", ""),
        )
    elif seg.type == "activity":
        base.update(
            venue_name=sl.get("name", ""),
            address=sl.get("address", ""),
            city=sl.get("city", ""),
        )
    return base
