"""Static IATA → tz/lat/lon lookup + haversine distance.

The airports.csv file is loaded once at module import (small, ~500 KB).
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from importlib import resources
from typing import Any


@dataclass(frozen=True)
class Airport:
    iata: str
    name: str
    city: str
    country: str
    tz: str
    lat: float
    lon: float


def _load() -> dict[str, Airport]:
    out: dict[str, Airport] = {}
    src = resources.files("trip_tracker.static.data").joinpath("airports.csv")
    with src.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get("tz") or not row.get("lat") or not row.get("lon"):
                continue
            try:
                out[row["iata"].upper()] = Airport(
                    iata=row["iata"].upper(),
                    name=row["name"],
                    city=row["city"],
                    country=row["country"],
                    tz=row["tz"],
                    lat=float(row["lat"]),
                    lon=float(row["lon"]),
                )
            except (ValueError, KeyError):
                continue
    return out


_AIRPORTS: dict[str, Airport] = _load()


def get_airport(iata: str) -> Airport | None:
    return _AIRPORTS.get(iata.upper())


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance between two (lat, lon) points, in km."""
    lat1, lon1 = a
    lat2, lon2 = b
    r = 6371.0  # Earth radius km
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def enrich_airport(loc: dict[str, Any]) -> dict[str, Any]:
    """If `loc` has an 'iata' key, fill in tz/lat/lon if known. Return a new dict."""
    iata = loc.get("iata")
    if not iata:
        return dict(loc)
    a = get_airport(iata)
    if a is None:
        return dict(loc)
    return {**loc, "tz": a.tz, "lat": a.lat, "lon": a.lon, "city": loc.get("city") or a.city}
