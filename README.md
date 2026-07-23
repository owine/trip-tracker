# trip-tracker

Self-hosted itinerary aggregator. Forwarded confirmation emails (flights, hotels,
rentals, trains, transfers, activities) → unified day-by-day timeline.

> **Status:** Phase 9 — parse-time duplicate detection, `/inbox` triage, and
> trip consolidation (merge with a 7-day undo). The package version stays
> `0.8.1`; no `0.9.0` release was cut on `main`.
> See [`docs/superpowers/specs/2026-04-26-trip-tracker-design.md`](docs/superpowers/specs/2026-04-26-trip-tracker-design.md) for the full spec.
>
> The `v0.9.0` git tag is **not** an ancestor of `main` — it marks the end of a
> line of work that was abandoned, and Phase 9 was re-landed on `main` instead.
> Build from `main`, not from that tag.

## Quick start (local dev)

Requires Docker + `uv`.

```bash
git clone <repo> && cd trip-tracker
uv sync --all-groups
cp .env.example .env
# Fill in OIDC_* and SESSION_SECRET
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

Visit http://localhost:8000.

## Development

```bash
uv run pytest            # tests (requires Postgres on PATH or use docker compose)
uv run ruff check .      # lint
uv run mypy              # types
uv run pre-commit run --all-files  # all hooks
```

## Search (Phase 4)

Press **⌘K** (Mac) or **Ctrl+K** (anywhere else) to open the search palette.
Type to find trips and segments by title, destination, provider, confirmation
number, city, flight/train number, or notes.

### How search works

Meilisearch stores a **derived index** of trips and segments. Postgres remains
the source of truth. After every Trip or Segment write, the saq worker
syncs the row to Meili (typically <1s end-to-end).

### Recovering after deploy

Existing Trip and Segment rows from before v0.4.0 aren't yet in Meili.
After deploy, run once:

    docker compose exec trip-tracker-app python -m trip_tracker reindex

Idempotent. Safe to re-run after a Meili upgrade or any time the index
drifts from Postgres. Use `--dry-run` to preview without writing.

### Known limitation

Cascaded segment deletes from deleting a parent Trip don't trigger per-segment
Meili deletes. If you delete a Trip with many segments, run `reindex` to
reconcile (the residual segment docs will be filtered out at query time
because their parent trip is gone, but they take up index space until then).

## Documents (Phase 5)

Upload boarding passes, hotel confirmations, vouchers, and other PDFs — either
manually from a trip / segment page, or by forwarding an email with a PDF
attached. Documents are auto-linked to a matching segment when the filename
contains a confirmation number, flight/train number, or unique date.

### Search

Documents land in Meilisearch's third index (`documents`) after async text
extraction (typically <2s for boarding-pass-sized PDFs). Press **⌘K** to
search filenames AND extracted text. Click a document hit to jump to its
linked segment (if any) or download it directly.

### Storage

Files live under `${DOCUMENTS_DIR}` (default `/data/documents`),
content-addressed as `<sha256[:2]>/<sha256>`. Same content uploaded twice =
one file on disk, one row in the database (UNIQUE constraint on
`owner_user_id + sha256`). v0.5.0 ships local storage only; S3/MinIO is a
Phase 5.x candidate.

### Reverse-proxy serving (recommended)

By default, the FastAPI app streams downloads via `FileResponse`. For better
performance, set `DOCUMENTS_X_ACCEL_PREFIX` (e.g., `/internal-documents`) and
configure your reverse proxy to serve `/data/documents` from that path **as
an internal-only location**:

```nginx
# Inside your trip-tracker server block:
location /internal-documents/ {
    internal;                                  # CRITICAL — never reachable from outside
    alias /data/documents/;
    add_header Content-Disposition $upstream_http_content_disposition;
}
```

The `internal;` directive ensures URL-guessing a `storage_key` from outside
returns 404 — auth always goes through the FastAPI handler first.

### Settings

| Env var                      | Default                  | Notes                                  |
|------------------------------|--------------------------|----------------------------------------|
| `DOCUMENTS_DIR`              | `/data/documents`        | Filesystem root for content-addressed PDFs |
| `MAX_UPLOAD_BYTES`           | `26214400` (25 MiB)      | Per-file size cap; 413 on exceed       |
| `DOCUMENTS_X_ACCEL_PREFIX`   | unset                    | If set, emits `X-Accel-Redirect` for proxy serving |

### Recovery

If Meili drifts from Postgres (after a restore, schema upgrade, etc.), run:

    docker compose exec trip-tracker-app python -m trip_tracker reindex

Walks all three indexes (`trips`, `segments`, `documents`).

### Out of scope (Phase 5.x roadmap)

- OCR for scanned PDFs and image attachments (Tesseract — Phase 5.1)
- S3 / MinIO storage backend (Phase 5.2)
- Document categories (Phase 5.3)
- Drag-and-drop UI + thumbnails (Phase 5.4)
- Per-user storage quota
- Re-extraction admin action (currently requires manually setting `extract_status='pending'`)

## ICS subscribable feed (Phase 6)

Subscribe your calendar app to your upcoming trips. Each segment becomes a
calendar event with title, location, confirmation number, deep-link back to
the app, and (for flights) a 3-hour-ahead reminder.

### Generate a feed URL

1. Sign in to trip-tracker.
2. Open **Settings** in the top nav.
3. Click **Generate calendar feed URL**.
4. The URL is displayed exactly **once** — copy it into your calendar client now.

### Subscribe in your calendar app

- **Apple Calendar:** *File → New Calendar Subscription → paste the URL*. Set
  refresh to "Every 15 minutes" if you want near-realtime updates.
- **Google Calendar:** *Other calendars → + → From URL*. Polls hourly.
- **Thunderbird:** *Calendar → New Calendar → On the network → iCalendar (ICS)*.
- **Outlook:** Use the `https://...` URL directly. Outlook desktop sometimes
  treats `webcal://` URLs as web links instead of subscriptions.

### Authelia exempt-path setup

The feed URL is gated by the token in the path; it does not require an
Authelia session. If you self-host with Authelia, add the path to your
`access_control.rules` block:

```yaml
# In authelia/configuration.yml under your trips.example.com domain rules:
access_control:
  rules:
    - domain: trips.example.com
      policy: bypass
      resources:
        - "^/api/ingest/email$"             # existing (Phase 2)
        - "^/api/ingest/forwardemail$"      # NEW for ForwardEmail JSON adapter
        - "^/healthz$"                      # existing (Phase 1)
        - "^/ics/[^/]+\\.ics$"              # existing (Phase 6)
```

Verify with `curl`:

```
curl -I https://trips.example.com/ics/<your-token>.ics
# Expect: HTTP/2 200 (NOT 302 redirect to Authelia login)
```

### Regenerate / revoke

Click **Regenerate** in Settings to invalidate the old URL and produce a new
one (the old URL returns 404 immediately). There is no separate "disable"
button in v0.6.0; regeneration is the revocation path.

### Out of scope (Phase 6.x roadmap)

- Per-segment-type SUMMARY polish (e.g., showing nightly check-in + check-out as separate events for hotels)
- Per-device tokens (revoke one device without affecting others)
- Trip-level feed variant (one event per trip in addition to per-segment events)
- ETag / If-Modified-Since 304 responses (bandwidth optimization for high-poll clients)
- Audit trail of fetches

## Map (Phase 7)

Two map views, both auth-gated:

- **`/map`** — Lifetime atlas. Every trip you're a traveler on, color-coded.
  Flight legs render as great-circle arcs (curving correctly over polar
  routes); other segments pin at airport coordinates or city centroids.
- **`/trips/<id>/map`** — Per-trip view with **weather cards** for upcoming
  destinations (within 14 days of today). Cards show 7-day daily highs/lows
  via Open-Meteo (free, keyless). Cold cache renders a "Loading…" placeholder
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

    uv run python scripts/_make_cities_data.py

Downloads the latest from <https://download.geonames.org/export/dump/>
and rewrites the bundled TSV. Commit the result.

## Expenses (Phase 8 — v0.8.0)

trip-tracker tracks per-trip expenses with frozen-at-entry FX so historical totals
never silently shift when ECB rates move.

- **Currencies:** ISO 4217 minor units (cents/sen/fils) stored as `bigint`;
  `Decimal` math throughout; `numeric(20,10)` `fx_rate`. JPY (0 decimals) and
  BHD (3 decimals) are handled via the `CURRENCY_MINOR` lookup.
- **FX:** Frankfurter (free, ECB-backed, no API key). Cached in Redis for 24h.
  If Frankfurter is unreachable AND nothing is cached, the expense save fails
  with a 503 — we never store a wrong rate.
- **Categories:** food, transit, lodging, activities, shopping, gratuities,
  connectivity, other (+ free-text notes).
- **Status:** paid (default) / pending. Pending expenses count toward the
  "Expected" total but not the "Spent so far" total.
- **Cancellation/deposit:** optional triple `deposit_minor` /
  `cancellation_deadline` / `cancellation_fee_minor`. Pending expenses with a
  deadline within 30 days surface a warning on the trip detail page.
- **Award redemptions:** flight + lodging segments accept inline award metadata
  (program, points, cash co-pay, optional cash equivalent). Covers airline
  miles AND CC-transferable points (Chase UR, Amex MR, Capital One, Citi TY,
  Bilt). Per-trip "saved by points" rollup uses live FX at render time;
  Frankfurter outages just hide the line, don't 500 the page.
- **Home currency:** per-user setting, default USD. Changing it only affects
  new expenses — existing rows keep their original frozen FX.

### Deferred to later v0.8.x phases

- v0.8.1 — Auto-extract expenses from forwarded receipt emails (vendor packs +
  Haiku LLM fallback).
- v0.8.2 — CSV import from credit-card statements.
- v0.8.3 — Hotel-loyalty award nights on lodging segments + nightly breakdown.
- v0.8.4 — Per-segment cost rollup.
- v0.8.5 — Expense splitting between travelers (master-spec non-goal; revisit if
  household travel ever becomes in scope).
- v0.8.6 — Multi-currency receipts (e.g., EUR folio + USD card surcharge).
- v0.8.7 — Re-FX historical expenses admin tool.

## Inbox & trip consolidation (Phase 9)

### Duplicate detection

Re-forwarding the same confirmation no longer creates a second segment. At parse
time each extracted draft is matched against your existing segments (scoped to
you, cancelled segments excluded):

| Rule | Match on |
|---|---|
| Strong | normalized provider + confirmation number (both present) |
| Medium — flight/train/transfer | same type + start within ±30 min + same origin/destination pair |
| Medium — lodging | same check-in date + case-insensitive hotel name |

Fuzzy provider matching is deliberately excluded — it produced false merges.
If **every** draft matches, nothing is persisted and the email lands in the
Duplicates bucket. If only some match, the fresh ones are saved and the email
goes to Review so you can see what was skipped.

### /inbox

Three buckets — **Review** (below `LLM_CONFIDENCE_FLOOR`, or a partial
duplicate), **Duplicates**, and **No segments found** — with five actions:

- **Confirm** — accept the extraction, optionally retargeting it to a different
  trip; also creates the auto-Expense when the email carried a price.
- **Discard** — drop the email without creating anything.
- **Reparse** — re-run the parser chain unchanged (use after adding a vendor pack).
- **Not a duplicate** — override the dedup verdict and re-parse.
- **Re-ask** — re-run with a free-text hint.

> **Known limitation:** Re-ask stores your hint on the RawEmail
> (`X-Tt-Hint`, `routes/inbox.py::reask`) but the worker does not yet feed it to
> the LLM (`worker.py::parse_raw_email`), so the re-parse currently behaves like
> a plain Reparse.

### Consolidation suggestions and merging

Trips that look like one trip split in two get a dismissible banner on the trip
detail page and in the inbox confirm preview. Up to 3 candidates are suggested,
scored:

- **High** — home-anchored (the next leg out, or the closing leg back home)
- **Medium** — shared endpoint city
- **Low** — nearest endpoint pair within 500 km

Candidates are drawn from a ±3-day window around the target. Dismissing a pair
suppresses it permanently for that pairing.

Merging reassigns segments, expenses, documents, and travelers onto the target,
widens the target's date range, and **soft-deletes** the source (it stays in the
database with `merged_into_id` + a `merge_audit` JSONB payload). Undo is
audit-driven and lossless within **7 days**; after that a daily cron
(`purge_merged_trips`, 04:00 UTC) hard-deletes the source and the merge becomes
permanent. Merges cannot be unwound out of order — if the target has itself
since been merged, unwind from the top first.

### Known limitations (Phase 9)

Both live in `trips/consolidation.py::consolidation_candidates`:

- Home-anchored matching runs through the same ±3-day window as the geometric
  fallback, so long-gap home-anchored chains (e.g. a trip out and a return leg
  three weeks later) will not surface until that window is widened.
- Candidate lookup issues N+1 selects across the windowed trips (bounded at 50).
  It runs per user action, not in a hot loop.

## Production deploy

See `docker-compose.yml` — drop into your existing Traefik + Authelia Docker stack.
Configure forwardemail.net webhook → `/api/ingest/email` (Phase 2).

### Configuration reference

All runtime configuration is via environment variables. The app/worker validate
these at startup via `Settings` (`src/trip_tracker/config.py`) — missing required
values fail fast.

**Required — stack won't run without these:**

The "Used by" column reflects the `Settings` / `WorkerSettings` split: worker
boots with only the env vars it actually needs. App needs both columns set.

| Env var | Used by | Notes |
|---|---|---|
| `DATABASE_URL` | app + worker | `postgresql+asyncpg://trip:${DB_PASSWORD}@trip-tracker-db:5432/trip` |
| `DB_PASSWORD` | postgres init | Compose-only; also feeds into `DATABASE_URL` |
| `SESSION_SECRET` | app | Min 32 chars. `python -c 'import secrets; print(secrets.token_hex(32))'` |
| `OIDC_ISSUER` | app | e.g. `https://auth.yourdomain.com` |
| `OIDC_CLIENT_ID` | app | Authelia client name |
| `OIDC_CLIENT_SECRET` | app | Authelia client secret |
| `OIDC_REDIRECT_URI` | app | e.g. `https://trips.yourdomain.com/auth/callback` |
| `BASE_URL` | app | e.g. `https://trips.yourdomain.com` |
| `WEBHOOK_SECRET` | app | HMAC for `/api/ingest/email`. `python -c 'import secrets; print(secrets.token_hex(32))'` |
| `FORWARDEMAIL_RELAY_TOKEN` | app | Token for `?token=` on `/api/ingest/forwardemail`. `python -c 'import secrets; print(secrets.token_urlsafe(32))'` |
| `ANTHROPIC_API_KEY` | worker | `sk-ant-...` for the Haiku LLM fallback parser |
| `MEILI_MASTER_KEY` | app + worker + meili | Search index access. `openssl rand -hex 32` |

**Optional — defaults shown; override only if needed:**

| Env var | Default | Notes |
|---|---|---|
| `TRIP_TRACKER_IMAGE` | `ghcr.io/REPLACE_OWNER/trip-tracker:latest` | Pin to a specific tag for stability |
| `TRIP_HOST` | `trips.example.com` | Used in Traefik routing rule |
| `REDIS_URL` | `redis://trip-tracker-redis:6379/0` | Override only for external Redis |
| `MEILI_URL` | `http://trip-tracker-search:7700` | Override only for external Meilisearch |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`WARNING`/`ERROR` |
| `LOG_FORMAT` | `json` | `console` for human-readable in dev |
| `TZ` | `UTC` | Container timezone, e.g. `America/Chicago` |
| `ADMIN_GROUP` | `trip-tracker:admin` | OIDC group claim that grants `/admin/*` access |
| `SESSION_COOKIE_NAME` | `tt_session` | Don't change unless reverse-proxy needs it |
| `SESSION_MAX_AGE_SECONDS` | `604800` (7 days) | Idle-logout window |
| `WEBHOOK_SIGNATURE_HEADER` | `X-Webhook-Signature` | Per FE's docs; override only if FE changes header name |
| `WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS` | `300` (5 min) | Replay-attack window for `/api/ingest/email` |
| `WEBHOOK_MAX_BODY_BYTES` | `26214400` (25 MiB) | Upper bound for direct webhook body |
| `LLM_DAILY_BUDGET_CENTS` | `100` ($1.00/day) | Soft cap on Haiku spend before parser short-circuits |
| `LLM_MODEL` | `claude-haiku-4-5-20251001` | Pinned per master spec |
| `LLM_CONFIDENCE_FLOOR` | `0.7` | Below this → segments land in `/inbox` for review |
| `DOCUMENTS_DIR` | `/data/documents` | Phase 5 storage path; container-internal |
| `MAX_UPLOAD_BYTES` | `26214400` (25 MiB) | Upper bound for `/documents/upload` |
| `DOCUMENTS_X_ACCEL_PREFIX` | (unset) | Set to e.g. `/protected-files` when fronting with nginx X-Accel-Redirect |

The worker uses `WorkerSettings` (a strict subset of `Settings`) and only
needs DB/Redis/LLM/Meili/log/documents env vars. App-only fields
(`SESSION_SECRET`, `OIDC_*`, `BASE_URL`, `WEBHOOK_SECRET`,
`FORWARDEMAIL_RELAY_TOKEN`) can be omitted from the worker container's
environment. See the trimmed `trip-tracker-worker` block in
`docker-compose.yml`.

### Authelia OIDC client

The app is a confidential OIDC client with PKCE S256. It reads claims directly
from the ID token (no `/userinfo` call), so `email` and `groups` must be
embedded in the ID token — not just exposed via userinfo, which is Authelia's
default since 4.38.

**1. Generate the client secret:**

```bash
authelia crypto hash generate pbkdf2 --variant sha512 \
  --random --random.length 72 --random.charset rfc3986
```

Save both outputs — the random plaintext goes in trip-tracker's `.env` as
`OIDC_CLIENT_SECRET`; the `$pbkdf2-sha512$…` digest goes in Authelia.

**2. Add to Authelia `configuration.yml`:**

```yaml
identity_providers:
  oidc:
    claims_policies:
      trip_tracker_policy:
        id_token:
          - email
          - email_verified
          - preferred_username
          - groups

    clients:
      - client_id: trip-tracker
        client_name: Trip Tracker
        client_secret: '$pbkdf2-sha512$310000$...'   # digest from step 1
        public: false
        authorization_policy: two_factor             # or one_factor
        require_pkce: true
        pkce_challenge_method: S256
        claims_policy: trip_tracker_policy
        redirect_uris:
          - https://trips.example.com/auth/callback
        scopes: [openid, profile, email, groups]
        grant_types: [authorization_code]
        response_types: [code]
        token_endpoint_auth_method: client_secret_post
        consent_mode: implicit
```

**3. Set trip-tracker `.env`:**

```bash
OIDC_ISSUER=https://auth.example.com           # must match `iss` in ID token exactly (no trailing slash)
OIDC_CLIENT_ID=trip-tracker
OIDC_CLIENT_SECRET=<plaintext from step 1>
OIDC_REDIRECT_URI=https://trips.example.com/auth/callback
ADMIN_GROUP=trip-tracker:admin                 # users in this Authelia group get admin
```

**4. Remove forward-auth middleware:**

Drop `traefik.http.routers.trip.middlewares=authelia@docker` from the app
service labels — the app authenticates itself via OIDC and the forward-auth
middleware will intercept `/auth/*` and break the flow.

The first user to log in is auto-promoted to admin. Subsequent users get admin
only via membership in `ADMIN_GROUP`.

## Email forwarding setup (Phase 2)

1. Generate a webhook secret: `python -c 'import secrets; print(secrets.token_hex(32))'` and put it in `.env` as `WEBHOOK_SECRET=…`.
2. As admin, log in and go to `/admin/aliases` → "+ New". Create `<your-local-part>` mapped to your user (e.g. `oliver`).
3. In forwardemail.net's dashboard for `trips.<your-domain>`:
   - Add forwarding rule `oliver@trips.<your-domain>` → webhook URL `https://trips.<your-domain>/api/ingest/email`.
   - Configure HMAC secret to match `WEBHOOK_SECRET`.
   - Confirm the signature header name; if it's not `X-Webhook-Signature`, set `WEBHOOK_SIGNATURE_HEADER=` accordingly.
4. Test: forward yourself a confirmation email to `oliver@trips.<your-domain>`. Within seconds it appears at `/admin/raw-emails`.
5. The parser pipeline (Phase 3) auto-extracts a segment from the email and either auto-attaches it to an existing trip or creates a new one. Low-confidence extractions land in `/inbox` for manual confirmation.

> **ForwardEmail.net free-plan users:** the HMAC signing the steps above assume is **paid-plan only**. If you're on the free plan, use the alternative adapter route `/api/ingest/forwardemail` which accepts FE's webhook JSON directly with a shared-secret token. See [`docs/forwardemail-setup.md`](docs/forwardemail-setup.md).

## How parsers work (Phase 3)

When a forwarding email arrives at `/api/ingest/email`, a saq worker runs three
strategies in priority order:

1. **JSON-LD via extruct** — for emails that embed schema.org `FlightReservation`,
   `LodgingReservation`, `RentalCarReservation`, or `EventReservation`. Highest
   confidence (~0.95).
2. **Vendor rule packs** — Air France, American, United, Fairmont, Avis, National,
   Amtrak, SNCF, Uber, Blacklane. Each pack matches by `From:` header. Confidence
   ~0.9 on a successful match.
3. **Anthropic Haiku 4.5** — fallback for unknown senders. Tool-use schema mirrors
   the `SegmentDraft` shape. Self-rated confidence clamped at 0.85 so vendor packs
   added later naturally override the cached Haiku result.

Each strategy can return zero segments (with high confidence "no itinerary here")
or one or more. The dispatcher keeps the best result across strategies. Trip
clustering then attaches the segment(s) to an existing trip — geo-distance via
`airports.csv` (200km threshold) when both endpoints have coords, normalized
city-name match otherwise; ±1 day adjacency window — or creates a new trip with
auto-title `"{primary_destination} {month year}"`.

### Adding a new vendor

1. Create `src/trip_tracker/parsers/vendors/<name>/__init__.py` with a
   `VendorParser` subclass.
2. Add the import to `src/trip_tracker/parsers/vendors/__init__.py`.
3. Drop a fixture pair: `fixtures/<scenario>.eml` + `<scenario>.expected.json`.
4. CI's parameterized vendor test will pick up the new fixture automatically —
   no test code changes required.

### Daily LLM budget

Set `LLM_DAILY_BUDGET_CENTS` (default 100 = $1/day). When exceeded, RawEmails
skip the Haiku step and route to `parse_status='review'` for manual handling.

### Recovering after deploy

Existing `RawEmail` rows from before v0.3.0 sit in `parse_status='pending'`.
Reprocess them once after deploy:

```bash
docker compose exec trip-tracker-app python -m trip_tracker parse_pending
```

Idempotent — safe to re-run. Use `--max-emails=N` to cap the batch and
`--dry-run` to preview without enqueueing.
