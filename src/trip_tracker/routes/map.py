"""Map routes: lifetime atlas (/map) and per-trip view (/trips/<id>/map).

Task 4 implements the lifetime route only; per-trip lands in Task 5.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from redis.asyncio import Redis as AsyncRedis
from saq import Queue as SaqQueue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.deps import get_settings, require_user
from trip_tracker.config import Settings
from trip_tracker.db import get_session
from trip_tracker.geo.arcs import great_circle_points
from trip_tracker.geo.resolve import resolve_point
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User
from trip_tracker.parsers.enrich import get_airport, haversine_km
from trip_tracker.templating import register_globals
from trip_tracker.weather.cache import get_cached
from trip_tracker.weather.client import Forecast

router = APIRouter(tags=["map"])

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
register_globals(templates)


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
            .where(TripTraveler.user_id == user.id, Trip.merged_into_id.is_(None))
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


# === Per-trip view constants ===
_GROUND_GATE_KM = 500.0
_WEATHER_FUTURE_DAYS = 14


async def _enqueue_weather_refresh(queue: SaqQueue, lat: float, lon: float) -> None:
    """Fire-and-forget saq enqueue for refresh_weather.

    `key=...` lets future job-status lookups find this enqueue; saq 0.26
    does NOT use `key` for in-flight dedup. We do NOT pass `unique=True`
    — saq treats only `Job.__dataclass_fields__` keys as job metadata, and
    `unique` is not one; it would fall through as a function kwarg and
    cause TypeError. Concurrent map renders may double-fetch the same
    destination — acceptable; Open-Meteo is keyless and we cap with the
    1h Redis TTL.
    """
    await queue.enqueue(
        "refresh_weather",
        lat=lat,
        lon=lon,
        key=f"weather:{lat:.2f}:{lon:.2f}",
    )


@router.get("/trips/{trip_id}/map", response_class=HTMLResponse)
async def map_per_trip(
    trip_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> HTMLResponse:
    """Per-trip map view: numbered markers + flight arcs + weather popups."""
    is_traveler = (
        await db.execute(
            select(TripTraveler.user_id).where(
                TripTraveler.trip_id == trip_id, TripTraveler.user_id == user.id
            )
        )
    ).scalar_one_or_none() is not None
    if not is_traveler:
        raise HTTPException(status_code=404, detail="Not found")

    trip = (
        await db.execute(select(Trip).where(Trip.id == trip_id, Trip.merged_into_id.is_(None)))
    ).scalar_one_or_none()
    if trip is None:
        raise HTTPException(status_code=404, detail="Not found")
    segments = (
        (
            await db.execute(
                select(Segment).where(Segment.trip_id == trip_id).order_by(Segment.start_at)
            )
        )
        .scalars()
        .all()
    )

    markers: list[dict[str, Any]] = []
    arcs: list[dict[str, Any]] = []
    ground_polyline_points: list[tuple[float, float]] = []
    last_non_flight_pt: tuple[float, float] | None = None
    last_non_flight_start_at = None

    seq = 0
    for seg in segments:
        s_pt = resolve_point(seg.start_location)
        e_pt = resolve_point(seg.end_location)

        for pt, loc in ((s_pt, seg.start_location), (e_pt, seg.end_location)):
            if pt is None:
                continue
            seq += 1
            # Build a human-readable label: type + provider (if any) + IATA (if any)
            iata = (loc or {}).get("iata") or ""
            base = f"{seg.type} · {seg.provider or ''}".strip(" ·")
            label = f"{base} · {iata}".strip(" ·") if iata else base
            markers.append(
                {
                    "lat": pt[0],
                    "lon": pt[1],
                    "seq": seq,
                    "label": label,
                    "start_at": seg.start_at.isoformat() if seg.start_at else None,
                }
            )

        if seg.type == "flight" and s_pt and e_pt:
            arcs.append(
                {
                    "points": great_circle_points(s_pt, e_pt, n_points=50),
                    "color": "#3b82f6",
                }
            )

        # Ground polyline gate: same-day OR <500 km between consecutive non-flight segs
        if seg.type != "flight" and s_pt:
            if last_non_flight_pt is not None and last_non_flight_start_at is not None:
                same_day = (
                    seg.start_at is not None
                    and last_non_flight_start_at is not None
                    and seg.start_at.date() == last_non_flight_start_at.date()
                )
                close_enough = haversine_km(last_non_flight_pt, s_pt) < _GROUND_GATE_KM
                if same_day or close_enough:
                    ground_polyline_points.append(last_non_flight_pt)
                    ground_polyline_points.append(s_pt)
            last_non_flight_pt = e_pt or s_pt
            last_non_flight_start_at = seg.start_at

    # Weather: only for current/future trips within the 14-day horizon
    today = date.today()
    weather_horizon_max = today + timedelta(days=_WEATHER_FUTURE_DAYS)
    weather_cards: list[dict[str, Any]] = []
    if today <= trip.end_date and trip.start_date <= weather_horizon_max:
        unique_pts: dict[tuple[float, float], str] = {}
        for seg in segments:
            if seg.start_at and seg.start_at.date() < today - timedelta(days=1):
                continue
            for pt, loc in (
                (resolve_point(seg.start_location), seg.start_location),
                (resolve_point(seg.end_location), seg.end_location),
            ):
                if pt is None:
                    continue
                key = (round(pt[0], 2), round(pt[1], 2))
                city = (loc or {}).get("city") if loc else None
                if city is None and loc:
                    iata = loc.get("iata")
                    if iata:
                        ap = get_airport(iata)
                        if ap is not None:
                            city = ap.city
                if key not in unique_pts and city:
                    unique_pts[key] = city

        redis = AsyncRedis.from_url(settings.redis_url)
        try:
            queue = SaqQueue.from_url(settings.redis_url)
            try:
                for (lat, lon), city in unique_pts.items():
                    cached: Forecast | None = await get_cached(lat, lon, redis)  # type: ignore[arg-type]
                    if cached is None:
                        await _enqueue_weather_refresh(queue, lat, lon)
                        weather_cards.append(
                            {"lat": lat, "lon": lon, "city": city, "loading": True}
                        )
                    else:
                        weather_cards.append(
                            {
                                "lat": lat,
                                "lon": lon,
                                "city": city,
                                "loading": False,
                                "days": [d.model_dump(mode="json") for d in cached.days],
                                "timezone": cached.timezone,
                            }
                        )
            finally:
                await queue.disconnect()
        finally:
            await redis.aclose()

    payload = json.dumps(
        {
            "markers": markers,
            "arcs": arcs,
            "ground": ground_polyline_points,
            "weather": weather_cards,
        }
    )
    return templates.TemplateResponse(
        request,
        "map/trip.html",
        {"user": user, "trip": trip, "map_data_json": payload},
    )
