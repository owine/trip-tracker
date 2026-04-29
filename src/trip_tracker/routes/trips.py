"""Trips routes: list, detail, edit, delete. Spec §6."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pydantic
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.deps import require_traveler, require_user
from trip_tracker.db import get_session
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User
from trip_tracker.schemas.trip_forms import TripForm

router = APIRouter(prefix="/trips", tags=["trips"])

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


@router.get("", response_class=HTMLResponse)
async def list_trips(
    request: Request,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    today = date.today()
    is_past = case((Trip.end_date < today, 1), else_=0)
    stmt = (
        select(Trip)
        .join(TripTraveler, TripTraveler.trip_id == Trip.id)
        .where(TripTraveler.user_id == user.id)
        .order_by(
            is_past.asc(),
            case((Trip.end_date >= today, Trip.start_date), else_=None).asc(),
            case((Trip.end_date < today, Trip.start_date), else_=None).desc(),
        )
    )
    trips = (await db.execute(stmt)).scalars().all()
    return templates.TemplateResponse(request, "trips/list.html", {"trips": trips, "user": user})


@router.get("/{trip_id}", response_class=HTMLResponse)
async def trip_detail(
    request: Request,
    trip: Trip = Depends(require_traveler),  # noqa: B008
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    from trip_tracker.models.segment import Segment

    segments = (
        (
            await db.execute(
                select(Segment).where(Segment.trip_id == trip.id).order_by(Segment.start_at)
            )
        )
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        request,
        "trips/detail.html",
        {"trip": trip, "segments": segments, "user": user},
    )


@router.get("/{trip_id}/edit", response_class=HTMLResponse)
async def edit_trip_form(
    request: Request,
    trip: Trip = Depends(require_traveler),  # noqa: B008
    user: User = Depends(require_user),  # noqa: B008
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "trips/edit.html", {"trip": trip, "user": user, "errors": {}}
    )


@router.post("/{trip_id}", response_model=None)
async def update_trip(
    request: Request,
    trip: Trip = Depends(require_traveler),  # noqa: B008
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
    title: str = Form(...),
    start_date: date = Form(...),  # noqa: B008
    end_date: date = Form(...),  # noqa: B008
    primary_destination: str | None = Form(None),
    notes: str | None = Form(None),
    cover_color: str | None = Form(None),
) -> RedirectResponse | HTMLResponse:
    try:
        form = TripForm(
            title=title,
            start_date=start_date,
            end_date=end_date,
            primary_destination=primary_destination,
            notes=notes,
            cover_color=cover_color,
        )
    except pydantic.ValidationError as e:
        return templates.TemplateResponse(
            request,
            "trips/edit.html",
            {"trip": trip, "user": user, "errors": {"_form": str(e)}},
        )
    trip.title = form.title
    trip.start_date = form.start_date
    trip.end_date = form.end_date
    trip.primary_destination = form.primary_destination
    trip.notes = form.notes
    trip.cover_color = form.cover_color
    await db.commit()
    return RedirectResponse(f"/trips/{trip.id}", status_code=303)


@router.post("/{trip_id}/delete", response_model=None)
async def delete_trip(
    trip: Trip = Depends(require_traveler),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> RedirectResponse:
    await db.delete(trip)
    await db.commit()
    return RedirectResponse("/trips", status_code=303)
