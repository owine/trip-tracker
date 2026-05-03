"""Trip consolidation suggestions — home-anchored with geometric fallback."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import IntEnum

from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.parsers.base import SegmentDraft  # not schemas.segments


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
