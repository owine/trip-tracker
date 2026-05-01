# Phase 7 — World Map + Weather Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `/map` (lifetime atlas) and `/trips/<id>/map` (per-trip route + weather cards) using Leaflet + OpenStreetMap tiles, bundled GeoNames cities-1000 for non-airport pins, great-circle arcs for flights, and Open-Meteo weather forecasts cached in Redis.

**Architecture:** A new `geo/` subpackage adds city lookup (filtered cities-1000 TSV) + great-circle slerp + a `resolve_point` dispatcher; airports re-use Phase 3's `parsers/enrich.py` (`@dataclass Airport`, fields `lat`/`lon`/`tz`). A new `weather/` subpackage adds an Open-Meteo httpx client + a Redis-backed forecast cache + a `refresh_weather` saq task. Two new routes in `routes/map.py` (auth-gated, `traveler_ids`-scoped) render Leaflet templates with markers, arcs, and weather popups. Coordinate convention is `(lat, lon)` everywhere (matches `parsers/enrich.py`).

**Tech Stack:** Python 3.14 (target=py313), FastAPI, SQLAlchemy 2.0 async, Postgres 18, Redis 7, saq, httpx (already in deps), Leaflet 1.9.4 via CDN with SRI, OpenStreetMap tiles, Open-Meteo (free, keyless) for forecasts, GeoNames cities-1000 for city centroids (CC BY 4.0 attribution required).

**Spec reference:** [`docs/superpowers/specs/2026-05-01-phase7-map-weather-design.md`](../specs/2026-05-01-phase7-map-weather-design.md). Section numbers (e.g. §6.3) below refer to this spec.

**Branch:** `feat/phase-7-map-weather`. Cut from `main` at the HEAD when implementation starts (currently `d987a2d` after the spec landed).

**Out of scope (Phase 7.x):** date-line wraparound for Pacific arcs, `/api/weather` JSON endpoint, per-segment location overrides, lifetime-atlas weather, imperial-unit toggle, self-hosted tile server.

**Toolchain quirks worth re-stating per task:**

- `from __future__ import annotations` at top of every new module.
- ruff `target=py313` + mypy `python_version=3.14`. PEP 585 forms (`list[...]`, `dict[...]`, `str | None`).
- `parsers/enrich.py` already provides `Airport`, `get_airport(iata)`, `haversine_km(a, b)`. **Re-use it directly** — no new airports loader.
- `worker.py` uses a module-level `settings = {...}` dict (Phase 4 saq migration shape). New saq tasks are appended to its `functions` list. Worker startup also adds new ctx keys (e.g., `ctx["redis"]`).
- The cities-1000 source from GeoNames is `cities1000.txt` (tab-separated, 19 columns, ~28 MB). Phase 7 bundles a **filtered 6-column TSV** (~7–10 MB) at `src/trip_tracker/static/data/cities1000.tsv`. The filter script lives at `scripts/_make_cities_data.py` and is committed for reproducibility but not run at build time.
- pre-commit djlint hook is `djlint-reformat`; template formatting must round-trip clean.
- The existing autouse fixtures in `tests/conftest.py` mock the saq queue (`_mock_meili_queue`, `_mock_documents_queue`); Phase 7 may need a similar one for `weather/cache.py` if the route handler reaches into Redis directly during page render.

---

## File Structure

```
src/trip_tracker/
├── geo/                                     [CREATE — new subpackage]
│   ├── __init__.py                          (marker only)
│   ├── cities.py                            City + lookup_city
│   ├── arcs.py                              great_circle_points (slerp)
│   └── resolve.py                           resolve_point dispatcher
│   #  airports lookup re-uses parsers.enrich (NO new module)
├── weather/                                 [CREATE — new subpackage]
│   ├── __init__.py                          (marker only)
│   ├── client.py                            Forecast model + fetch_forecast
│   └── cache.py                             get_cached / set_cached
├── routes/
│   └── map.py                               [CREATE — /map and /trips/{id}/map]
├── worker.py                                [MODIFY: add refresh_weather; ctx["redis"] in startup]
├── app.py                                   [MODIFY: include map_router]
├── static/data/
│   └── cities1000.tsv                       [CREATE — bundled, filtered ~7-10 MB]
└── templates/
    ├── base.html                            [MODIFY: add /map navbar link]
    └── map/                                 [CREATE]
        ├── _leaflet_head.html               (Leaflet CSS/JS CDN includes — shared partial)
        ├── all_trips.html                   /map view
        └── trip.html                        /trips/<id>/map view

scripts/
└── _make_cities_data.py                    [CREATE — one-shot filter from GeoNames source]

tests/
├── test_geo_cities.py                       [CREATE]
├── test_geo_arcs.py                         [CREATE]
├── test_geo_resolve.py                      [CREATE]
├── test_weather_client.py                   [CREATE]
├── test_weather_cache.py                    [CREATE]
├── test_worker_refresh_weather.py           [CREATE]
├── test_routes_map_lifetime.py              [CREATE]
└── test_routes_map_per_trip.py              [CREATE]
```

---

## Task 1 — Cities lookup + bundled `cities1000.tsv` + `resolve_point`

**Spec ref:** §4.1, §4.2, §4.4.

**Files:**
- Create: `src/trip_tracker/geo/__init__.py` (`"""Geographic lookups (Phase 7)."""`)
- Create: `src/trip_tracker/geo/cities.py`
- Create: `src/trip_tracker/geo/resolve.py`
- Create: `src/trip_tracker/static/data/cities1000.tsv` (filtered bundle)
- Create: `scripts/_make_cities_data.py`
- Create: `tests/test_geo_cities.py`
- Create: `tests/test_geo_resolve.py`

### Step 1.1 — Write the filter script

`scripts/_make_cities_data.py`:

```python
"""One-shot script: download cities1000 from GeoNames and emit a filtered TSV.

Run once locally (not at build time):
    uv run python scripts/_make_cities_data.py

Produces src/trip_tracker/static/data/cities1000.tsv with 6 columns:
    name <TAB> asciiname <TAB> country_code <TAB> population <TAB> lat <TAB> lon

Source CSV columns (GeoNames cities1000.txt, 19 columns, tab-separated):
    geonameid, name, asciiname, alternatenames, latitude, longitude, feature_class,
    feature_code, country_code, cc2, admin1_code, admin2_code, admin3_code,
    admin4_code, population, elevation, dem, timezone, modification_date

We keep: name, asciiname, country_code, population, lat, lon (~7-10 MB after filter).
"""

from __future__ import annotations

import csv
import io
import urllib.request
import zipfile
from pathlib import Path

GEONAMES_URL = "https://download.geonames.org/export/dump/cities1000.zip"
OUT = Path(__file__).parent.parent / "src" / "trip_tracker" / "static" / "data" / "cities1000.tsv"


def main() -> None:
    print(f"Downloading {GEONAMES_URL} ...")
    with urllib.request.urlopen(GEONAMES_URL) as resp:  # nosec B310 — fixed URL, dev-only
        zip_bytes = resp.read()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        raw = zf.read("cities1000.txt").decode("utf-8")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with OUT.open("w", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter="\t", lineterminator="\n")
        writer.writerow(["name", "asciiname", "country_code", "population", "lat", "lon"])
        for line in raw.splitlines():
            cols = line.split("\t")
            if len(cols) < 19:
                continue
            try:
                name = cols[1]
                asciiname = cols[2]
                lat = float(cols[4])
                lon = float(cols[5])
                country_code = cols[8]
                population = int(cols[14] or "0")
            except (ValueError, IndexError):
                continue
            writer.writerow([name, asciiname, country_code, population, lat, lon])
            n += 1
    print(f"Wrote {n} cities to {OUT} ({OUT.stat().st_size / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main()
```

Run once: `uv run python scripts/_make_cities_data.py`. Commit the resulting `cities1000.tsv`.

### Step 1.2 — Write failing tests

`tests/test_geo_cities.py`:

```python
"""City lookup: highest-population fallback + country-code disambiguation."""

from __future__ import annotations

from trip_tracker.geo.cities import City, lookup_city


def test_lookup_paris_returns_paris_france() -> None:
    """No country hint → highest-pop match (Paris/FR ~2.1M)."""
    c = lookup_city("Paris")
    assert c is not None
    assert c.country_code == "FR"
    assert c.population > 2_000_000


def test_lookup_paris_with_us_country_returns_paris_texas() -> None:
    c = lookup_city("Paris", country="US")
    assert c is not None
    assert c.country_code == "US"
    # Paris, Texas is ~25k; Paris, KY is ~10k. Highest-pop US match should be one of them.
    assert c.population < 100_000


def test_lookup_unknown_city_returns_none() -> None:
    assert lookup_city("Atlantis") is None


def test_lookup_handles_diacritics_via_asciiname() -> None:
    """'Zurich' (no umlaut) should match 'Zürich' via the asciiname index."""
    c = lookup_city("Zurich")
    assert c is not None
    assert c.country_code == "CH"


def test_city_is_frozen_dataclass() -> None:
    c = lookup_city("Paris")
    assert c is not None
    import dataclasses
    assert dataclasses.is_dataclass(c)
    # Cannot mutate (frozen=True)
    import pytest
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.population = 0  # type: ignore[misc]
```

`tests/test_geo_resolve.py`:

```python
"""resolve_point: iata → city → None priority chain."""

from __future__ import annotations

from trip_tracker.geo.resolve import resolve_point


def test_resolve_with_iata_returns_airport_coords() -> None:
    point = resolve_point({"iata": "JFK", "city": "New York"})
    assert point is not None
    lat, lon = point
    # JFK is at ~40.64°N, 73.78°W
    assert 40.5 < lat < 40.7
    assert -74 < lon < -73


def test_resolve_with_unknown_iata_falls_back_to_city() -> None:
    """Unknown IATA but known city → city centroid."""
    point = resolve_point({"iata": "XYZ", "city": "Berlin", "country": "DE"})
    assert point is not None
    lat, lon = point
    # Berlin is ~52.52°N, 13.40°E
    assert 52 < lat < 53
    assert 13 < lon < 14


def test_resolve_with_only_city() -> None:
    point = resolve_point({"city": "Tokyo", "country": "JP"})
    assert point is not None
    lat, lon = point
    # Tokyo ~35.7°N, 139.7°E
    assert 35 < lat < 36
    assert 139 < lon < 140


def test_resolve_with_no_location_data_returns_none() -> None:
    assert resolve_point(None) is None
    assert resolve_point({}) is None


def test_resolve_unknown_city_returns_none() -> None:
    assert resolve_point({"city": "Atlantis"}) is None
```

### Step 1.3 — Run tests to verify they fail

```bash
uv run pytest tests/test_geo_cities.py tests/test_geo_resolve.py -v
# Expect: ImportError on trip_tracker.geo.cities + .resolve
```

### Step 1.4 — Implement `cities.py`

`src/trip_tracker/geo/cities.py`:

```python
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
    country_code: str   # ISO 3166-1 alpha-2
    population: int
    lat: float
    lon: float


def _load() -> tuple[
    dict[str, list[City]],            # name (case-folded) → cities sorted by pop desc
    dict[tuple[str, str], list[City]], # (asciiname, country_code) → cities
]:
    by_name: dict[str, list[City]] = defaultdict(list)
    by_ascii_country: dict[tuple[str, str], list[City]] = defaultdict(list)

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

    # Sort each bucket by population descending so first match wins
    for bucket in by_name.values():
        bucket.sort(key=lambda c: c.population, reverse=True)
    for bucket in by_ascii_country.values():
        bucket.sort(key=lambda c: c.population, reverse=True)
    return dict(by_name), dict(by_ascii_country)


_BY_NAME, _BY_ASCII_COUNTRY = _load()


def lookup_city(name: str, country: str | None = None) -> City | None:
    """Return the highest-population match for `name`.

    If `country` is provided, restrict to that country code first; on no
    match, fall back to the global highest-population match.
    """
    folded = name.casefold()
    if country:
        bucket = _BY_ASCII_COUNTRY.get((folded, country.upper()))
        if bucket:
            return bucket[0]
    bucket = _BY_NAME.get(folded)
    if bucket:
        return bucket[0]
    # Final fallback: try asciiname across all countries (handles diacritics-stripped input)
    for (ascii_name, _cc), b in _BY_ASCII_COUNTRY.items():
        if ascii_name == folded:
            return b[0]
    return None
```

### Step 1.5 — Implement `resolve.py`

`src/trip_tracker/geo/resolve.py`:

```python
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
```

### Step 1.6 — Run tests + commit

```bash
uv run pytest tests/test_geo_cities.py tests/test_geo_resolve.py -v
# 10 tests pass

uv run pytest -q                  # full suite stays green (341 + 10 = 351)
uv run mypy src
uv run ruff check . && uv run ruff format --check .

git add src/trip_tracker/geo/ \
        src/trip_tracker/static/data/cities1000.tsv \
        scripts/_make_cities_data.py \
        tests/test_geo_cities.py tests/test_geo_resolve.py
git commit -m "feat(geo): cities-1000 lookup + resolve_point dispatcher"
```

**Quality bar:**
- `from __future__ import annotations` at top of every new module.
- `casefold()` (not `lower()`) for international case-folding (handles `ß` → `ss` in German etc).
- The `for (ascii_name, _cc), b in _BY_ASCII_COUNTRY.items()` final-fallback loop is O(N) on the dict size (~150k entries). Acceptable for a per-render cost when country is unset; if perf bites, add a separate `by_ascii_no_country` index.
- Don't pin reportlab or any other dev dep for the filter script — it's stdlib-only (`urllib`, `zipfile`, `csv`).
- The `# nosec B310` on `urllib.request.urlopen` is intentional (the URL is hardcoded).
- The TSV file is committed as binary; add to `.gitattributes` as `binary` if git diff complains. Phase 5 already pinned `tests/fixtures/documents/*.pdf` as binary; mirror.

---

## Task 2 — Great-circle interpolation

**Spec ref:** §4.3.

**Files:**
- Create: `src/trip_tracker/geo/arcs.py`
- Create: `tests/test_geo_arcs.py`

### Step 2.1 — Write failing tests

`tests/test_geo_arcs.py`:

```python
"""Great-circle arc interpolation: spherical linear interpolation (slerp)."""

from __future__ import annotations

import math

import pytest

from trip_tracker.geo.arcs import great_circle_points


def test_endpoints_match_input() -> None:
    points = great_circle_points((40.64, -73.78), (49.01, 2.55), n_points=10)
    assert points[0] == pytest.approx((40.64, -73.78), abs=1e-3)
    assert points[-1] == pytest.approx((49.01, 2.55), abs=1e-3)


def test_n_points_count() -> None:
    points = great_circle_points((0, 0), (0, 90), n_points=50)
    assert len(points) == 50


def test_jfk_to_cdg_arches_north() -> None:
    """A great-circle JFK → CDG passes north of the straight Mercator line."""
    start = (40.64, -73.78)   # JFK
    end = (49.01, 2.55)       # CDG
    midpoint_idx = 50 // 2
    points = great_circle_points(start, end, n_points=50)
    mid_lat, _mid_lon = points[midpoint_idx]
    # The straight Mercator midpoint of JFK→CDG is at lat ~44.8.
    # The great-circle midpoint is at ~52° (curving north over Greenland).
    straight_mid_lat = (start[0] + end[0]) / 2
    assert mid_lat > straight_mid_lat + 4   # at least 4° farther north


def test_short_distance_arc_is_nearly_linear() -> None:
    """JFK → BOS is short (~300 km); arc should be nearly straight."""
    start = (40.64, -73.78)   # JFK
    end = (42.36, -71.01)     # BOS
    points = great_circle_points(start, end, n_points=20)
    # Midpoint should be very close to the straight midpoint
    mid_lat, mid_lon = points[10]
    straight_lat = (start[0] + end[0]) / 2
    straight_lon = (start[1] + end[1]) / 2
    assert abs(mid_lat - straight_lat) < 0.1
    assert abs(mid_lon - straight_lon) < 0.1


def test_antipodal_points_no_crash() -> None:
    """Edge case: nearly-antipodal points (great-circle is degenerate)."""
    points = great_circle_points((0, 0), (0, 179.99), n_points=10)
    assert len(points) == 10
    # Just verify we don't crash and endpoints are right
    assert points[0] == pytest.approx((0, 0), abs=1e-3)
    assert points[-1] == pytest.approx((0, 179.99), abs=1e-3)


def test_default_n_points_is_50() -> None:
    points = great_circle_points((0, 0), (0, 90))
    assert len(points) == 50
```

### Step 2.2 — Run tests to verify failure

```bash
uv run pytest tests/test_geo_arcs.py -v
# Expect: ImportError
```

### Step 2.3 — Implement

`src/trip_tracker/geo/arcs.py`:

```python
"""Great-circle interpolation via spherical linear interpolation (slerp).

Used to render flight arcs that look correctly curved on a 2D map (Web
Mercator, Equirectangular, etc.) instead of straight Mercator lines that
slice through the wrong hemisphere on long-haul routes.
"""

from __future__ import annotations

import math


def _to_xyz(lat: float, lon: float) -> tuple[float, float, float]:
    """Lat/lon (degrees) → unit-sphere xyz."""
    lat_r = math.radians(lat)
    lon_r = math.radians(lon)
    return (
        math.cos(lat_r) * math.cos(lon_r),
        math.cos(lat_r) * math.sin(lon_r),
        math.sin(lat_r),
    )


def _from_xyz(x: float, y: float, z: float) -> tuple[float, float]:
    """Unit-sphere xyz → lat/lon (degrees)."""
    lat = math.degrees(math.asin(z))
    lon = math.degrees(math.atan2(y, x))
    return lat, lon


def great_circle_points(
    start: tuple[float, float],
    end: tuple[float, float],
    n_points: int = 50,
) -> list[tuple[float, float]]:
    """Interpolate `n_points` along the great-circle arc between two lat/lon pairs.

    Returns a list of (lat, lon) tuples suitable for Leaflet's L.polyline.
    Uses spherical linear interpolation (slerp) on the unit sphere.
    """
    if n_points < 2:
        raise ValueError("n_points must be ≥ 2")

    p1 = _to_xyz(*start)
    p2 = _to_xyz(*end)
    # Angle between the two vectors
    dot = max(-1.0, min(1.0, p1[0] * p2[0] + p1[1] * p2[1] + p1[2] * p2[2]))
    omega = math.acos(dot)

    if omega < 1e-9:
        # Coincident points: just return n_points copies of start
        return [start for _ in range(n_points)]

    sin_omega = math.sin(omega)
    points: list[tuple[float, float]] = []
    for i in range(n_points):
        t = i / (n_points - 1)
        a = math.sin((1 - t) * omega) / sin_omega
        b = math.sin(t * omega) / sin_omega
        x = a * p1[0] + b * p2[0]
        y = a * p1[1] + b * p2[1]
        z = a * p1[2] + b * p2[2]
        points.append(_from_xyz(x, y, z))
    return points
```

### Step 2.4 — Run + commit

```bash
uv run pytest tests/test_geo_arcs.py -v          # 6 tests pass
uv run pytest -q                                  # full suite green
uv run mypy src && uv run ruff check . && uv run ruff format --check .

git add src/trip_tracker/geo/arcs.py tests/test_geo_arcs.py
git commit -m "feat(geo): great-circle arc interpolation (slerp)"
```

**Quality bar:**
- `dot = max(-1.0, min(1.0, ...))` clamps for numerical stability — `acos(1.0000001)` raises `ValueError`. Don't skip.
- `if omega < 1e-9:` handles the coincident-endpoints edge case (start == end).
- Don't try to handle the **antipodal** case (start at (0,0) and end at (0,180)) gracefully — slerp is genuinely undefined there. The test passes near-antipodal (179.99) which has a unique solution.
- ~30 lines of math; no external libs.

---

## Task 3 — Open-Meteo client + Redis cache + saq task wiring

**Spec ref:** §6.1, §6.2, §6.4, §6.5.

**Files:**
- Create: `src/trip_tracker/weather/__init__.py` (`"""Weather forecasts via Open-Meteo (Phase 7)."""`)
- Create: `src/trip_tracker/weather/client.py`
- Create: `src/trip_tracker/weather/cache.py`
- Modify: `src/trip_tracker/worker.py` — add `refresh_weather` task; add `ctx["redis"]` in `startup()`; append to `functions` list
- Create: `tests/test_weather_client.py`
- Create: `tests/test_weather_cache.py`
- Create: `tests/test_worker_refresh_weather.py`

### Step 3.1 — Failing tests

`tests/test_weather_client.py`:

```python
"""Open-Meteo HTTP client: parses the daily-forecast response into a Pydantic model."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from trip_tracker.weather.client import Forecast, fetch_forecast


@pytest.mark.asyncio
async def test_fetch_forecast_parses_response() -> None:
    fake = {
        "latitude": 48.8566,
        "longitude": 2.3522,
        "timezone": "Europe/Paris",
        "daily": {
            "time": ["2026-06-01", "2026-06-02"],
            "temperature_2m_max": [22.4, 24.1],
            "temperature_2m_min": [13.5, 14.8],
            "weather_code": [1, 3],
            "precipitation_probability_max": [10, 60],
        },
    }
    with respx.mock(base_url="https://api.open-meteo.com") as router:
        router.get("/v1/forecast").respond(json=fake)
        f = await fetch_forecast(48.8566, 2.3522)
    assert isinstance(f, Forecast)
    assert f.lat == pytest.approx(48.8566)
    assert f.lon == pytest.approx(2.3522)
    assert f.timezone == "Europe/Paris"
    assert len(f.days) == 2
    assert f.days[0].date == date(2026, 6, 1)
    assert f.days[0].temp_max_c == 22.4
    assert f.days[1].precip_prob == 60


@pytest.mark.asyncio
async def test_fetch_forecast_raises_on_5xx() -> None:
    with respx.mock(base_url="https://api.open-meteo.com") as router:
        router.get("/v1/forecast").respond(503)
        with pytest.raises(httpx.HTTPError):
            await fetch_forecast(0, 0)
```

`tests/test_weather_cache.py`:

```python
"""Redis cache for forecasts: round-trip + TTL key shape."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from trip_tracker.weather.cache import get_cached, set_cached
from trip_tracker.weather.client import DailyForecast, Forecast


def _f() -> Forecast:
    return Forecast(
        lat=48.86, lon=2.35, timezone="Europe/Paris",
        days=[
            DailyForecast(
                date=date(2026, 6, 1), temp_max_c=22.4, temp_min_c=13.5,
                weather_code=1, precip_prob=10,
            ),
        ],
    )


@pytest.mark.asyncio
async def test_set_then_get_round_trip() -> None:
    fake_redis = MagicMock()
    fake_redis.get = AsyncMock(return_value=None)
    fake_redis.set = AsyncMock()
    f = _f()
    await set_cached(f, fake_redis)
    fake_redis.set.assert_awaited_once()
    args, kwargs = fake_redis.set.call_args
    # Key shape: weather:<lat:.2f>:<lon:.2f>
    assert args[0] == "weather:48.86:2.35"
    # TTL set via `ex=` kwarg, 3600 seconds
    assert kwargs.get("ex") == 3600


@pytest.mark.asyncio
async def test_get_cached_returns_parsed_forecast() -> None:
    f = _f()
    fake_redis = MagicMock()
    fake_redis.get = AsyncMock(return_value=f.model_dump_json().encode("utf-8"))
    result = await get_cached(48.86, 2.35, fake_redis)
    assert result is not None
    assert result.lat == pytest.approx(48.86)
    assert result.days[0].temp_max_c == 22.4


@pytest.mark.asyncio
async def test_get_cached_returns_none_on_miss() -> None:
    fake_redis = MagicMock()
    fake_redis.get = AsyncMock(return_value=None)
    assert await get_cached(0, 0, fake_redis) is None


@pytest.mark.asyncio
async def test_cache_key_rounds_to_two_decimals() -> None:
    """48.85657 → '48.86'; -73.7842 → '-73.78'."""
    fake_redis = MagicMock()
    fake_redis.get = AsyncMock(return_value=None)
    await get_cached(48.85657, -73.7842, fake_redis)
    fake_redis.get.assert_awaited_once_with("weather:48.86:-73.78")
```

`tests/test_worker_refresh_weather.py`:

```python
"""saq task: refresh_weather pulls forecast and writes to Redis cache."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_refresh_weather_fetches_and_caches() -> None:
    from datetime import date

    # Import the task BEFORE patching so the worker module is fully loaded
    # and `fetch_forecast` is bound in the worker namespace at patch time.
    from trip_tracker.weather.client import DailyForecast, Forecast
    from trip_tracker.worker import refresh_weather

    fake_forecast = Forecast(
        lat=48.86, lon=2.35, timezone="Europe/Paris",
        days=[DailyForecast(date=date(2026, 6, 1), temp_max_c=20.0,
                             temp_min_c=10.0, weather_code=1, precip_prob=0)],
    )
    fake_redis = MagicMock()
    fake_redis.set = AsyncMock()

    with patch("trip_tracker.worker.fetch_forecast", AsyncMock(return_value=fake_forecast)):
        ctx = {"redis": fake_redis}
        await refresh_weather(ctx, lat=48.86, lon=2.35)

    fake_redis.set.assert_awaited_once()
    key = fake_redis.set.call_args.args[0]
    assert key == "weather:48.86:2.35"
```

### Step 3.2 — Run tests to verify failure

```bash
uv run pytest tests/test_weather_client.py tests/test_weather_cache.py \
              tests/test_worker_refresh_weather.py -v
# Expect: ImportError
```

### Step 3.3 — Implement `client.py`

`src/trip_tracker/weather/client.py`:

```python
"""Open-Meteo HTTP client. Free, keyless. Spec §6.1."""

from __future__ import annotations

from datetime import date

import httpx
from pydantic import BaseModel

_BASE_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT_SEC = 10.0


class DailyForecast(BaseModel):
    date: date
    temp_max_c: float
    temp_min_c: float
    weather_code: int      # WMO code; mapped to emoji + label client-side
    precip_prob: int       # 0-100


class Forecast(BaseModel):
    lat: float
    lon: float
    timezone: str
    days: list[DailyForecast]


async def fetch_forecast(lat: float, lon: float) -> Forecast:
    """Fetch a 7-day daily forecast from Open-Meteo. ~200 ms typical latency."""
    params: dict[str, str | float | int] = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max",
        "timezone": "auto",
        "forecast_days": 7,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT_SEC) as client:
        r = await client.get(_BASE_URL, params=params)
        r.raise_for_status()
        body = r.json()

    daily = body["daily"]
    days = [
        DailyForecast(
            date=date.fromisoformat(d),
            temp_max_c=tmax,
            temp_min_c=tmin,
            weather_code=int(wc),
            precip_prob=int(pp or 0),
        )
        for d, tmax, tmin, wc, pp in zip(
            daily["time"],
            daily["temperature_2m_max"],
            daily["temperature_2m_min"],
            daily["weather_code"],
            daily["precipitation_probability_max"],
            strict=True,
        )
    ]
    return Forecast(
        lat=float(body["latitude"]),
        lon=float(body["longitude"]),
        timezone=str(body["timezone"]),
        days=days,
    )
```

### Step 3.4 — Implement `cache.py`

`src/trip_tracker/weather/cache.py`:

```python
"""Redis-backed forecast cache. Keyed by (lat, lon) rounded to 2 decimals.

Cache values are JSON-serialized Forecast models, TTL 3600s. Keys are
shared across users (the Paris forecast is the same regardless of who
asks). Spec §6.2.
"""

from __future__ import annotations

from typing import Protocol

from trip_tracker.weather.client import Forecast

_TTL_SEC = 3600


class _RedisLike(Protocol):
    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: str | bytes, ex: int | None = None) -> object: ...


def _key(lat: float, lon: float) -> str:
    return f"weather:{lat:.2f}:{lon:.2f}"


async def get_cached(lat: float, lon: float, redis: _RedisLike) -> Forecast | None:
    raw = await redis.get(_key(lat, lon))
    if raw is None:
        return None
    return Forecast.model_validate_json(raw)


async def set_cached(forecast: Forecast, redis: _RedisLike) -> None:
    payload = forecast.model_dump_json()
    await redis.set(_key(forecast.lat, forecast.lon), payload, ex=_TTL_SEC)
```

### Step 3.5 — Wire into `worker.py`

In `src/trip_tracker/worker.py`:

1. Add imports near the existing imports:

```python
from redis.asyncio import Redis as AsyncRedis

from trip_tracker.weather.cache import set_cached
from trip_tracker.weather.client import fetch_forecast
```

2. Add a new task function alongside the existing `parse_raw_email` / `sync_meili` / `extract_document`:

```python
async def refresh_weather(ctx: dict[str, Any], *, lat: float, lon: float) -> None:
    """saq task: pull a fresh Open-Meteo forecast and cache it. Idempotent.

    Dedup key (set at enqueue time) collapses concurrent requests for the
    same (lat, lon) into one network call. Spec §6.4.
    """
    redis = ctx["redis"]
    forecast = await fetch_forecast(lat, lon)
    await set_cached(forecast, redis)
```

3. In `startup(ctx)`, add a redis client to `ctx`:

```python
async def startup(ctx: dict[str, Any]) -> None:
    s = Settings()
    ctx["settings"] = s
    ctx["engine"] = create_async_engine(str(s.database_url))
    ctx["meili"] = build_client(s)
    ctx["storage"] = LocalFsStorage(Path(s.documents_dir))
    ctx["redis"] = AsyncRedis.from_url(s.redis_url)   # NEW for Phase 7
```

4. In `shutdown(ctx)`, close the redis client:

```python
async def shutdown(ctx: dict[str, Any]) -> None:
    engine = ctx.get("engine")
    if engine is not None:
        await engine.dispose()
    redis = ctx.get("redis")
    if redis is not None:
        await redis.aclose()                          # NEW for Phase 7
```

5. Append `refresh_weather` to the `functions` list:

```python
settings = {
    "queue": queue,
    "functions": [parse_raw_email, sync_meili, extract_document, refresh_weather],
    "startup": startup,
    "shutdown": shutdown,
    "concurrency": 1,
}
```

### Step 3.6 — Run tests + commit

```bash
uv run pytest tests/test_weather_client.py tests/test_weather_cache.py \
              tests/test_worker_refresh_weather.py -v
# 7 tests pass

uv run pytest -q                                    # full suite green
uv run mypy src
uv run ruff check . && uv run ruff format --check .

git add src/trip_tracker/weather/ src/trip_tracker/worker.py \
        tests/test_weather_client.py tests/test_weather_cache.py \
        tests/test_worker_refresh_weather.py
git commit -m "feat(weather): Open-Meteo client + Redis cache + refresh_weather saq task"
```

**Quality bar:**
- `respx` is already in dev deps from Phase 4 (Meili client tests). If not, `uv add --dev respx` first; verify latest stable per `feedback_dependency-currency.md`.
- `redis.asyncio.Redis` is available via the already-installed `redis` package (Phase 4 saq migration bumped redis-py to 7.x).
- `httpx.AsyncClient` is already in deps (Phase 4 Meili).
- `redis.aclose()` (not `redis.close()`) — redis-py 7+ uses `aclose` for the async client.
- The `_RedisLike` Protocol keeps tests free of an actual Redis dependency; only the methods we use are typed.
- `Forecast.model_dump_json()` / `model_validate_json()` is the Pydantic v2 round-trip pattern.
- The `strict=True` on `zip` ensures all 5 daily arrays have the same length; Open-Meteo always does, but the strict guard catches schema drift.

---

## Task 4 — `/map` lifetime route + template

**Spec ref:** §5.1, §5.4, §5.5.

**Files:**
- Create: `src/trip_tracker/routes/map.py`
- Create: `src/trip_tracker/templates/map/_leaflet_head.html` (shared partial for Leaflet CDN includes)
- Create: `src/trip_tracker/templates/map/all_trips.html`
- Modify: `src/trip_tracker/templates/base.html` — add `/map` navbar link
- Modify: `src/trip_tracker/app.py` — include `map_router`
- Create: `tests/test_routes_map_lifetime.py`

### Step 4.1 — Failing tests

`tests/test_routes_map_lifetime.py`:

```python
"""GET /map: lifetime atlas — auth + all-trips JSON marshaling."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User


def _cookie(user, settings):
    return {"tt_session": encode_session(
        SessionPayload(user_id=user.id, oidc_subject=user.oidc_subject),
        secret=settings.session_secret.get_secret_value(),
        max_age=3600,
    )}


@asynccontextmanager
async def _ctx(app, settings, user):
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test",
            cookies=_cookie(user, settings),
        ) as c,
    ):
        yield c


@pytest.fixture
def authenticated_client_factory(db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    def _make(user):
        settings = Settings()
        app = create_app(settings=settings)
        return _ctx(app, settings, user)
    return _make


async def _seed(db: AsyncSession) -> User:
    u = User(oidc_subject="m1", email="m1@x.com", display_name="M1")
    db.add(u); await db.flush()
    t = Trip(title="Paris", start_date=date(2026, 6, 1), end_date=date(2026, 6, 7),
             created_by=u.id)
    db.add(t); await db.flush()
    db.add(TripTraveler(trip_id=t.id, user_id=u.id, role="owner"))
    db.add(Segment(
        trip_id=t.id, owner_user_id=u.id, type="flight", status="confirmed",
        provider="Air France",
        start_at=datetime(2026, 6, 1, 13, tzinfo=UTC), start_tz="UTC",
        end_at=datetime(2026, 6, 1, 22, tzinfo=UTC), end_tz="Europe/Paris",
        start_location={"iata": "JFK", "city": "New York"},
        end_location={"iata": "CDG", "city": "Paris"},
        details={"flight_number": "AF007"},
        parse_source="manual", parse_confidence=1.0,
    ))
    await db.commit()
    return u


@pytest.mark.asyncio
async def test_anonymous_request_401(db_url, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
    ):
        r = await c.get("/map", follow_redirects=False)
    assert r.status_code in (401, 302, 303)


@pytest.mark.asyncio
async def test_authed_request_200(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    u = await _seed(db_session)
    async with authenticated_client_factory(u) as c:
        r = await c.get("/map")
    assert r.status_code == 200
    # Page renders Leaflet bootstrap + map-data JSON blob
    assert "leaflet" in r.text.lower()
    assert 'id="map-data"' in r.text
    assert "JFK" in r.text or "New York" in r.text  # marker for the flight origin


@pytest.mark.asyncio
async def test_other_users_segments_excluded(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    u_self = await _seed(db_session)
    other = User(oidc_subject="m2", email="m2@x.com", display_name="M2")
    db_session.add(other); await db_session.flush()
    other_trip = Trip(title="Berlin", start_date=date(2026, 7, 1),
                      end_date=date(2026, 7, 5), created_by=other.id)
    db_session.add(other_trip); await db_session.flush()
    db_session.add(TripTraveler(trip_id=other_trip.id, user_id=other.id, role="owner"))
    db_session.add(Segment(
        trip_id=other_trip.id, owner_user_id=other.id, type="flight",
        status="confirmed", provider="Lufthansa",
        start_at=datetime(2026, 7, 1, 9, tzinfo=UTC), start_tz="UTC",
        start_location={"iata": "JFK"},
        end_location={"iata": "BER"},
        details={"flight_number": "LH401"},
        parse_source="manual", parse_confidence=1.0,
    ))
    await db_session.commit()

    async with authenticated_client_factory(u_self) as c:
        r = await c.get("/map")
    assert "LH401" not in r.text
    assert "BER" not in r.text
```

### Step 4.2 — Run failing

```bash
uv run pytest tests/test_routes_map_lifetime.py -v
# Expect: 404 on /map; route module not yet created
```

### Step 4.3 — Implement `routes/map.py`

`src/trip_tracker/routes/map.py`:

```python
"""Map routes: lifetime atlas (/map) and per-trip view (/trips/<id>/map)."""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
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
    "#3b82f6", "#10b981", "#f59e0b", "#ef4444",
    "#8b5cf6", "#ec4899", "#14b8a6", "#f97316",
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

    # Group by trip for color assignment
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
            markers.append({
                "lat": pt[0], "lon": pt[1],
                "trip_id": str(trip.id), "trip_title": trip.title,
                "color": color,
            })

        if seg.type == "flight" and start_pt and end_pt:
            arcs.append({
                "points": great_circle_points(start_pt, end_pt, n_points=50),
                "color": color,
                "trip_id": str(trip.id),
            })

    payload = json.dumps({"markers": markers, "arcs": arcs})
    return templates.TemplateResponse(
        request,
        "map/all_trips.html",
        {"user": user, "map_data_json": payload},
    )
```

### Step 4.4a — Add `head_extra` block + Map nav link to `base.html` FIRST

(This must happen before the new templates extend `base.html` — otherwise Jinja2 fails on the `{% block head_extra %}` reference in child templates.)

In `src/trip_tracker/templates/base.html`:

1. Inside `<head>...</head>`, add a `head_extra` block so child templates can inject Leaflet CSS:

```html
<head>
  ...existing meta/title/css...
  {% block head_extra %}{% endblock %}
</head>
```

2. Add `Map` link to the navbar between `Trips` and `Inbox`:

```html
<a href="/trips" class="hover:underline">Trips</a>
<a href="/map" class="hover:underline">Map</a>
<a href="/inbox" class="hover:underline">Inbox</a>
```

Read the actual `base.html` first to confirm exact insertion points; insert in matching style.

### Step 4.4 — `_leaflet_head.html` partial

`src/trip_tracker/templates/map/_leaflet_head.html`:

```html
<link rel="stylesheet"
      href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
      integrity="sha384-sHL9NAb7lN7rfvG5lfHpm643Xkcjzp4jFvuavGOndn6pjVqS6ny56CAt3nsEVT4H"
      crossorigin>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        integrity="sha384-cxOPjt7s7Iz04uaHJceBmS+qpjv2JkIHNVcuOrM+YHwZOmJGBXI00mdUXEq65HTH"
        crossorigin></script>
```

**SRI hashes:** generate per `feedback_dependency-currency.md` — `curl -s https://unpkg.com/leaflet@1.9.4/dist/leaflet.css | openssl dgst -sha384 -binary | openssl base64 -A` and `curl -s https://unpkg.com/leaflet@1.9.4/dist/leaflet.js | ...`. Verify both files actually load 200 OK on unpkg before committing the hashes — Phase 4's Alpine.js 3.5.11 incident taught this.

### Step 4.5 — `all_trips.html` template

`src/trip_tracker/templates/map/all_trips.html`:

```html
{% extends "base.html" %}
{% block title %}Map · trip-tracker{% endblock %}
{% block head_extra %}
  {% include "map/_leaflet_head.html" %}
  <style>
    #map { width: 100%; height: calc(100vh - 4rem); }
  </style>
{% endblock %}
{% block content %}
  <div id="map"></div>
  <script id="map-data" type="application/json">{{ map_data_json | safe }}</script>
  <script>
    (function () {
      const data = JSON.parse(document.getElementById('map-data').textContent);
      const map = L.map('map', { worldCopyJump: true }).setView([20, 0], 2);
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> · '
                   + 'Geocoding © <a href="https://www.geonames.org/">GeoNames</a> (CC BY 4.0)',
        maxZoom: 19,
      }).addTo(map);

      // Markers
      data.markers.forEach(m => {
        L.circleMarker([m.lat, m.lon], {
          radius: 5, color: m.color, fillColor: m.color, fillOpacity: 0.8,
          weight: 1.5,
        }).bindPopup(
          `<strong>${escapeHtml(m.trip_title)}</strong><br>`
          + `<a href="/trips/${m.trip_id}">View trip</a>`
        ).addTo(map);
      });

      // Arcs
      data.arcs.forEach(a => {
        L.polyline(a.points, { color: a.color, weight: 2, opacity: 0.7 }).addTo(map);
      });

      function escapeHtml(s) {
        return s.replace(/[&<>"']/g, c => ({
          '&': '&amp;', '<': '&lt;', '>': '&gt;',
          '"': '&quot;', "'": '&#39;',
        }[c]));
      }
    })();
  </script>
{% endblock %}
```

`{{ map_data_json | safe }}` is acceptable here because the JSON blob is inside a `<script type="application/json">` tag — the browser doesn't execute it; only `JSON.parse(...)` does. Trip titles are inert until they hit the `escapeHtml()` helper before insertion into the DOM via `bindPopup`.

### Step 4.6 — Wire router into app

In `src/trip_tracker/app.py`:

```python
from trip_tracker.routes.map import router as map_router
app.include_router(map_router)
```

### Step 4.7 — Run + commit

```bash
uv run djlint src/trip_tracker/templates --reformat
uv run pytest tests/test_routes_map_lifetime.py -v
uv run pytest -q
uv run mypy src
uv run ruff check . && uv run ruff format --check .

git add src/trip_tracker/routes/map.py \
        src/trip_tracker/templates/map/ \
        src/trip_tracker/templates/base.html \
        src/trip_tracker/app.py \
        tests/test_routes_map_lifetime.py
git commit -m "feat(map): /map lifetime atlas with Leaflet + flight arcs"
```

**Quality bar:**
- The `<script id="map-data" type="application/json">` pattern keeps trip titles as inert JSON; never use `<script>{{ … }}</script>` with raw template expressions for user data. Spec §5.4 + §7 (XSS row).
- Leaflet's `worldCopyJump: true` lets the map smoothly pan across the antimeridian for users zooming around the world. Doesn't fix the date-line arc problem (deferred to Phase 7.x) but makes panning feel natural.
- The `escapeHtml` JS helper in the template guards against XSS in marker popups. Don't reach for a template lib for one helper.
- ruff target=py313, mypy 3.14.
- djlint must pass on the template; run `--reformat` first.
- The `_PALETTE` color cycle is intentional — 8 colors covers most users' trip counts; 9th trip wraps to color 1. Acceptable for v0.7.0.

---

## Task 5 — `/trips/<id>/map` per-trip route + weather card overlay

**Spec ref:** §5.2, §6.3.

**Depends on Task 4** (reuses `_leaflet_head.html` partial + `routes/map.py` module + similar template structure).

**Files:**
- Modify: `src/trip_tracker/routes/map.py` — add per-trip handler
- Create: `src/trip_tracker/templates/map/trip.html`
- Create: `tests/test_routes_map_per_trip.py`

### Step 5.1 — Failing tests

`tests/test_routes_map_per_trip.py`:

```python
"""GET /trips/<id>/map: per-trip view + weather card overlay."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User
from trip_tracker.weather.client import DailyForecast, Forecast


def _cookie(user, settings):
    return {"tt_session": encode_session(
        SessionPayload(user_id=user.id, oidc_subject=user.oidc_subject),
        secret=settings.session_secret.get_secret_value(),
        max_age=3600,
    )}


@asynccontextmanager
async def _ctx(app, settings, user):
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test",
            cookies=_cookie(user, settings),
        ) as c,
    ):
        yield c


@pytest.fixture
def authenticated_client_factory(db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    def _make(user):
        settings = Settings()
        app = create_app(settings=settings)
        return _ctx(app, settings, user)
    return _make


async def _seed_with_future_trip(db: AsyncSession) -> tuple[User, Trip]:
    u = User(oidc_subject="t1", email="t1@x.com", display_name="T1")
    db.add(u); await db.flush()
    soon = date.today() + timedelta(days=5)
    t = Trip(title="Paris", start_date=soon, end_date=soon + timedelta(days=6),
             created_by=u.id)
    db.add(t); await db.flush()
    db.add(TripTraveler(trip_id=t.id, user_id=u.id, role="owner"))
    db.add(Segment(
        trip_id=t.id, owner_user_id=u.id, type="flight", status="confirmed",
        start_at=datetime.combine(soon, datetime.min.time()).replace(hour=13, tzinfo=UTC),
        start_tz="UTC",
        start_location={"iata": "JFK"},
        end_location={"iata": "CDG"},
        details={"flight_number": "AF007"},
        parse_source="manual", parse_confidence=1.0,
    ))
    await db.commit()
    return u, t


@pytest.mark.asyncio
async def test_per_trip_renders_with_cached_weather(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    u, t = await _seed_with_future_trip(db_session)
    cached = Forecast(
        lat=49.01, lon=2.55, timezone="Europe/Paris",
        days=[DailyForecast(date=date.today(), temp_max_c=22.0, temp_min_c=14.0,
                             weather_code=1, precip_prob=10)],
    )
    with patch("trip_tracker.routes.map.get_cached", AsyncMock(return_value=cached)):
        async with authenticated_client_factory(u) as c:
            r = await c.get(f"/trips/{t.id}/map")
    assert r.status_code == 200
    assert "AF007" in r.text or "JFK" in r.text
    # Weather card: explicit temperature value (NOT "Paris" — that's also the trip title)
    assert "22.0" in r.text or "22°C" in r.text or '"temp_max_c": 22' in r.text


@pytest.mark.asyncio
async def test_per_trip_cold_cache_enqueues_refresh(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    u, t = await _seed_with_future_trip(db_session)
    enq = AsyncMock()
    with (
        patch("trip_tracker.routes.map.get_cached", AsyncMock(return_value=None)),
        patch("trip_tracker.routes.map._enqueue_weather_refresh", enq),
    ):
        async with authenticated_client_factory(u) as c:
            r = await c.get(f"/trips/{t.id}/map")
    assert r.status_code == 200
    enq.assert_awaited()  # one or more destinations triggered a refresh


@pytest.mark.asyncio
async def test_per_trip_404_for_non_traveler(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    _u, t = await _seed_with_future_trip(db_session)
    other = User(oidc_subject="t2", email="t2@x.com", display_name="T2")
    db_session.add(other); await db_session.commit()
    async with authenticated_client_factory(other) as c:
        r = await c.get(f"/trips/{t.id}/map")
    assert r.status_code in (403, 404)


@pytest.mark.asyncio
async def test_past_trip_skips_weather_fetch(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    """Trip start > 14 days in past → no weather card, no enqueue."""
    u = User(oidc_subject="t3", email="t3@x.com", display_name="T3")
    db_session.add(u); await db_session.flush()
    long_ago = date.today() - timedelta(days=100)
    t = Trip(title="OldTrip", start_date=long_ago, end_date=long_ago + timedelta(days=3),
             created_by=u.id)
    db_session.add(t); await db_session.flush()
    db_session.add(TripTraveler(trip_id=t.id, user_id=u.id, role="owner"))
    db_session.add(Segment(
        trip_id=t.id, owner_user_id=u.id, type="flight", status="confirmed",
        start_at=datetime.combine(long_ago, datetime.min.time()).replace(tzinfo=UTC),
        start_tz="UTC",
        start_location={"iata": "JFK"}, end_location={"iata": "CDG"},
        details={"flight_number": "AF1"},
        parse_source="manual", parse_confidence=1.0,
    ))
    await db_session.commit()

    enq = AsyncMock()
    get_c = AsyncMock(return_value=None)
    with (
        patch("trip_tracker.routes.map.get_cached", get_c),
        patch("trip_tracker.routes.map._enqueue_weather_refresh", enq),
    ):
        async with authenticated_client_factory(u) as c:
            r = await c.get(f"/trips/{t.id}/map")
    assert r.status_code == 200
    enq.assert_not_awaited()
    get_c.assert_not_awaited()
```

### Step 5.2 — Run failing

```bash
uv run pytest tests/test_routes_map_per_trip.py -v
# Expect: 404 on /trips/{id}/map; route not yet defined
```

### Step 5.3 — Extend `routes/map.py`

Append the per-trip handler + helpers (and ensure `get_settings` is in the existing imports at the top of `routes/map.py` from Task 4 — `from trip_tracker.auth.deps import get_settings, require_user`):

```python
from datetime import date, timedelta

from saq import Queue

from trip_tracker.auth.deps import get_settings  # add to existing imports
from trip_tracker.config import Settings
from trip_tracker.parsers.enrich import haversine_km
from trip_tracker.weather.cache import get_cached
from trip_tracker.weather.client import Forecast


_GROUND_GATE_KM = 500.0
_WEATHER_FUTURE_DAYS = 14


async def _enqueue_weather_refresh(
    queue: Queue, lat: float, lon: float
) -> None:
    """Fire-and-forget saq enqueue for refresh_weather. Dedupes via key."""
    await queue.enqueue(
        "refresh_weather",
        lat=lat, lon=lon,
        unique=True,
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
    # Auth: traveler-scoped
    is_traveler = (await db.execute(
        select(TripTraveler.user_id).where(
            TripTraveler.trip_id == trip_id, TripTraveler.user_id == user.id
        )
    )).scalar_one_or_none() is not None
    if not is_traveler:
        raise HTTPException(status_code=404, detail="Not found")

    trip = (await db.execute(
        select(Trip).where(Trip.id == trip_id)
    )).scalar_one()
    segments = (await db.execute(
        select(Segment).where(Segment.trip_id == trip_id).order_by(Segment.start_at)
    )).scalars().all()

    # Resolve points + build markers in chronological order
    markers: list[dict[str, Any]] = []
    arcs: list[dict[str, Any]] = []
    ground_polyline_points: list[tuple[float, float]] = []
    last_non_flight_pt: tuple[float, float] | None = None
    last_non_flight_start_at = None

    seq = 0
    for seg in segments:
        s_pt = resolve_point(seg.start_location)
        e_pt = resolve_point(seg.end_location)

        for pt in (s_pt, e_pt):
            if pt is None:
                continue
            seq += 1
            markers.append({
                "lat": pt[0], "lon": pt[1],
                "seq": seq,
                "label": f"{seg.type} · {seg.provider or ''}".strip(" ·"),
                "start_at": seg.start_at.isoformat() if seg.start_at else None,
            })

        if seg.type == "flight" and s_pt and e_pt:
            arcs.append({
                "points": great_circle_points(s_pt, e_pt, n_points=50),
                "color": "#3b82f6",
            })

        # Ground polyline gate: same-day OR <500km between consecutive non-flight segs
        if seg.type != "flight" and s_pt:
            if last_non_flight_pt is not None and last_non_flight_start_at is not None:
                same_day = (
                    seg.start_at and last_non_flight_start_at
                    and seg.start_at.date() == last_non_flight_start_at.date()
                )
                close_enough = haversine_km(last_non_flight_pt, s_pt) < _GROUND_GATE_KM
                if same_day or close_enough:
                    ground_polyline_points.append(last_non_flight_pt)
                    ground_polyline_points.append(s_pt)
            last_non_flight_pt = e_pt or s_pt
            last_non_flight_start_at = seg.start_at

    # Weather: only if trip start_date is within ±14 days of today
    today = date.today()
    weather_horizon_max = today + timedelta(days=_WEATHER_FUTURE_DAYS)
    weather_cards: list[dict[str, Any]] = []
    if today <= trip.end_date and trip.start_date <= weather_horizon_max:
        # Unique destinations with future-or-active segments
        unique_pts: dict[tuple[float, float], str] = {}
        for seg in segments:
            if seg.start_at and seg.start_at.date() < today - timedelta(days=1):
                continue
            for pt, loc in ((resolve_point(seg.start_location), seg.start_location),
                            (resolve_point(seg.end_location), seg.end_location)):
                if pt is None:
                    continue
                # Round to 2 decimals matching cache key
                key = (round(pt[0], 2), round(pt[1], 2))
                city = (loc or {}).get("city") if loc else None
                if key not in unique_pts and city:
                    unique_pts[key] = city

        from redis.asyncio import Redis as AsyncRedis
        redis = AsyncRedis.from_url(settings.redis_url)
        try:
            queue = Queue.from_url(settings.redis_url)
            try:
                for (lat, lon), city in unique_pts.items():
                    cached: Forecast | None = await get_cached(lat, lon, redis)
                    if cached is None:
                        await _enqueue_weather_refresh(queue, lat, lon)
                        weather_cards.append({
                            "lat": lat, "lon": lon, "city": city, "loading": True,
                        })
                    else:
                        weather_cards.append({
                            "lat": lat, "lon": lon, "city": city, "loading": False,
                            "days": [d.model_dump(mode="json") for d in cached.days],
                            "timezone": cached.timezone,
                        })
            finally:
                await queue.disconnect()
        finally:
            await redis.aclose()

    payload = json.dumps({
        "markers": markers,
        "arcs": arcs,
        "ground": ground_polyline_points,
        "weather": weather_cards,
    })
    return templates.TemplateResponse(
        request,
        "map/trip.html",
        {"user": user, "trip": trip, "map_data_json": payload},
    )
```

### Step 5.4 — `trip.html` template

`src/trip_tracker/templates/map/trip.html`:

```html
{% extends "base.html" %}
{% block title %}{{ trip.title }} · Map · trip-tracker{% endblock %}
{% block head_extra %}
  {% include "map/_leaflet_head.html" %}
  <style>
    #map { width: 100%; height: calc(100vh - 4rem); }
    .weather-card { font-size: 0.8rem; min-width: 12rem; }
    .weather-card .day { display: inline-block; margin-right: 0.5rem; }
  </style>
{% endblock %}
{% block content %}
  <div class="px-4 py-2 flex justify-between items-center">
    <div>
      <strong>{{ trip.title }}</strong>
      <span class="text-zinc-500 text-sm">{{ trip.start_date }} – {{ trip.end_date }}</span>
    </div>
    <a href="/trips/{{ trip.id }}" class="text-sm underline">← Back to trip</a>
  </div>
  <div id="map"></div>
  <script id="map-data" type="application/json">{{ map_data_json | safe }}</script>
  <script>
    (function () {
      const data = JSON.parse(document.getElementById('map-data').textContent);
      const map = L.map('map');
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> · '
                   + 'Geocoding © <a href="https://www.geonames.org/">GeoNames</a> · '
                   + 'Weather © <a href="https://open-meteo.com/">Open-Meteo</a>',
        maxZoom: 19,
      }).addTo(map);

      // Numbered markers with weather popup
      const weatherByPoint = {};
      data.weather.forEach(w => {
        const k = `${w.lat.toFixed(2)},${w.lon.toFixed(2)}`;
        weatherByPoint[k] = w;
      });

      const bounds = [];
      data.markers.forEach(m => {
        const k = `${m.lat.toFixed(2)},${m.lon.toFixed(2)}`;
        const w = weatherByPoint[k];
        const marker = L.marker([m.lat, m.lon])
          .bindTooltip(`${m.seq}. ${m.label}`)
          .addTo(map);
        if (w) {
          marker.bindPopup(renderWeatherCard(w));
        }
        bounds.push([m.lat, m.lon]);
      });

      // Flight arcs (great-circle)
      data.arcs.forEach(a => {
        L.polyline(a.points, { color: a.color, weight: 2, opacity: 0.8 }).addTo(map);
      });

      // Ground polyline (gated to same-day or <500km)
      if (data.ground.length >= 2) {
        for (let i = 0; i < data.ground.length; i += 2) {
          L.polyline([data.ground[i], data.ground[i + 1]], {
            color: '#10b981', weight: 1.5, opacity: 0.6, dashArray: '5,5',
          }).addTo(map);
        }
      }

      if (bounds.length) map.fitBounds(bounds, { padding: [40, 40] });
      else map.setView([20, 0], 2);

      function renderWeatherCard(w) {
        if (w.loading) {
          return `<div class="weather-card"><strong>${escapeHtml(w.city)}</strong><br>`
                 + `Loading weather…</div>`;
        }
        const html = w.days.map(d => {
          return `<span class="day"><strong>${d.date.slice(5)}</strong> `
               + `${Math.round(d.temp_max_c)}°/${Math.round(d.temp_min_c)}°</span>`;
        }).join('');
        return `<div class="weather-card"><strong>${escapeHtml(w.city)}</strong><br>`
               + `<small>${escapeHtml(w.timezone)}</small><br>${html}</div>`;
      }

      function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, c => ({
          '&': '&amp;', '<': '&lt;', '>': '&gt;',
          '"': '&quot;', "'": '&#39;',
        }[c]));
      }
    })();
  </script>
{% endblock %}
```

### Step 5.5 — Run + commit

```bash
uv run djlint src/trip_tracker/templates --reformat
uv run pytest tests/test_routes_map_per_trip.py -v
uv run pytest -q
uv run mypy src
uv run ruff check . && uv run ruff format --check .

git add src/trip_tracker/routes/map.py \
        src/trip_tracker/templates/map/trip.html \
        tests/test_routes_map_per_trip.py
git commit -m "feat(map): /trips/<id>/map per-trip view with weather cards"
```

**Quality bar:**
- The `Settings = Depends(lambda: Settings())` in the handler signature is the same pattern as Phase 5/6 routes (Settings is rebuilt per-request to pick up env changes in tests). If the project has a shared `get_settings` dep, use that instead — `from trip_tracker.auth.deps import get_settings`.
- The redis + queue clients are built per-request and closed via `try/finally`. If this becomes hot, refactor to a FastAPI dependency that reuses connection pools — but YAGNI for v0.7.0.
- The `weather:<lat:.2f>:<lon:.2f>` cache key MUST match exactly between `routes/map.py` and `weather/cache.py` — verify with the existing `test_cache_key_rounds_to_two_decimals` from Task 3.
- The weather-horizon test (`test_past_trip_skips_weather_fetch`) is critical — without the date check, every map render of a 5-year-old trip would hit Open-Meteo for nothing.
- The "loading" card is just a string in the JSON; the template renders a placeholder. On the next page refresh the cache hit shows the real data.
- The `ground_polyline_points` gate uses `last_non_flight_*` state across the loop — make sure it resets correctly on flight segments (see code: it doesn't reset, only updates on non-flight). That's intentional — a flight in the middle doesn't break the chain of "did the user drive between Paris and Lyon-Paris."

---

## Task 6 — README + navbar link

**Spec ref:** §5.5, §8.

(No test changes; the navbar link landed in Task 4. This task adds the README section and is committed inline.)

### Step 6.1 — Update README Status

In `README.md`:

```markdown
> **Status:** Phase 7 — world map + per-trip Open-Meteo weather cards.
> Phase 8 (TBD) is next.
```

### Step 6.2 — Add "Map (Phase 7)" section

Append before "Production deploy":

```markdown
## Map (Phase 7)

Two map views, both auth-gated:

- **`/map`** — Lifetime atlas. Every trip you're a traveler on, color-coded.
  Flight legs render as great-circle arcs (curving correctly over polar
  routes); other segments pin at airport coordinates or city centroids.
- **`/trips/<id>/map`** — Per-trip view with **weather cards** for upcoming
  destinations (within 14 days of today). Cards show 7-day daily highs/lows
  via Open-Meteo (free, keyless). Cold cache renders a "loading…" placeholder
  and triggers a background refresh; the next page load shows the real data.

### Data sources

- **Tiles:** OpenStreetMap (CC BY-SA, attribution shown). Heavy public deployments
  should swap to a self-hosted tile server.
- **Airports:** IATA codes resolve via the bundled `airports.csv` (Phase 3).
- **Cities:** Bundled, filtered GeoNames cities-1000 (CC BY 4.0, attribution
  shown). ~150k cities with population ≥ 1000.
- **Weather:** Open-Meteo (no key, no signup). Forecasts cached in Redis 1h.

### Known limitations (v0.7.0)

- **Pacific routes** that cross ±180° longitude (e.g., LAX→SYD) draw the
  "wrong way" because Leaflet's default polyline doesn't split at the
  antimeridian. Phase 7.x adds the standard fix.
- **City disambiguation** falls back to highest-population for ambiguous
  names (e.g., "Paris" without a country code → Paris, France). Per-segment
  location overrides are deferred to Phase 7.x.
- **No imperial-unit toggle** for weather cards — temperatures display in
  Celsius. Phase 7.x can add `?units=imperial` or a per-user setting.

### Refreshing the cities-1000 bundle

The bundled `cities1000.tsv` was filtered from GeoNames `cities1000.txt` to
6 columns (~7-10 MB). To refresh:

\`\`\`
uv run python scripts/_make_cities_data.py
\`\`\`

This downloads the latest from <https://download.geonames.org/export/dump/>
and rewrites the bundled TSV. Commit the result.
```

### Step 6.3 — Commit

```bash
git add README.md
git commit -m "docs: README — Phase 7 Map + Weather section"
```

**Quality bar:**
- The README's Authelia exempt-path section from Phase 6 is unchanged — `/map` and `/trips/<id>/map` go through the standard session auth, no exemption needed.
- The CC BY 4.0 attribution to GeoNames is **also** baked into the Leaflet tile-layer config; the README repeats it for completeness. Both must remain.

---

## Task 7 — Verification gate + Playwright smoke + tag v0.7.0

(Inline. Same shape as v0.5.0 / v0.6.0 ship.)

### Step 7.1 — Local verification gate

```bash
./scripts/build-tailwind.sh
uv run pytest --cov                          # ≥85%
uv run ruff check src tests migrations
uv run ruff format --check .                 # whole tree
uv run mypy src
uv run pre-commit run --all-files
uv run bandit -c pyproject.toml -r src/
uv run djlint src/trip_tracker/templates --check
docker build -t trip-tracker:dev .
```

All must be green.

### Step 7.2 — Smoke test (manual, ad-hoc per v0.6.0 recipe)

1. Boot Postgres + Redis on alternate ports (5433, 6380) via `docker run`. (Meili and pdfplumber not strictly needed for Phase 7 smoke; boot them too if you want full coverage.)
2. Run migrations against the alt DB.
3. Seed an admin user + a trip with at least one flight (JFK→CDG) and a hotel in Paris.
4. Boot the app: `uv run uvicorn 'trip_tracker.app:create_app' --factory --host 127.0.0.1 --port 8765`.
5. Mint a session cookie via the same one-shot Python script Phase 5/6 used.
6. **curl smoke:**
   - `curl -I http://127.0.0.1:8765/map` (with cookie) → 200, `text/html`.
   - `curl http://127.0.0.1:8765/trips/<id>/map` → 200; grep for `JFK`, `CDG`, `AF007`, `Paris`.
7. **Playwright smoke (real browser):**
   - Navigate to `/trips/<id>/map`, paste session cookie via DevTools.
   - Verify the map renders (Leaflet tile request to `tile.openstreetmap.org`).
   - Verify a numbered marker exists at JFK and CDG.
   - Verify a flight arc connects them (Leaflet draws an `<svg>` polyline).
   - Click the CDG marker → weather card popup opens with city name + temps (or "Loading weather…" on cold cache).
   - Wait 5s; refresh; verify the cold-cache card now shows real data (the saq worker fetched it).
8. **Tear down containers.**

### Step 7.3 — Commit, tag, push

```bash
git tag -a -s v0.7.0 -m "Phase 7 — World map + Open-Meteo weather

- /map (lifetime atlas) + /trips/<id>/map (per-trip + weather cards)
- Bundled GeoNames cities-1000 (filtered ~7-10MB) + great-circle arcs
- Open-Meteo client + Redis cache + refresh_weather saq task
- Re-uses parsers/enrich.py Airport @dataclass + haversine_km
- README documents Pacific-arc + city-disambiguation known limits"

git checkout main
git merge --ff-only feat/phase-7-map-weather
git push origin main
git push origin v0.7.0
```

The release workflow on GitHub fires on the tag push, producing a multi-arch image at `ghcr.io/<owner>/trip-tracker:v0.7.0`, signed with cosign + SBOM.

### Step 7.4 — Schedule release-verification agent

Same pattern as v0.5.0 / v0.6.0: schedule a one-time remote agent ~20 min after tag push to verify GHCR image, signature, SBOM. Reuse the prompt template from prior tags, swapping `v0.6.0` → `v0.7.0`.

**Quality bar:**
- Coverage ≥85% AFTER all 7 tasks land.
- The full `ruff format --check .` (whole tree) must pass.
- Bandit clean. Any `# nosec` includes the specific code with a one-line reason.
- Signed tag uses your SSH signing key (already configured per Phase 2).
- Smoke test must verify the cold-cache → next-render cycle works end-to-end (not just the happy path with pre-warmed Redis).

---

## Done Definition for Phase 7

- All 7 tasks merged to `main`.
- CI green (lint + typecheck + test + security + docker + djlint + bandit).
- Coverage ≥ 85%.
- New `geo/` subpackage: cities lookup, great-circle interpolation, resolve_point dispatcher (airports re-uses parsers/enrich).
- Filtered `cities1000.tsv` (~7–10 MB) bundled at `static/data/`; filter script committed.
- New `weather/` subpackage: Open-Meteo client + Redis cache.
- `worker.py`'s `settings = {...}` dict has `refresh_weather` in `functions`; `startup()` adds `ctx["redis"]`; `shutdown()` closes it.
- `GET /map` returns lifetime atlas: traveler-scoped segments pinned, flights arc, no weather.
- `GET /trips/<id>/map` returns per-trip view: numbered chronological markers, flight arcs, ground polyline gated to same-day OR <500 km, weather cards on future/active destinations, "Loading…" cards on cold cache.
- Both routes 401 when unauthenticated; 404 for non-traveler accessing per-trip.
- Leaflet 1.9.4 loaded via CDN with valid SRI hashes.
- OSM, GeoNames, Open-Meteo attributions visible.
- Navbar `Map` link added.
- README "Map (Phase 7)" section: subscription flow, data sources, known limitations.
- Signed `v0.7.0` tag pushed; release workflow produces signed multi-arch GHCR image; release-verification scheduled agent confirms.

After this lands, return to brainstorming/writing-plans for the next Phase. Candidates: expense tracking with frozen FX (Frankfurter ECB), OCR (Phase 5.1), S3 storage backend (Phase 5.2), Pacific-arc fix (Phase 7.1).
