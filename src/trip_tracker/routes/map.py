"""Map routes: lifetime atlas (/map) and per-trip view (/trips/<id>/map).

Task 4 implements the lifetime route only; per-trip lands in Task 5.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.deps import require_user
from trip_tracker.db import get_session
from trip_tracker.geo.arcs import great_circle_points
from trip_tracker.geo.resolve import resolve_point
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User

router = APIRouter(tags=["map"])

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# Color palette for trip-color cycling (lifetime view)
_PALETTE = [
    "#3b82f6",
    "#10b981",
    "#f59e0b",
    "#ef4444",
    "#8b5cf6",
    "#ec4899",
    "#14b8a6",
    "#f97316",
]


@router.get("/map", response_class=HTMLResponse)
async def map_lifetime(
    request: Request,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    """Lifetime atlas: every trip the user is a traveler on, colored by trip."""
    rows = (
        await db.execute(
            select(Segment, Trip)
            .join(Trip, Trip.id == Segment.trip_id)
            .join(TripTraveler, TripTraveler.trip_id == Segment.trip_id)
            .where(TripTraveler.user_id == user.id)
            .order_by(Trip.start_date, Segment.start_at)
        )
    ).all()

    trips_seen: dict[uuid.UUID, int] = {}  # trip_id → palette index
    markers: list[dict[str, Any]] = []
    arcs: list[dict[str, Any]] = []

    for seg, trip in rows:
        if trip.id not in trips_seen:
            trips_seen[trip.id] = len(trips_seen) % len(_PALETTE)
        color = _PALETTE[trips_seen[trip.id]]

        start_pt = resolve_point(seg.start_location)
        end_pt = resolve_point(seg.end_location)

        for pt in (start_pt, end_pt):
            if pt is None:
                continue
            markers.append(
                {
                    "lat": pt[0],
                    "lon": pt[1],
                    "trip_id": str(trip.id),
                    "trip_title": trip.title,
                    "color": color,
                }
            )

        if seg.type == "flight" and start_pt and end_pt:
            arcs.append(
                {
                    "points": great_circle_points(start_pt, end_pt, n_points=50),
                    "color": color,
                    "trip_id": str(trip.id),
                }
            )

    payload = json.dumps({"markers": markers, "arcs": arcs})
    return templates.TemplateResponse(
        request,
        "map/all_trips.html",
        {"user": user, "map_data_json": payload},
    )
