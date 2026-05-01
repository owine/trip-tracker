"""Resolve a Segment.start_location / end_location dict to (lat, lon)."""

from __future__ import annotations

from typing import Any

from trip_tracker.geo.cities import lookup_city
from trip_tracker.parsers.enrich import get_airport


def resolve_point(loc: dict[str, Any] | None) -> tuple[float, float] | None:
    """Pick the best (lat, lon) for a JSONB location dict, in priority order:
    1. iata → airports lookup
    2. (city, country) → cities1000 lookup
    3. None
    """
    if not loc:
        return None
    iata = loc.get("iata")
    if iata:
        airport = get_airport(iata)
        if airport is not None:
            return airport.lat, airport.lon
    city = loc.get("city")
    if city:
        country = loc.get("country")
        c = lookup_city(city, country=country)
        if c is not None:
            return c.lat, c.lon
    return None
