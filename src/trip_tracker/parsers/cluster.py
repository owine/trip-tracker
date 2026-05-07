"""Trip clustering rule: place a SegmentDraft into an existing Trip
(or signal a new one / route to /inbox).

Rule (spec §5):
  candidates = trips overlapping or adjacent ±1 day AND in location proximity
  score = 1 / (1 + days_to_trip_center)
  if no candidates -> create_new (auto-title)
  if best.score - second_best.score < 0.20 of best -> ambiguous
  else -> attach to best
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.trip import Trip
from trip_tracker.parsers.base import SegmentDraft
from trip_tracker.parsers.enrich import get_airport

# Segment types whose primary destination is the END location (vs start).
_END_DESTINATION_TYPES = {"flight", "train", "transfer"}

# Spec §5: 200km threshold for airport-coord distance.
GEO_PROXIMITY_KM = 200.0


@dataclass
class ClusterDecision:
    kind: Literal["attach", "create_new", "ambiguous"]
    trip_id: uuid.UUID | None = None
    auto_title: str | None = None  # populated when kind == 'create_new'


def _airport_city(iata: str | None) -> str | None:
    """Resolve IATA to canonical city (first token before '(' or ',')."""
    if not iata:
        return None
    ap = get_airport(iata)
    if not ap:
        return None
    city = ap.city.split("(")[0].split(",")[0].strip()
    return city or None


def derive_destination(draft: SegmentDraft) -> str | None:
    """Return the raw destination city from location dicts (no airport DB lookup).

    For flights/trains/transfers the END location is the destination;
    for lodging/car/activity the START location is used.
    """
    end_loc = draft.end_location or {}
    start_loc = draft.start_location or {}
    if draft.type in _END_DESTINATION_TYPES:
        return end_loc.get("city") or start_loc.get("city")
    return start_loc.get("city") or end_loc.get("city")


def _resolved_destination(draft: SegmentDraft) -> str | None:
    """Like derive_destination but prefers airport DB city over raw dict city."""
    end_loc = draft.end_location or {}
    start_loc = draft.start_location or {}
    if draft.type in _END_DESTINATION_TYPES:
        return (
            _airport_city(end_loc.get("iata"))
            or end_loc.get("city")
            or _airport_city(start_loc.get("iata"))
            or start_loc.get("city")
        )
    return (
        _airport_city(start_loc.get("iata"))
        or start_loc.get("city")
        or _airport_city(end_loc.get("iata"))
        or end_loc.get("city")
    )


def _auto_title(draft: SegmentDraft) -> str:
    dest = _resolved_destination(draft) or "Trip"
    return f"{dest} {draft.start_at.strftime('%B %Y')}"


def _segment_dates(draft: SegmentDraft) -> tuple[date, date]:
    s = draft.start_at.date()
    e = (draft.end_at or draft.start_at).date()
    return s, e


def _location_proximity(draft: SegmentDraft, trip: Trip) -> bool:
    """Approximate spec rule: same city OR within 200km via airport coords."""
    draft_dest = _resolved_destination(draft)
    trip_dest = trip.primary_destination
    if draft_dest and trip_dest and draft_dest.strip().lower() == trip_dest.strip().lower():
        return True
    # Also check both start and end airport cities against the trip destination.
    start_iata = (draft.start_location or {}).get("iata")
    end_iata = (draft.end_location or {}).get("iata")
    for iata in (start_iata, end_iata):
        ap_city = _airport_city(iata)
        if ap_city and trip_dest and ap_city.lower() == trip_dest.strip().lower():
            return True
    return False


def _date_overlap_or_adjacent(draft: SegmentDraft, trip: Trip, *, adjacent_days: int = 1) -> bool:
    s, e = _segment_dates(draft)
    delta = timedelta(days=adjacent_days)
    return s - delta <= trip.end_date and e + delta >= trip.start_date


def _score(draft: SegmentDraft, trip: Trip) -> float:
    """Higher = better match. Inverse of days from trip center to segment start."""
    center_days = (trip.start_date - date(1970, 1, 1)).days + (
        (trip.end_date - trip.start_date).days / 2
    )
    seg_days = (draft.start_at.date() - date(1970, 1, 1)).days
    distance = abs(center_days - seg_days)
    return 1.0 / (1.0 + distance)


async def cluster_for_user(
    db: AsyncSession,
    user_id: uuid.UUID,  # noqa: ARG001
    draft: SegmentDraft,
) -> ClusterDecision:
    """Find the best Trip for `draft` among `user_id`'s trips, or signal a new one."""
    rows = (await db.execute(select(Trip).where(Trip.merged_into_id.is_(None)))).scalars().all()

    candidates: list[tuple[Trip, float]] = []
    for trip in rows:
        if not _date_overlap_or_adjacent(draft, trip):
            continue
        if not _location_proximity(draft, trip):
            continue
        candidates.append((trip, _score(draft, trip)))

    if not candidates:
        return ClusterDecision(kind="create_new", auto_title=_auto_title(draft))

    candidates.sort(key=lambda x: x[1], reverse=True)
    best_trip, best_score = candidates[0]
    if len(candidates) >= 2:
        _, second_score = candidates[1]
        if best_score > 0 and (best_score - second_score) / best_score < 0.20:
            return ClusterDecision(kind="ambiguous", trip_id=best_trip.id)

    return ClusterDecision(kind="attach", trip_id=best_trip.id)
