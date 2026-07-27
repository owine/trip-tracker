# Phase 7 — World Map + Weather Design

**Status:** Approved (brainstorm 2026-05-01, owine + Claude).
**Target tag:** `v0.7.0`.
**Predecessors:** Phase 1–6 (auth, ingestion, parsers, search, documents, ICS feed).
**Successor (sketched):** Phase 7.x — date-line wraparound fix, `/api/weather` JSON endpoint + auto-refresh, per-segment location overrides, lifetime-atlas weather, imperial-unit toggle.

---

## 1. Goal

Add two new map views — `/map` (lifetime atlas of every trip) and `/trips/<id>/map` (per-trip route with weather) — using Leaflet + OpenStreetMap tiles. Per-destination Open-Meteo weather cards on the per-trip view answer "what should I pack." Geographic lookups are bundled-data-driven (no runtime geocoding API calls).

**Out of scope for v0.7.0:** date-line wraparound for Pacific arcs, `/api/weather` JSON endpoint, per-segment location overrides, lifetime-atlas weather, imperial-unit toggle, self-hosted tile server, animated trip-reveal, trip-overlap detection.

---

## 2. Scope decisions (locked during brainstorm)

| # | Decision | Choice |
|---|---|---|
| 1 | Phase 7 candidate | World map + Open-Meteo weather (vs expenses, OCR, S3) |
| 2 | Map trip scope | Both `/map` (lifetime) + `/trips/<id>/map` (per-trip) |
| 3 | Non-flight pin strategy | City-centroid fallback via bundled GeoNames cities-1000 (no runtime API) |
| 4 | Flight visualization | Great-circle arcs (~30 lines stdlib math) |
| 5 | Weather card scope | Per-destination cards on per-trip view; lifetime view has no weather |

---

## 3. Architecture overview

```
┌──────────────────────┐    GET /map   /trips/<id>/map     ┌──────────────────┐
│ Browser (Leaflet)    │ ─────────────────────────────────► │  routes/map.py   │
│ + OSM tile XHR       │                                    │  require_user    │
└─────────┬────────────┘ ◄───────── HTML + JSON  ────────── │  traveler_ids    │
          │                                                  └────────┬─────────┘
          │ tile.openstreetmap.org                                     │
          │ (free, attribution required)                                ▼
          ▼                                  ┌─────────────────────────────────┐
   ┌─────────────┐                           │  geo/resolve.py                 │
   │  OSM tiles  │                           │  - airports.py (Phase 3 CSV)    │
   └─────────────┘                           │  - cities.py (cities1000.tsv)   │
                                             │  - arcs.py (great-circle slerp) │
                                             └─────────────┬───────────────────┘
                                                           ▼
                                              segment → (lat, lng) | None
                                                           ▼
                                             [per-trip view only]
                                              ┌───────────────────────────────┐
                                              │  weather/                     │
                                              │  cache.py (Redis, 1h TTL)     │
                                              │  client.py (Open-Meteo)       │
                                              │  refresh_weather (saq task)   │
                                              └───────────────────────────────┘
```

**Public surface:** two GET routes, auth-gated. **Storage:** zero new Postgres tables — all geographic lookups read bundled files; weather cache lives in Redis. **External APIs:** OSM tiles (browser → tile server, no app proxying), Open-Meteo (saq task only — never blocks the page render).

---

## 4. Data sources

### 4.1 Airports — re-use `src/trip_tracker/parsers/enrich.py`

Phase 3's `parsers/enrich.py` already loads `airports.csv` into a `dict[str, Airport]` and exposes `get_airport(iata) -> Airport | None`. The existing `Airport` is a `@dataclass(frozen=True)` with fields: `iata`, `name`, `city`, `country`, `tz`, `lat`, `lon`. **Note: the field is `lon`, not `lng`.**

Phase 7 imports directly:

```python
from trip_tracker.parsers.enrich import Airport, get_airport, haversine_km
```

No new airport loader / no `geo/airports.py` module — single source of truth. The Phase 3 module also already provides `haversine_km(a, b)` for great-circle distance, which Section 5.2's ground-polyline gate uses.

Coordinate convention throughout Phase 7 is `(lat, lon)` (matching `enrich.py`); the spec wording elsewhere should be read with `lon = lng`.

### 4.2 Cities — `src/trip_tracker/geo/cities.py`

New bundled file `src/trip_tracker/static/data/cities1000.tsv` derived from GeoNames `cities1000.txt`. License: **CC BY 4.0** (GeoNames data) — attribution shown on the map page footer.

**Source vs bundled.** GeoNames' raw `cities1000.txt` (download at https://download.geonames.org/export/dump/cities1000.zip) is tab-separated despite the `.txt` extension and contains 19 columns (~28 MB raw). Phase 7 bundles a **filtered** version — only the 6 columns we need — produced once via a `scripts/_make_cities_data.py` one-shot script. After filtering, `cities1000.tsv` is ~7–10 MB and contains:

```
name <TAB> asciiname <TAB> country_code <TAB> population <TAB> lat <TAB> lon
```

(Lon, not lng, matching `enrich.py`.) The filter script is committed for reproducibility but not run at build time.

```python
@dataclass(frozen=True)
class City:
    name: str
    asciiname: str
    country_code: str  # ISO 3166-1 alpha-2
    population: int
    lat: float
    lon: float


def lookup_city(name: str, country: str | None = None) -> City | None:
    """Returns the highest-population match. If `country` is provided, restricts
    to that country code first; falls back to global highest-pop on no match.
    """
```

**Disambiguation strategy:** "Paris" with no country hint → returns Paris/FR (population 2.1M, highest). "Paris" with `country="US"` → returns Paris/Texas. Most segments have `country` set by the parser, so disambiguation is rarely ambiguous; when it is, the highest-pop fallback is the right answer ~99% of the time.

**Memory:** ~12–15 MB resident after parsing (the asciiname + country_code index doubles the raw text footprint). Acceptable.

**Loader:** parses TSV at import time, builds two indexes — exact-name → list of cities, and (asciiname, country_code) → city. The first lookup hit returns the highest-population match.

**Why `@dataclass(frozen=True)` not `NamedTuple`:** matches `enrich.Airport` style — single convention across `geo/`.

### 4.3 Great-circle arcs — `src/trip_tracker/geo/arcs.py`

Pure stdlib math, ~30 lines:

```python
def great_circle_points(
    start: tuple[float, float],
    end: tuple[float, float],
    n_points: int = 50,
) -> list[tuple[float, float]]:
    """Interpolate n_points along the great-circle arc between two lat/lng pairs.

    Uses spherical linear interpolation (slerp) on the unit sphere. Returns
    a list of [lat, lng] suitable for Leaflet's L.polyline. The arc closely
    follows the actual flight path; a 50-point line over JFK→HND looks
    correctly curved in any standard tile projection (Web Mercator, etc).
    """
```

Implementation: convert to 3D unit-sphere vectors, slerp at uniformly-spaced `t ∈ [0, 1]`, convert back to lat/lng. ~50 points × 8 bytes × 2 floats ≈ 800 bytes per arc — Leaflet renders smoothly with hundreds of arcs.

**Date-line caveat:** Pacific routes (LAX→SYD, NRT→LAX) cross ±180° longitude; Leaflet's default polyline rendering connects consecutive points with a "wrong way" line that wraps around the world. v0.7.0 ships with this limitation; document in README. Phase 7.x adds the standard fix (split the arc at the antimeridian, render as two segments).

### 4.4 Resolution dispatcher — `src/trip_tracker/geo/resolve.py`

```python
def resolve_point(loc: dict[str, Any] | None) -> tuple[float, float] | None:
    """Pick the best (lat, lng) for a JSONB location dict, in priority order:
    1. iata → airports lookup
    2. (city, country) → cities1000 lookup
    3. None
    """
```

The map handler iterates `Segment.start_location` and `Segment.end_location` through `resolve_point` to build the markers list.

---

## 5. Map page implementation — `src/trip_tracker/routes/map.py`

### 5.1 `GET /map` — Lifetime atlas

Auth-gated; queries all segments via `traveler_ids` join (mirrors search proxy + ICS feed). Resolves each segment's start/end points. Renders a Leaflet map showing:

- **Markers** at every resolved point. Duplicate visits to the same airport collapse into one marker; the popup lists the trips that touched it.
- **Flight arcs** between every flight's start_location.iata and end_location.iata, rendered as great-circle polylines. Color-coded by trip via a small palette (cycle through 6–8 colors).
- **No weather card** — historical/lifetime view doesn't need forecasts.

Marker popups: trip title(s), date(s), link back to `/trips/<id>`.

JSON marshaling pattern:

```html
<script id="map-data" type="application/json">{ "markers": [...], "arcs": [...] }</script>
<script>
  const data = JSON.parse(document.getElementById('map-data').textContent);
  // Leaflet init reads data.markers + data.arcs
</script>
```

This keeps user-supplied trip titles in JSON (parsed via `JSON.parse`) rather than `<script>`-embedded JS, avoiding XSS.

### 5.2 `GET /trips/<trip_id>/map` — Per-trip view

Same handler shape, scoped to one trip. Renders:

- **Numbered markers** for every resolved segment in trip order (1, 2, 3, ...) so users see the chronological path.
- **Flight arcs** for the trip's flights only.
- **Polyline** between consecutive non-flight segments — gated to **same-day OR within 500 km** transitions only, using `haversine_km` from `parsers/enrich.py`. This prevents drawing a transcontinental "ground" line between Paris hotel night 3 and a Berlin hotel on day 4 (which would imply they drove). When the gate fails, no line is drawn — the geographic gap is the user's clue that they flew or trained between those points.
- **Per-destination weather cards** overlaid on the map — one per unique (lat, lng) where the segment is in the future or "active" (start_at within the next 14 days). Past segments don't show weather.

Weather cards render as Leaflet popups attached to the matching marker, opened on hover or click. **NOT** separate floating elements that need repositioning on pan/zoom.

### 5.3 Page layout

Full-screen map (full viewport height/width) with overlay UI:

- Top-left: trip title + date range (per-trip) or "Your Trips" (lifetime).
- Top-right: link back to `/trips/<id>` (per-trip) or `/trips` (lifetime).
- Bottom-right: attribution corner (OSM + GeoNames + Open-Meteo).

### 5.4 Leaflet wiring

CDN-loaded with SRI (matches Alpine.js pattern from Phase 4):

```html
<link rel="stylesheet"
      href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
      integrity="sha384-…" crossorigin>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        integrity="sha384-…" crossorigin></script>
```

OpenStreetMap tile layer (free, attribution required):

```js
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> · '
             + 'Geocoding © <a href="https://www.geonames.org/">GeoNames</a> (CC BY 4.0)',
  maxZoom: 19,
}).addTo(map);
```

OSM's tile-usage policy permits a single self-hoster well under their 1M-tile-per-day fair-use threshold. Heavy public deployments swap to a self-hosted tile server in a Phase 7.x.

### 5.5 Navbar link

In `templates/base.html`, add between `Trips` and `Inbox`:

```html
<a href="/map" class="hover:underline">Map</a>
```

---

## 6. Weather subsystem

### 6.1 Open-Meteo client — `src/trip_tracker/weather/client.py`

Free, keyless. Single endpoint:

```
GET https://api.open-meteo.com/v1/forecast
  ?latitude=<lat>&longitude=<lng>
  &daily=temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max
  &timezone=auto
  &forecast_days=7
```

Returns 7-day daily forecast (~1 KB JSON).

```python
class DailyForecast(BaseModel):
    date: date
    temp_max_c: float
    temp_min_c: float
    weather_code: int  # WMO code; mapped to emoji + label client-side
    precip_prob: int  # 0-100


class Forecast(BaseModel):
    lat: float
    lng: float
    timezone: str
    days: list[DailyForecast]


async def fetch_forecast(lat: float, lng: float) -> Forecast:
    """Single Open-Meteo HTTP call. ~200 ms typical latency. 10s timeout."""
```

httpx is already in deps from Phase 4. On HTTP error (5xx, timeout) → raises; the caller decides whether to surface or skip the card.

### 6.2 Redis cache — `src/trip_tracker/weather/cache.py`

Forecasts are keyed by `(lat, lng)` rounded to 2 decimals (~1 km precision — plenty for a city-level forecast). Key format: `weather:<lat:.2f>:<lng:.2f>`. TTL: 3600 s (1 hour).

```python
async def get_cached(lat: float, lng: float, redis: Redis) -> Forecast | None: ...


async def set_cached(forecast: Forecast, redis: Redis) -> None: ...
```

The cache value is the JSON serialization of the `Forecast` Pydantic model. Round-trip via `Forecast.model_validate_json(...)`.

### 6.3 Page render flow

The map handler for `/trips/<id>/map` does this for each unique destination:

```python
forecasts: dict[tuple[float, float], Forecast | None] = {}
for lat, lng in unique_destinations:
    cached = await get_cached(lat, lng, redis)
    if cached is not None:
        forecasts[(lat, lng)] = cached
        continue
    # Cache miss: fire-and-forget saq refresh; render this card as "loading"
    await queue.enqueue(
        "refresh_weather",
        lat=lat,
        lng=lng,
        unique=True,
        key=f"weather:{lat:.2f}:{lng:.2f}",
    )
    forecasts[(lat, lng)] = None
```

Page render p99 stays under ~50 ms even on cold cache. Stale destinations show a "loading…" card; the saq job fetches in the background; the user sees the forecast on the next refresh.

### 6.4 saq task — `refresh_weather`

Registered by appending to the `functions` list in `worker.py`'s `settings = {...}` dict (the existing pattern shipped in Phase 4's saq migration; there is no `WorkerSettings` class):

```python
async def refresh_weather(ctx: dict[str, Any], *, lat: float, lng: float) -> None:
    """Fetch from Open-Meteo and write to Redis. Idempotent."""
    redis = ctx["redis"]
    forecast = await fetch_forecast(lat, lng)
    await set_cached(forecast, redis)
```

The saq dedup key (`weather:<lat:.2f>:<lng:.2f>`) prevents thundering-herd refresh on cache expiry — concurrent map renders for the same destination produce one network call.

### 6.5 Failure modes

- **Open-Meteo down:** `fetch_forecast` raises; the saq task fails; the card stays "loading" until the next attempt. Page render is unaffected. Log at INFO, not ERROR.
- **Cache key sharing across users:** the cache key is `weather:<lat>:<lng>` — multiple users' map renders share the same cache slot, which is desired (Paris forecast is the same regardless of who's asking). No PII leak.
- **Unit ambiguity:** Open-Meteo returns Celsius. Render shows `°C` explicitly; no toggle in v0.7.0. Phase 7.x can add `?units=imperial`.

### 6.6 What's NOT in v0.7.0

- `/api/weather?lat=…&lng=…` JSON endpoint — page server-renders forecasts; cold-cache cards show "loading…" until next refresh. Phase 7.x can add the JSON endpoint + Alpine.js polling for snappier UX.
- Hourly forecasts — daily is plenty for trip planning.
- Historical weather (e.g., "what was the weather like in Paris last year") — Open-Meteo offers it; v0.7.0 doesn't surface it.

---

## 7. Threat model

| Risk | Mitigation |
|---|---|
| Map data leaks via the URL/page | Both routes are `require_user`-gated and `traveler_ids`-scoped, identical to search/ICS semantics. |
| OSM tile-policy violation | Single self-hoster is well below fair-use; README documents the limit + recommends self-hosted tiles for heavy public deployments. |
| GeoNames license violation | Attribution baked into the tile-layer config; README credits GeoNames. |
| XSS via trip titles in marker popups | All trip-title strings flow through `<script type="application/json">` blob → `JSON.parse` → Leaflet API. No `<script>`-embedded JS templating. |
| Open-Meteo abuse / rate-limit hit | saq dedup key collapses concurrent fetches; 1h cache TTL caps fetches to ≤24/destination/day. Even an adversarial user mashing refresh produces ≤1 fetch/destination/hour. |
| Redis full from weather cache | Each cache entry ~1 KB; even 10k unique destinations = ~10 MB. 1h TTL means cold entries auto-evict. |
| Pacific routes look wrong | Documented as a known v0.7.0 limitation. |

---

## 8. Done definition

- [ ] New `geo/` subpackage with airports re-export, cities1000 lookup, great-circle interpolation, `resolve_point` dispatcher.
- [ ] Filtered `cities1000.tsv` (6 columns from GeoNames source) committed at `src/trip_tracker/static/data/cities1000.tsv`; size ≤10 MB. Filter script `scripts/_make_cities_data.py` committed for reproducibility but not run at build time.
- [ ] New `weather/` subpackage: Open-Meteo client + Redis cache + `refresh_weather` saq task.
- [ ] `worker.py`'s `settings` dict has `refresh_weather` appended to its `functions` list.
- [ ] `GET /map` returns the lifetime atlas: every traveler-scoped segment pinned, flights arc, no weather.
- [ ] `GET /trips/<id>/map` returns the per-trip view: numbered chronological markers, flight arcs, ground polylines, weather cards on future/active destinations.
- [ ] Both routes 401 when unauthenticated; 404 / empty when no segments are visible to the user.
- [ ] Leaflet 1.9.4 loaded via CDN with valid SRI hashes.
- [ ] OSM, GeoNames, Open-Meteo attributions visible on the map page footer.
- [ ] Navbar `Map` link added between Trips and Inbox.
- [ ] Weather card render path: cached forecast served instantly; cold-cache shows "loading…" + triggers saq refresh; next page render shows real data.
- [ ] README "Map (Phase 7)" section: short description, OSM/GeoNames/Open-Meteo credits, date-line wraparound caveat for Pacific routes.
- [ ] 85% project-wide coverage holds. ruff + mypy + bandit + djlint + pre-commit clean.
- [ ] Signed `v0.7.0` tag pushed; release-verification scheduled agent confirms.

---

## 9. Risks & mitigations

| # | Risk | Mitigation |
|---|---|---|
| 1 | Date-line wraparound on Pacific arcs | Documented v0.7.0 limitation; Phase 7.x splits at antimeridian. |
| 2 | City disambiguation wrong (e.g., "Paris, TX") | Highest-pop fallback handles ~99% correctly; per-segment override is a Phase 7.x escape hatch. |
| 3 | Open-Meteo cold-cache UX | "Loading…" card + background saq refresh; subsequent page render shows real data. |
| 4 | OSM tile fair-use breach | Documented; self-hosted tile-server option for heavy use cases. |
| 5 | cities1000 file accidentally not committed | Tests load it from the bundled path; CI fails if missing. |
| 6 | Memory footprint creep | airports + cities tables + Leaflet payload sit at ~10 MB total resident; well within container limits. |
| 7 | Bundled CDN goes down (unpkg.com) | Map page broken; rest of app unaffected. v0.7.0 accepts this; Phase 7.x can self-host. |
| 8 | XSS via marker popup content | JSON blob + `JSON.parse` + Leaflet API path; no `<script>` injection of user data. |

---

## 10. Sequencing (rough — full plan from `writing-plans`)

| # | Task | Model | Notes |
|---|---|---|---|
| 1 | Cities lookup + bundled `cities1000.tsv` + airports re-export + `resolve_point` | haiku | Pure data + lookup helpers |
| 2 | Great-circle interpolation (`geo/arcs.py`) | haiku | Stdlib math, ~30 lines, table-driven tests |
| 3 | Open-Meteo client + Redis cache + saq task wiring | sonnet | Adds task to worker.py + httpx call |
| 4 | `/map` lifetime route + template (Leaflet init + JSON marker pipeline) | sonnet | Frontend-heavy; SRI hashes; OSM attribution |
| 5 | `/trips/<id>/map` per-trip route + template + weather card overlay | sonnet | **Depends on Task 4** (reuses its Leaflet config + base template); adds weather popups + the same-day/500km ground-polyline gate |
| 6 | README + navbar link | inline | No code surface beyond the navbar one-liner |
| 7 | Verification gate + Playwright smoke (open `/trips/<id>/map`, assert markers + arc + weather) + tag v0.7.0 | inline | Same shape as v0.5.0/v0.6.0 ship |

~7 tasks total — comparable to Phase 6 in scope.

---

## 11. Future phases (Phase 7.x)

- **Phase 7.1 — Date-line wraparound fix** for Pacific arcs.
- **Phase 7.2 — `/api/weather` JSON endpoint + Alpine.js polling** for cold-cache UX.
- **Phase 7.3 — Per-segment location overrides** for city-disambiguation edge cases.
- **Phase 7.4 — Lifetime atlas weather** for the upcoming-trip pin on `/map`.
- **Phase 7.5 — Imperial-unit toggle** (`?units=imperial` or per-user setting).
- **Phase 7.6 — Self-hosted tile-server option** for heavy public deployments.
- **Phase 7.7 — Animated trip reveal** (segments fade in chronologically) and trip-overlap detection.
