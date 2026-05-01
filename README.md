# trip-tracker

Self-hosted itinerary aggregator. Forwarded confirmation emails (flights, hotels,
rentals, trains, transfers, activities) → unified day-by-day timeline.

> **Status:** Phase 7 — world map + per-trip Open-Meteo weather cards.
> Phase 8 (TBD — candidates: expenses with frozen FX, OCR, S3 storage) is next.
> See [`docs/superpowers/specs/2026-04-26-trip-tracker-design.md`](docs/superpowers/specs/2026-04-26-trip-tracker-design.md) for the full spec.

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
        - "^/api/ingest/email$"        # existing (Phase 2)
        - "^/healthz$"                 # existing (Phase 1)
        - "^/ics/[^/]+\\.ics$"          # NEW for Phase 6
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

## Production deploy

See `docker-compose.yml` — drop into your existing Traefik + Authelia Docker stack.
Configure forwardemail.net webhook → `/api/ingest/email` (Phase 2).

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
