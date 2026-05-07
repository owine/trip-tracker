"""Trip consolidation suggestions — home-anchored with geometric fallback."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import IntEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.geo.cities import lookup_city
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User
from trip_tracker.parsers.base import SegmentDraft  # not schemas.segments
from trip_tracker.parsers.enrich import get_airport, haversine_km
from trip_tracker.trips.home import infer_home


class _Weight(IntEnum):  # consumed in B5
    LOW = 1
    MEDIUM = 2
    HIGH = 3


@dataclass(frozen=True)
class ConsolidationTarget:
    """Normalized view of either an existing Trip or in-flight drafts.

    Both surfaces (trip-detail page + inbox-confirm preview) need the same
    shape: a date range and the set of endpoint cities/IATAs. This adapter
    lets ``consolidation_candidates`` (B5) stay agnostic to which surface
    called it.
    """

    start_date: date
    end_date: date
    start_city: str | None
    end_city: str | None
    endpoint_iatas: frozenset[str]
    trip_id: uuid.UUID | None  # None for drafts (no Trip row yet)

    @classmethod
    def from_trip(
        cls,
        trip: Trip,
        segments: Sequence[Segment],
    ) -> ConsolidationTarget:
        """Build from an existing Trip row + its Segment rows."""
        ordered = sorted(segments, key=lambda s: s.start_at)
        start_city = (ordered[0].start_location or {}).get("city") if ordered else None
        end_city = (ordered[-1].end_location or {}).get("city") if ordered else None
        iatas: set[str] = set()
        for s in ordered:
            for loc in (s.start_location, s.end_location):
                iata = (loc or {}).get("iata")
                if iata:
                    iatas.add(iata)
        return cls(
            start_date=trip.start_date,
            end_date=trip.end_date,
            start_city=start_city,
            end_city=end_city,
            endpoint_iatas=frozenset(iatas),
            trip_id=trip.id,
        )

    @classmethod
    def from_drafts(cls, drafts: Sequence[SegmentDraft]) -> ConsolidationTarget:
        """Build from a list of in-flight SegmentDrafts (no Trip row yet)."""
        ordered = sorted(drafts, key=lambda d: d.start_at)
        start_city = (ordered[0].start_location or {}).get("city") if ordered else None
        end_city = (ordered[-1].end_location or {}).get("city") if ordered else None
        iatas: set[str] = set()
        for d in ordered:
            for loc in (d.start_location, d.end_location):
                iata = (loc or {}).get("iata")
                if iata:
                    iatas.add(iata)
        start_date = ordered[0].start_at.date() if ordered else date.today()
        # max() across all drafts: a later-starting flight may have an earlier
        # end_at than a longer-running lodging draft. Picking ordered[-1] would
        # silently shrink the window and miss consolidation candidates.
        end_date = max((d.end_at or d.start_at) for d in ordered).date() if ordered else start_date
        return cls(
            start_date=start_date,
            end_date=end_date,
            start_city=start_city,
            end_city=end_city,
            endpoint_iatas=frozenset(iatas),
            trip_id=None,
        )


# ---------------------------------------------------------------------------
# B5: ConsolidationCandidate + helpers + consolidation_candidates
# ---------------------------------------------------------------------------

_GAP_DAYS_FALLBACK = 3
_DISTANCE_KM_LOW = 500.0
_TOP_K = 3
_WINDOW_LIMIT = 50


@dataclass(frozen=True)
class ConsolidationCandidate:
    """A trip that looks like it should be merged with the target, with a score."""

    trip: Trip
    weight: _Weight


async def _user_trips_within_window(
    db: AsyncSession,
    user: User,  # noqa: ARG001
    target: ConsolidationTarget,
) -> list[Trip]:
    """Trips that overlap target's date window (±3 days).

    Excludes the target trip itself (when target.trip_id is not None).
    Single-owner mode: no created_by or soft-delete filter needed.
    """
    window = timedelta(days=_GAP_DAYS_FALLBACK)
    stmt = (
        select(Trip)
        .where(
            Trip.start_date <= target.end_date + window,
            Trip.end_date >= target.start_date - window,
        )
        .order_by(Trip.start_date.desc())
        .limit(_WINDOW_LIMIT)
    )
    if target.trip_id is not None:
        stmt = stmt.where(Trip.id != target.trip_id)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


async def _load_trip_segments(db: AsyncSession, trip_id: uuid.UUID) -> list[Segment]:
    """Load non-cancelled segments for *trip_id*, ordered by start_at."""
    stmt = (
        select(Segment)
        .where(
            Segment.trip_id == trip_id,
            Segment.status != "cancelled",
        )
        .order_by(Segment.start_at)
    )
    return list((await db.execute(stmt)).scalars().all())


def _trip_is_open(trip_view: ConsolidationTarget, home: str) -> bool:
    """Trip is 'open' iff its end_city is NOT home (no return-to-home leg yet)."""
    return trip_view.end_city != home


def _trip_has_outbound_from_home(trip_view: ConsolidationTarget, home: str) -> bool:
    """True when the trip started from home (start_city == home)."""
    return trip_view.start_city == home


def _shared_endpoint_city(a: ConsolidationTarget, b: ConsolidationTarget) -> bool:
    """True when any endpoint city appears in both targets (case-sensitive)."""
    a_cities = {c for c in (a.start_city, a.end_city) if c}
    b_cities = {c for c in (b.start_city, b.end_city) if c}
    return bool(a_cities & b_cities)


def _coords_for(target: ConsolidationTarget) -> list[tuple[float, float]]:
    """Resolve (lat, lon) for each endpoint via IATA first, city-name fallback.

    Note: when both an IATA and a city name resolve for the same endpoint
    (e.g. iata='CDG' + city='Paris'), both points are appended. The min()
    in `_min_endpoint_distance_km` then picks the closest pair, which can
    score LOW slightly more permissively than airport-to-airport alone
    (~20 km drift between an airport and its city centroid). Accepted for
    v0.9.0 — fix if real data shows false-positive LOW matches.
    """
    coords: list[tuple[float, float]] = []
    # Airport lookup is most precise — collect for all known IATAs.
    for iata in target.endpoint_iatas:
        ap = get_airport(iata)
        if ap is not None:
            coords.append((ap.lat, ap.lon))
    # City-name fallback for endpoints that may not have IATAs.
    for city in (target.start_city, target.end_city):
        if not city:
            continue
        c = lookup_city(city)
        if c is not None:
            coords.append((c.lat, c.lon))
    return coords


def _min_endpoint_distance_km(a: ConsolidationTarget, b: ConsolidationTarget) -> float:
    """Minimum haversine distance (km) between any pair of resolved endpoints."""
    a_pts = _coords_for(a)
    b_pts = _coords_for(b)
    if not a_pts or not b_pts:
        return float("inf")
    return min(haversine_km(p, q) for p in a_pts for q in b_pts)


async def consolidation_candidates(
    db: AsyncSession,
    user: User,
    target: ConsolidationTarget,
) -> list[ConsolidationCandidate]:
    """Return up to 3 trips that look like they should be merged with *target*.

    Scoring:
    - HIGH  — home-anchored (next leg or closing leg back home)
    - MEDIUM — shared endpoint city (geometric fallback)
    - LOW  — nearest endpoint pair ≤ 500 km (geometric fallback)

    Sorted by (weight DESC, start_date DESC).

    Spec deviation (deferred to v0.9.1): per spec §3.2 prose, home-anchored
    matching is meant to be gap-agnostic. This implementation runs everything
    through the same ±3-day window as geometric fallback (matching the spec
    pseudocode, not the prose). Long-gap home-anchored chains will not
    surface until this is widened.

    Performance note: N+1 SELECTs across the windowed trips (≤ 50 via LIMIT).
    Bounded and runs per-user-action, not in a hot loop.
    """
    home = await infer_home(db, user.id)
    candidates: list[ConsolidationCandidate] = []

    for trip in await _user_trips_within_window(db, user, target):
        trip_segments = await _load_trip_segments(db, trip.id)
        trip_view = ConsolidationTarget.from_trip(trip, trip_segments)

        weight: _Weight | None = None

        # --- Home-anchored scoring (HIGH) ---
        if home is not None and _trip_is_open(trip_view, home):
            if target.start_city == trip_view.end_city:
                # New segment continues from where the existing trip ends.
                weight = _Weight.HIGH
            elif target.end_city == home and _trip_has_outbound_from_home(trip_view, home):
                # New segment is the closing leg back home.
                weight = _Weight.HIGH

        # --- Geometric fallback (MEDIUM / LOW) ---
        if weight is None:
            if _shared_endpoint_city(trip_view, target):
                weight = _Weight.MEDIUM
            elif _min_endpoint_distance_km(trip_view, target) <= _DISTANCE_KM_LOW:
                weight = _Weight.LOW

        if weight is not None:
            candidates.append(ConsolidationCandidate(trip=trip, weight=weight))

    # Sort: highest weight first; ties broken by newer start_date.
    candidates.sort(key=lambda c: (-int(c.weight), -c.trip.start_date.toordinal()))
    return candidates[:_TOP_K]
