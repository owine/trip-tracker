"""City lookup from a bundled, filtered GeoNames cities-1000 TSV.

Loaded once at module import (~12-15 MB resident after parsing).
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from importlib import resources


@dataclass(frozen=True)
class City:
    name: str
    asciiname: str
    country_code: str  # ISO 3166-1 alpha-2
    population: int
    lat: float
    lon: float


def _load() -> tuple[
    dict[str, list[City]],
    dict[tuple[str, str], list[City]],
    dict[str, list[City]],
]:
    by_name: dict[str, list[City]] = defaultdict(list)
    by_ascii_country: dict[tuple[str, str], list[City]] = defaultdict(list)
    by_ascii: dict[str, list[City]] = defaultdict(list)

    src = resources.files("trip_tracker.static.data").joinpath("cities1000.tsv")
    with src.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            try:
                city = City(
                    name=row["name"],
                    asciiname=row["asciiname"],
                    country_code=row["country_code"],
                    population=int(row["population"] or "0"),
                    lat=float(row["lat"]),
                    lon=float(row["lon"]),
                )
            except (ValueError, KeyError):
                continue
            by_name[city.name.casefold()].append(city)
            by_ascii_country[(city.asciiname.casefold(), city.country_code)].append(city)
            by_ascii[city.asciiname.casefold()].append(city)

    for bucket in by_name.values():
        bucket.sort(key=lambda c: c.population, reverse=True)
    for bucket in by_ascii_country.values():
        bucket.sort(key=lambda c: c.population, reverse=True)
    for bucket in by_ascii.values():
        bucket.sort(key=lambda c: c.population, reverse=True)
    return dict(by_name), dict(by_ascii_country), dict(by_ascii)


_BY_NAME, _BY_ASCII_COUNTRY, _BY_ASCII = _load()


def lookup_city(name: str, country: str | None = None) -> City | None:
    """Return the highest-population match for `name`."""
    folded = name.casefold()
    if country:
        bucket = _BY_ASCII_COUNTRY.get((folded, country.upper()))
        if bucket:
            return bucket[0]
    bucket = _BY_NAME.get(folded)
    if bucket:
        return bucket[0]
    # Try asciiname without country (e.g., "Zurich" → "Zuerich")
    bucket = _BY_ASCII.get(folded)
    if bucket:
        return bucket[0]
    return None
