# Trip Tracker — Design Spec

**Date:** 2026-04-26
**Status:** Draft (pre-implementation)
**Author:** Oliver + Claude (brainstorm)

A self-hosted itinerary aggregator that replaces TripIt for a household. Forwarded confirmation emails are parsed into a unified, searchable, day-of-aware travel timeline. PWA frontend, OIDC-authenticated, with optional public share links.

---

## 1. Goals & Non-Goals

### Goals (v1)

- **Aggregate** confirmation emails (flights, hotels, rental cars, trains, ride/transfer, activities) into a single timeline
- **Day-of view** that surfaces what's happening *now* and *next* — the "open at the gate" experience
- **Household sharing**: multiple authenticated users, with co-traveler trips that auto-merge bookings from any traveler's email
- **Public share links** (read-only, sanitized by default, revocable, optional expiration/password)
- **PWA** (installable, offline view of upcoming segments, web push for parser-confirms)
- **ICS subscribable feed**, world map of trip route, document vault (boarding passes, etc.), per-trip expense tracking with frozen FX, weather forecast at destination
- **Self-hosted, Docker-based**, designed to fit a Traefik + Authelia stack with no cloud dependencies (other than the Anthropic API and forwardemail.net's webhook)

### Non-Goals (v1, deferred)

- Cruises (rare, complex, low ROI)
- Live flight status (paid API; defer until v2)
- Travel stats / "passport" view
- Packing lists
- Expense splits (who-owes-whom between travelers)
- Native mobile apps (PWA covers it)
- Multi-tenant SaaS (single-household tool)
- Trip planning / collaborative drafting (this is a *log*, not a planner)

---

## 2. Architecture Overview

```
                ┌────────────────────────┐
                │  forwardemail.net      │
                │  (MX for trips.*.com)  │
                └───────────┬────────────┘
                            │  HMAC-signed webhook (raw MIME)
                            ▼
┌────────────────────┐   ┌──────────────────────┐
│  Authelia (OIDC)   │◄──┤  Traefik (TLS, routing)│
└────────────────────┘   └───────────┬───────────┘
       (most routes)                 │ (/share, /api/ingest exempted)
                                     ▼
                         ┌────────────────────────┐         ┌──────────────────┐
                         │  trip-tracker-app      │◄────────┤  Anthropic API   │
                         │  (FastAPI + Jinja+HTMX)│         │  Haiku 4.5       │
                         └─────┬─────────────┬────┘         └──────────────────┘
                               │             │              ┌──────────────────┐
                               │             ▼              │  Open-Meteo      │
                               │     ┌──────────────┐       │  Frankfurter FX  │
                               │     │ ARQ worker   │◄──────┤  (free, keyless) │
                               │     │ (same image) │       └──────────────────┘
                               ▼     └──────┬───────┘
                         ┌──────────┐       │
                         │ Postgres │◄──────┘
                         │   18     │
                         └────┬─────┘    ┌──────────┐
                              │          │  Redis 7 │ (ARQ queue + cache)
                              │          └──────────┘
                              │ post-commit sync (ARQ task)
                              ▼
                         ┌──────────────┐
                         │ Meilisearch  │ (derived index — typo-tolerant ⌘K)
                         │  (rebuildable)│
                         └──────────────┘

  Document storage: pluggable adapter
    - LocalFsStorage  → /data/documents (content-addressed)
    - S3Storage       → bucket via aioboto3 (S3, MinIO, R2, B2, Garage…)
```

**5 new containers:** app, worker, postgres, redis, meilisearch. Plus your existing Traefik + Authelia.

### Boundaries

- **Ingestion pipeline** (raw email → segments) is fully decoupled from presentation. Re-runnable from stored MIME.
- **Public share routes** and **webhook routes** live on separate FastAPI routers with no shared session-auth dependencies — bugs there cannot inherit user permissions.
- **Document storage** is a `Protocol` with two implementations selected by env var. Same content-addressed key shape in both.

---

## 3. Data Model (Postgres 18)

Foreign keys, `created_at`/`updated_at`, and indexes elided for brevity.

### Core

```sql
users (
  id            uuid PK,
  oidc_subject  text UNIQUE,           -- Authelia 'sub' claim
  email         text UNIQUE,
  display_name  text,
  is_admin      boolean DEFAULT false
)

forwarding_aliases (
  id          uuid PK,
  user_id     uuid FK,
  local_part  text UNIQUE              -- 'oliver' for oliver@trips.yourdomain.com
)

trips (
  id                  uuid PK,
  title               text,             -- "Paris May 2026" — auto-derived, user-editable
  start_date          date,
  end_date            date,
  primary_destination text,
  notes               text,
  cover_color         text
)

trip_travelers (
  trip_id  uuid FK,
  user_id  uuid FK,
  role     text,                        -- 'owner' | 'companion'
  PRIMARY KEY (trip_id, user_id)
)

segments (
  id                  uuid PK,
  trip_id             uuid FK,
  owner_user_id       uuid FK,          -- whose alias received the email
  type                text,             -- flight|lodging|car|train|transfer|activity
  status              text,             -- confirmed|cancelled|tentative
  confirmation_number text,
  provider            text,             -- 'Delta', 'Marriott', etc.
  start_at            timestamptz,
  start_tz            text,             -- IANA
  end_at              timestamptz,
  end_tz              text,
  start_location      jsonb,            -- {name, iata?, lat, lon, address, city, country}
  end_location        jsonb,
  details             jsonb,            -- type-specific
  parse_source        text,             -- json-ld | rules:delta | llm:haiku-4-5 | manual
  parse_confidence    float,            -- 0..1; <0.7 → review queue
  search_text         tsvector,         -- generated, FTS index
  raw_email_id        uuid FK NULL,
  superseded_by       uuid FK NULL          -- self-FK; non-null means a newer version exists
)

segment_versions (                          -- audit trail for manual edits + re-parse
  id              uuid PK,
  segment_id      uuid FK,                  -- the *current* segment row
  snapshot_jsonb  jsonb,                    -- full prior row at the moment of change
  changed_by      uuid FK,                  -- user who edited, or NULL for parser
  change_reason   text,                     -- 'manual_edit' | 'reparse' | 'inbox_confirm'
  created_at      timestamptz
)

raw_emails (
  id           uuid PK,
  received_at  timestamptz,
  to_address   text,
  from_address text,
  subject      text,
  message_id   text UNIQUE,             -- RFC 5322; dedupes re-forwards
  mime_blob    bytea,
  headers      jsonb,
  parse_status text,                    -- pending|parsed|failed|no_segments|review
  parse_error  text
)
```

### Supporting

```sql
documents (
  id             uuid PK,
  trip_id        uuid FK NULL,
  segment_id     uuid FK NULL,
  owner_user_id  uuid FK,
  filename       text,
  mime_type      text,
  size_bytes     bigint,
  storage_key    text,                  -- '<sha256[:2]>/<sha256>'
  sha256         text,
  category       text                   -- boarding_pass|visa|voucher|other
)

expenses (
  id                uuid PK,
  trip_id           uuid FK,
  paid_by_user_id   uuid FK,
  occurred_on       date,
  amount_minor      bigint,             -- minor units of source currency
  currency          char(3),
  amount_home_minor bigint,             -- frozen home-currency value
  fx_rate           numeric(18, 8),     -- frozen rate at entry
  category          text,
  description       text
)

share_tokens (
  id            uuid PK,
  trip_id       uuid FK,
  token_hash    text UNIQUE,            -- sha256(plaintext); plaintext only in URL
  created_by    uuid FK,
  expires_at    timestamptz NULL,       -- NULL = permanent (explicit opt-in)
  password_hash text NULL,              -- bcrypt
  sanitized     boolean DEFAULT true,
  revoked_at    timestamptz NULL,
  view_count    int DEFAULT 0
)

fx_rates (
  date  date,
  base  char(3),
  quote char(3),
  rate  numeric(18, 8),
  PRIMARY KEY (date, base, quote)
)
```

### Key invariants

1. **Trips are derived, not entered.** Auto-clustered from segments. Users can split/merge.
2. **Segments store IANA tz separately** from `timestamptz` — display always uses stored tz.
3. **`raw_emails.message_id` UNIQUE** — re-forwarding the same confirmation is a no-op.
4. **`parse_confidence < 0.7` → review queue.** No silent drops.
5. **Expenses freeze FX at entry time.** Trip totals are stable forever.
6. **Share tokens hashed at rest.** DB leak doesn't expose live shares.

---

## 4. Ingestion Pipeline

Six stages, all idempotent. Each can be re-run independently from stored MIME.

### Stages

1. **Webhook arrives** — `POST /api/ingest/email` from forwardemail.net. Verify HMAC-SHA256 signature constant-time. Reject duplicates via `(timestamp, nonce)` cache (24h). Reject payloads >25MB. Return `202` immediately, queue work.
2. **Persist + dedupe** — Insert into `raw_emails` with `parse_status='pending'`. UNIQUE on `Message-ID`. Resolve `To:` → `forwarding_aliases` → owner user (unknown alias = flag for review).
3. **Parse — strategies in priority order:**
   - **3a. JSON-LD** via `extruct` — looks for `FlightReservation`, `LodgingReservation`, `RentalCarReservation`, `EventReservation`. Confidence ~0.95 on hit.
   - **3b. Provider rules** — match `From:` against handlers. v1 set: Delta, United, American, Southwest, Marriott, Hilton, Hyatt, IHG, Hertz, Avis, National, Enterprise, Amtrak, Booking.com, Airbnb. (Avis covers Avis + Budget; Enterprise covers Enterprise + National + Alamo.) Confidence ~0.9.
   - **3c. Claude Haiku 4.5** — last resort. System prompt + few-shot examples (cached via prompt caching). Body sent via tool-use with `Segment` JSON schema. Model self-rates confidence, clamped ≤0.85. Cost: ~$0.005/email; ~$0.25/year at household scale.
4. **Normalize + enrich** — Resolve airport codes → IANA tz + lat/lon (static `airports.csv`). Geocode hotel addresses (cached, OSM Nominatim, low rate). Normalize provider names.
5. **Cluster into a trip** — Find existing trip where `(date_overlap OR adjacent±1d) AND (location proximity OR shared traveler with overlapping trip)`. **Tiebreak:** if multiple existing trips match, pick the one whose date range center is closest to the segment's start. If the score gap to the next-best match is < 20%, route the segment to `/inbox` for manual disambiguation instead of guessing. Otherwise create a new trip with auto-title `"{primary_destination} {month year}"`.
6. **Outcome:**
   - `confidence ≥ 0.7` → `parsed`, visible in timeline, owner gets web-push notification.
   - `confidence < 0.7` → `review` → `/inbox` queue.
   - 0 segments returned → `no_segments` → `/inbox` queue.
   - Exception → `failed` (with error) → `/inbox` queue.

### Re-parse

`POST /api/segments/:id/reparse` and bulk `arq reparse --since=DATE`. Original segments are versioned, not destroyed. Used when adding a provider rule, swapping the LLM, or fixing a bug.

---

## 5. UI / UX

PWA. Mobile-first. Server-rendered HTML via Jinja + HTMX for interactivity. Tailwind for styling, dark mode via `prefers-color-scheme` (default), with explicit override toggle in settings.

### Bottom nav (mobile) / sidebar (desktop)

`Today · Trips · Map · Inbox`. Settings/expenses/documents are scoped contexts inside trip detail or behind a header menu.

### Screens (v1)

1. **Today** — landing screen during an active trip. Shows current event (highlighted with countdown), today's remaining events, peek at tomorrow. When no trip is active, shows next upcoming trip card.
2. **Trips** — list. Big cards on mobile (cover color, dates, destination), compact rows on desktop. Filters: upcoming / past / shared with me.
3. **Trip detail** — vertical day-grouped timeline of segments. Time on left rail, type icon + provider + key details in each card. Tap → segment detail (raw email link, documents, edit, re-parse). Co-traveler avatar tag visible on segments contributed by another traveler.
4. **Map** — Leaflet + OSM tiles (no API key). Pins for location-having segments + curved arcs for flight routes. Weather card pinned to current/upcoming destination (Open-Meteo).
5. **Inbox** — review queue. Three buckets: low-confidence parses, no-segments emails, possible duplicates.
   - **Low-confidence parses** open into an editable form **pre-filled with whatever the parser extracted** (even partial). Each pre-filled field shows a small "AI-suggested" indicator (✨ icon + tooltip with the source: `json-ld` / `rules:delta` / `llm:haiku-4-5`); user-confirmed fields drop the indicator. The raw email is shown side-by-side (collapsible iframe sandbox of the HTML body, or plain-text fallback) so the user can verify against the source. Actions: **Confirm** (accept all current values, status → parsed), **Edit** (modify fields, then confirm), **Re-ask Claude with hint** (one-line text input → re-runs Haiku with the hint appended to the prompt, repopulates the form), **Split** (this email actually contains multiple segments — opens a multi-segment editor), **Discard** (mark email as `parse_status='no_segments'`, no segment created).
   - **No-segments emails:** view raw / re-parse / "Add segment manually" (empty form pre-populated only with sender → provider guess and email date → start date) / Discard.
   - **Possible duplicates:** merge / keep both / discard.
   - All edits via the inbox flow are recorded in `segment_versions` with `change_reason='inbox_confirm'` for audit and undo. Empty inbox = parser working.
6. **Trip → Documents** — list + upload + view. Boarding passes, visas, vouchers. Served via short-TTL presigned URLs when on S3, X-Sendfile-style on local FS.
7. **Trip → Expenses** — line items, category breakdown, currency-aware. FX frozen at entry.
8. **Settings** — household members (admin only), forwarding aliases (admin), theme toggle, ICS feed URL, share token management.

### ⌘K palette

Global keyboard shortcut (`⌘K` / `Ctrl+K`). Opens a modal that queries Meilisearch directly. Scope: trips, segments (provider/confirmation/route/hotel), documents (filename). Typo-tolerant. Results grouped by entity type with keyboard navigation. Selecting a result deep-links into the relevant view.

### PWA

- Service worker via `Workbox` (or hand-rolled) caches the next 7 days of segments + boarding pass thumbnails + app shell.
- Offline view restricted to cached upcoming segments + map tiles for cached locations.
- Web push for parser confirms (iOS requires "Add to Home Screen" first — one-time setup banner).
- Manifest with theme colors, icons, install prompts.

---

## 6. Auth & Sharing

Three distinct auth paths, intentionally isolated.

### A. Household users — OIDC via Authelia

Authorization Code + PKCE. Validate ID token against Authelia JWKS (cached, refreshed on rotation). Upsert `users` keyed on `oidc_subject`. First user *or* members of `trip-tracker:admin` group get `is_admin=true`. Sessions: HTTP-only, Secure, SameSite=Lax cookies (signed, 7d TTL). Logout = local destroy + RP-initiated logout to Authelia.

Local `users` table needed so trips/segments/etc. FK to a stable id, and so admins can invite household members (creating user rows + alias mappings) without Authelia API access.

### B. Public share links — token-based

Generate 32-byte token via `secrets.token_urlsafe(32)`. Store `sha256(token)` in `share_tokens.token_hash`. Plaintext returned **once**. URL: `/share/<token>`. Constant-time hash comparison. Optional bcrypt password gate. Optional expiration; **default = 30 days** with explicit "permanent" opt-in checkbox. Revocation sets `revoked_at` → 410 Gone. Rate-limit `/share/*` per IP. `noindex` header.

**Sanitized view (default ON):** Hides confirmation numbers entirely (not last-4), seat numbers, frequent-flyer numbers, payment fragments, document attachments. Shows times, dates, locations, hotel names, flight numbers/route, expense totals (no line items).

### C. Webhook — HMAC

`/api/ingest/email` exempt from Authelia at Traefik. forwardemail.net signs each POST with HMAC-SHA256 (shared secret). Constant-time verify. Replay protection via `(timestamp, nonce)` cache (24h). 25MB body cap.

### Route map

```
/                        Authelia required
/today /trips /inbox     Authelia required
/api/...                 Authelia required
/api/ingest/email        EXEMPT — HMAC-verified
/share/<token>           EXEMPT — token-verified, read-only router
/auth/callback           EXEMPT — OIDC redirect
/healthz                 EXEMPT — health check
/ics/<user_token>.ics    EXEMPT — per-user signed token (32-byte, regenerable in Settings)
```

A second host, `search.trips.yourdomain.com`, fronts the **Meilisearch** container directly via Traefik (no Authelia). It is auth'd by **scoped Meilisearch tenant tokens** issued by FastAPI (signed JWT, scoped to the user's `traveler_ids`, 1h TTL). See §8.5. The Meilisearch master key never leaves the app container.

ICS feed URLs include a 32-byte `user_token` stored hashed on the user row. Regenerable from Settings (rotates the URL); revocation is just regeneration.

The `/share` and `/api/ingest/email` routers are wired with **no shared dependencies** on session-auth machinery. They cannot accidentally inherit a logged-in user's permissions.

---

## 7. Tech Stack

| Layer            | Choice                                                                 |
|------------------|------------------------------------------------------------------------|
| Language         | Python 3.13                                                            |
| Web framework    | FastAPI                                                                |
| ORM              | SQLAlchemy 2.0 (async) + Alembic migrations                            |
| Validation       | Pydantic v2                                                            |
| Templating       | Jinja2                                                                 |
| Frontend         | HTMX + Alpine.js + Tailwind CSS (Vite for asset bundling)              |
| Job queue        | ARQ (Redis-backed)                                                     |
| Database         | Postgres 18                                                            |
| Cache + queue    | Redis 7                                                                |
| Search           | Meilisearch (derived index, rebuildable from Postgres)                 |
| LLM              | Anthropic Python SDK, Claude Haiku 4.5 (`claude-haiku-4-5-20251001`), prompt caching enabled on system prompt |
| Email parsing    | `mail-parser` (RFC 822) + `extruct` (JSON-LD) + custom rules           |
| Document storage | `Protocol` adapter: `LocalFsStorage` or `S3Storage` (`aioboto3`)       |
| Map              | Leaflet + OpenStreetMap tiles                                          |
| Weather          | Open-Meteo (keyless)                                                   |
| FX rates         | Frankfurter (ECB, keyless)                                             |
| Geocoding        | OSM Nominatim (rate-limited, cached)                                   |
| Package mgmt     | `uv` with `uv.lock` (full hash pinning)                                |
| Container base   | `python:3.13-slim` (digest-pinned)                                     |
| Reverse proxy    | Traefik (existing)                                                     |
| Auth provider    | Authelia (existing)                                                    |
| Inbound mail     | forwardemail.net (webhook)                                             |

---

## 8. Deployment

### Compose (5 new services)

`trip-tracker-app`, `trip-tracker-worker` (same image, `command: arq ...`), `trip-tracker-db` (postgres:18-alpine), `trip-tracker-redis` (redis:7-alpine, `--save "" --appendonly no`), `trip-tracker-search` (`getmeili/meilisearch:v1`, master key from env, internal network only).

Traefik labels handle:
- Authed router on `Host(trips.yourdomain.com)` with `authelia@docker` middleware
- Public router (higher priority) on the same host with `PathPrefix(/share) || Path(/api/ingest/email)` to bypass Authelia

Single named volume `trip-tracker-data` for `/data/documents` (when `STORAGE_BACKEND=local`). Postgres has its own `trip-tracker-pg` volume.

### Image

- Multi-stage build (`python:3.13-slim` builder + runtime, both digest-pinned)
- `uv` for dep install with `uv.lock`
- Multi-arch: `linux/amd64` + `linux/arm64` via `docker buildx`
- Signed with `cosign` (keyless, GitHub OIDC)
- Tags: `latest`, `vMAJOR.MINOR.PATCH`, `vMAJOR.MINOR`, `vMAJOR` + immutable digest
- Pushed to `ghcr.io`
- SBOM via `syft`, attached to GH release

### First-run

1. Add Authelia client config (`trip-tracker`, scopes `openid profile email groups`, redirect `/auth/callback`).
2. Add forwardemail.net domain (`trips.yourdomain.com`), MX records, webhook URL + HMAC secret.
3. Create per-user aliases in forwardemail.net.
4. `cp .env.example .env`, fill secrets, `docker compose up -d`.
5. Visit, log in (becomes admin), invite household users + create their aliases.
6. Forward a test confirmation.

### Backups

- `pg_dump` → gzip → offsite (rclone)
- `/data/documents` tar OR S3 bucket versioning + lifecycle policy
- **Meilisearch is NOT backed up** — it's a derived index. `bin/reindex` rebuilds from Postgres in seconds.
- Wrapper: `scripts/backup.sh`

### Updates

- `docker compose pull && up -d`
- Alembic migrations run automatically at container start (`alembic upgrade head`, idempotent)
- Worker restarts mid-job → ARQ retries from queue
- Rollback: pin to previous digest; each migration ships with a documented manual revert SQL block in its docstring

### Observability

- `/healthz` → DB + Redis + storage backend status (Traefik health check)
- Structured JSON logs to stdout
- `metrics` table with daily counters (emails received/parsed/reviewed/failed, LLM tokens spent), surfaced in Settings → Stats

---

## 8.5 Search (Meilisearch)

### Role

Meilisearch is a **derived index**, not a system of record. Every searchable value lives in Postgres first; Meili exists only to provide typo-tolerant, sub-50ms full-text search for the ⌘K palette and search bars.

### Indexes

| Index name | Source table(s) | Searchable fields | Filterable fields |
|---|---|---|---|
| `segments` | `segments` (joined w/ `trips`, `users`) | provider, confirmation_number, flight_number, hotel name, route (e.g. "JFK → CDG"), notes | trip_id, owner_user_id, traveler_ids[], type, year, status |
| `trips` | `trips` (joined w/ `trip_travelers`, agg of `segments.primary_destination`) | title, primary_destination, notes, destinations[] | traveler_ids[], year |
| `documents` | `documents` (filename + extracted text v2) | filename, category | trip_id, owner_user_id |

Documents content extraction (PDF text → searchable) is deferred to v2.

### Sync mechanism

- **Post-commit ARQ task.** Any write to `segments`/`trips`/`raw_emails` enqueues `sync_search(entity, id)`.
- ARQ deduplicates queued tasks for the same `(entity, id)` within a 5s coalescing window — bursts collapse to one upsert.
- Deletes propagate identically (`unsync_search(entity, id)`).
- Eventual consistency: typically <1s lag at this volume.
- **Bulk reindex command:** `python -m trip_tracker.search reindex [--index segments|trips|all]`. Used after schema changes, Meili upgrades, or recovery.
- A nightly periodic ARQ task verifies counts (`SELECT count FROM segments` vs `meili.stats.numberOfDocuments`) and triggers reindex on mismatch.

### Frontend integration

- ⌘K palette opens a modal that hits Meilisearch **directly from the browser** — not proxied through FastAPI.
- FastAPI issues a **scoped tenant token** on page load: a short-lived (1h) signed JWT that scopes the search API key to `traveler_ids = [me, ...co_travelers]`. Renewed silently before expiry.
- This keeps the architecture clean (Meili visible to the browser via Traefik label `Host(search.trips.yourdomain.com)`, no auth proxy needed) and search latency minimal.
- Meilisearch master key is server-side only and never leaves FastAPI.

### Container

```yaml
trip-tracker-search:
  image: getmeili/meilisearch:v1
  restart: unless-stopped
  environment:
    MEILI_MASTER_KEY: ${MEILI_MASTER_KEY}
    MEILI_ENV: production
    MEILI_NO_ANALYTICS: "true"
  volumes:
    - trip-tracker-meili:/meili_data
  networks: [internal, proxy]
  labels:
    - "traefik.enable=true"
    - "traefik.http.routers.trip-search.rule=Host(`search.trips.yourdomain.com`)"
    - "traefik.http.routers.trip-search.entrypoints=websecure"
    - "traefik.http.routers.trip-search.tls.certresolver=letsencrypt"
    - "traefik.http.services.trip-search.loadbalancer.server.port=7700"
```

Memory footprint: ~80MB at our scale. Volume holds the rebuildable index — losing it triggers `bin/reindex` automatically on next app start.

### Why not pgroonga / Postgres-only

We considered: Postgres `tsvector` (already on the table) + `pg_trgm` for fuzzy match. It works but the typo tolerance is noticeably weaker, faceted ranking is hand-rolled, and the as-you-type latency requires careful query tuning. Meili gets us a Linear/Raycast-quality palette out of the box for the cost of one tiny container.

---

## 9. CI / CD

### Workflows

- **`.github/workflows/ci.yml`** (on PR + push to main):
  - **Lint:** `ruff check`, `ruff format --check`, `djlint --check` (Jinja templates), `prettier --check` (CSS/JS/HTML statics)
  - **Typecheck:** `mypy --strict` on app code, relaxed on tests
  - **Tests:** `pytest` with `pytest-asyncio`, real Postgres 18 + Redis as service containers, parametrized provider-rule tests using `tests/fixtures/emails/<provider>/*.eml` snapshot tests, coverage threshold 85%
  - **Smoke E2E:** `playwright` against the running app for 2-3 critical flows (auth callback, render trip, render share page)
  - **Security:**
    - `bandit` (Python static security)
    - `pip-audit` (Python deps; uv has built-in too as backup)
    - `gitleaks` (secrets scan)
    - `trivy` (built image — OS packages + Python deps)
    - `semgrep` (OWASP top 10 ruleset)
  - **CodeQL** (`.github/workflows/codeql.yml`) — Python + JS, weekly + on PR
  - **Dependency review** (`.github/workflows/dependency-review.yml`) — blocks PRs introducing known-vulnerable deps
- **`.github/workflows/release.yml`** (on tag `v*`):
  - Build multi-arch image
  - Sign with `cosign` (keyless, GitHub OIDC)
  - Push to `ghcr.io` with digest pin
  - Generate SBOM (`syft`), attach to GitHub release
  - Auto-changelog from conventional commits
- **`.github/workflows/renovate.yml`** — runs Renovate self-hosted on schedule (or use the Renovate GitHub App)

### Pre-commit hooks (`.pre-commit-config.yaml`)

Mirrors CI for fast local feedback: `ruff`, `ruff-format`, `mypy` (limited), `gitleaks`, `prettier` on templates. Runs on `git commit` via `pre-commit install`.

### Renovate config (`renovate.json`)

```jsonc
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "extends": ["config:recommended", ":dependencyDashboard"],
  "schedule": ["before 6am on monday"],
  "timezone": "America/Los_Angeles",
  "labels": ["dependencies"],
  "packageRules": [
    {
      "matchUpdateTypes": ["patch", "minor"],
      "matchManagers": ["pip_requirements", "pep621"],
      "groupName": "python (patch+minor)",
      "automerge": true
    },
    {
      "matchManagers": ["dockerfile", "docker-compose"],
      "matchUpdateTypes": ["digest", "pinDigest"],
      "groupName": "docker digests",
      "automerge": true
    },
    {
      "matchUpdateTypes": ["major"],
      "automerge": false,
      "addLabels": ["needs-review"]
    },
    {
      "matchPackageNames": ["python", "postgres"],
      "matchUpdateTypes": ["major"],
      "addLabels": ["breaking", "needs-review"]
    }
  ],
  "vulnerabilityAlerts": { "labels": ["security"], "automerge": true },
  "lockFileMaintenance": { "enabled": true, "schedule": ["before 6am on monday"] }
}
```

Effect: weekly grouped patch/minor + digest PRs auto-merge after CI; majors get individual PRs requiring review; security advisories cut PRs immediately.

### Test fixtures

`tests/fixtures/emails/<provider>/<scenario>.eml` — anonymized real confirmations, PII scrubbed by `tests/fixtures/scrub.py`. Each fixture has a sibling `.expected.json` with the parsed `Segment` output. New provider = new fixture + parametrized test row.

---

## 10. Open Questions / Future Work

- **Email provider coverage** beyond v1 set — add as needed via new rule modules. Each one is ~50 LOC.
- **Live flight status** (v2) — likely AeroAPI; needs decision on cost model (per-poll vs per-trip).
- **Travel stats** (v2) — countries visited, miles flown, etc. Pure SQL over existing data, fast follow.
- **Multi-leg flight detection** — single email with 2+ legs. Schema supports it (multiple segments per `raw_email_id`); needs care in clustering.
- **Cancellations** — confirmation emails for cancellations should mark existing segments `cancelled` rather than create new ones. Match by confirmation_number + provider.
- **Gate / terminal updates** — could come via email *or* a future flight-status integration.
- **Calendar invites (.ics) inside emails** — sometimes airlines attach them; could be an additional parse strategy.

---

## 11. Risks

| Risk | Mitigation |
|------|------------|
| Parser silently misses segments | Confidence threshold + `/inbox` review queue + raw MIME storage for re-parse |
| Anthropic API outage | Provider rules + JSON-LD cover ~80% on their own; Haiku is the fallback, not the primary |
| forwardemail.net outage | They retry; raw MIME persisted on first successful POST. Monitor webhook delivery in Settings → Stats |
| Wrong timezone display | Always store IANA tz with each timestamp; display logic uses stored tz, never viewer's |
| Share token leak | Hashed at rest; default 30d expiration; sanitized view default; `noindex`; rate-limited |
| Self-hosted user breaks own DB during update | Forward-only migrations with documented manual revert; backup script before update is the documented happy path |
| CVE in a dep | Renovate vulnerability alerts auto-PR; Trivy on every build blocks shipping a vulnerable image |
| User forwards an email containing other people's data | Sanitized share defaults; private documents stay behind auth |

---

## 12. Implementation Sequencing (preview, full plan separate)

Suggested order, each phase shippable:

1. **Skeleton + auth** — FastAPI app, OIDC client, user model, "hello $user" page, Docker compose, CI green.
2. **Ingestion v0** — webhook endpoint, raw email storage, manual segment entry UI.
3. **Parsers** — JSON-LD path → 1-2 provider rules (Delta, Marriott as exemplars) → Haiku fallback.
4. **Trip clustering** — auto-cluster + manual split/merge.
5. **Day-of view + timeline** — the headline UI.
6. **Inbox / review queue** — pre-filled editable form, AI-suggested indicators, Re-ask-with-hint, segment_versions audit trail. Until phase 7 lands, list/search uses Postgres ILIKE on `search_text`.
7. **Meilisearch integration** — sync jobs, ⌘K palette, scoped tenant tokens. Replaces ILIKE in trip/segment lists.
8. **PWA shell + offline cache.**
9. **Map + weather.**
10. **Documents + S3 backend.**
11. **Expenses + FX.**
12. **Public share links + sanitized view.**
13. **ICS feed.**
14. **Remaining provider parsers** (United, AA, Southwest, Hilton, Hyatt, IHG, Hertz, Avis, Enterprise, Amtrak, Booking.com, Airbnb).
15. **Web push notifications.**
16. **Polish, dark mode, settings, stats.**
