# TripIt Wrapper Pivot — v1.0.0 Design

**Status:** Draft (brainstormed 2026-05-06; awaiting review)
**Supersedes:** The "TripIt clone" architecture established through Phase 9 (v0.9.x line)
**Target version:** `v1.0.0`
**Branch strategy:** All work on a `v2` branch cut from `main` post-v0.9.0 tag; cutover via fast-forward merge.

---

## Background and motivation

Today, `trip-tracker` is a self-hosted, multi-user itinerary aggregator that owns its own trip and segment identity. Through Phases 1–9 we built rich intake (email forwarding via ForwardEmail webhook, JSON-LD parser, Haiku LLM fallback), a viewer (trip list + detail + Phase 7 map + weather + Phase 8 expenses), and a consolidation/inbox/undo subsystem (Phase 9, just shipped at v0.9.x).

The owner has decided to pivot the app to be a **wrapper around TripIt's API** rather than a standalone TripIt clone. The intake pipelines and the desktop viewer remain valuable, but trip identity moves to TripIt. TripIt's mobile apps then provide on-the-go viewing; this app provides intake, expenses, and a richer desktop viewer than TripIt's own web UI.

The owner has manually moved all existing trip data into TripIt directly. The local DB will be wiped at cutover; no migration script is needed.

This is a single-user installation (the owner). The multi-user machinery — signup, password hashing, trip-traveler joins, expense splitting across travelers, per-pair merge dismissals — is removed.

---

## Goals and non-goals

### Goals

- TripIt is the source of truth for **trip identity, segment data, and trip-level notes**. Local DB caches these for fast read access.
- This app is the source of truth for **raw intake artifacts (emails, pasted text, uploaded documents), parse audit, expenses, and document storage**. TripIt has no equivalent for these.
- Three intake modalities are supported: email forwarding (existing), pasted freeform text (new), and document upload (new). All converge on a single parse → review → push pipeline.
- The desktop viewer keeps full Phase 7 / Phase 8 functionality (map, weather, expenses, documents) and reads from the local cache only — no live TripIt calls in the page-render path.
- A 10-minute "undo last attach" affordance handles the common misclassification case without requiring users to fix it inside TripIt's app.
- Single-user collapse: signup/login/passwords are removed; auth is a shared session token cookie set from an env var.

### Non-goals

- **No data migration from old schema.** Owner has moved data to TripIt; the local DB is wiped at cutover.
- **No multi-user support.** Re-introducing it later is possible (a single seeded `user` row keeps FKs ergonomic) but not in scope for v1.
- **No TripIt-feature parity for trip CRUD.** Trip creation/edit/delete inside TripIt's own UI is the canonical path; this app pushes intake to TripIt but doesn't expose generic trip CRUD.
- **No expense splitting across users.** Single-user; expenses belong to the owner.
- **No conflict resolution beyond per-field ownership.** TripIt-owned fields and locally-owned fields are partitioned cleanly; there is no field that both can write.

---

## Architecture overview

### Source of truth split (hybrid model)

| Owned by TripIt (canonical there, cached here) | Owned by this app (TripIt has no equivalent) |
|---|---|
| Trip identity (`tripit_trip_id`), display name, dates, primary location | Raw email / pasted text / uploaded document blobs + parse audit |
| Segments (air, lodging, car, rail, transport, activity) and their fields | Expenses (per-trip line items, currency, totals) |
| Trip-level notes | Inbox state (review queue, candidate scoring, attach decisions) |
| | Document files (the actual PDFs/images on disk) |
| | Per-segment "source intake" pointer (`raw_email_id` / `raw_text_id` / `raw_document_id`) |

The per-field partitioning means there are no real merge conflicts: pulling from TripIt updates only TripIt-owned fields; intake/expense writes only touch locally-owned fields.

### Five data-flow paths

1. **Intake → push.** Email webhook / paste textarea / document upload → parse (JSON-LD, then Haiku LLM, then Haiku vision for documents) → `attach_decider` (auto-confirm if confidence high; inbox review otherwise) → push segments to TripIt → cache the resulting `tripit_trip_id` + segments locally.
2. **TripIt → us pull.** Notification API webhook (push) and a 60-minute `modified_since` polling cron (fallback) trigger incremental pulls. A daily 03:00 full-list reconcile catches deletions (which the modified-since API does not report).
3. **Viewer reads.** Web UI reads only the local cache. Trip detail page joins TripIt-sourced trip/segments with locally-owned expenses + documents + parse audit.
4. **Affordances write back.** "Undo last attach" (within 10 minutes) deletes the just-pushed segments from TripIt and restores the source intake row to the inbox. "Force new trip" bypasses overlap detection and pushes as a new TripIt trip.
5. **Expenses stay local.** Auto-Expense-on-segment-create still fires when the pull cron sees a new segment land. Expense CRUD never touches TripIt.

### External dependencies

**Added:** TripIt OAuth 1.0a credentials (one consumer key/secret + one user access token/secret, single-user). Anthropic Haiku vision (already accessible via existing `ANTHROPIC_API_KEY`).

**Unchanged:** PostgreSQL, Redis (for saq), Meilisearch.

**Removed:** Argon2 password hashing (`passlib` dep drops). Any signup/magic-link flow.

---

## Data model

### Tables dropped entirely

- `trip_traveler` — multi-user join, no longer needed.
- `trip_merge_dismissal` — Phase 9 dismissal-pair table (no per-pair dismissal in the new model).
- (No drop of `user`; see "single seeded user" below.)

### Tables radically simplified

- **`trip`** — drops `created_by`, `merged_into_id`, `merged_at`, `merge_audit`. Adds:
  - `tripit_trip_id` (text, unique, nullable until first push)
  - `tripit_synced_at` (timestamptz, nullable)
  - `tripit_etag` (text, nullable; reserved for future conditional fetches)
  - `upstream_deleted_at` (timestamptz, nullable; set by the daily reconcile when a cached trip is missing from `/v1/list/trip`)
- **`segment`** — drops anything Phase-9-specific. Adds:
  - `tripit_segment_id` (text, unique, nullable until pushed)
  - `tripit_segment_type` (enum: `air`/`lodging`/`car`/`rail`/`transport`/`activity`)
  - `tripit_synced_at` (timestamptz, nullable)
  - Existing parse-audit fields (`raw_email_id`, `parser_used`, `confidence`) stay.
- **`raw_email`** — unchanged.

### Tables added

- **`raw_text`** — pasted-blob intake. Columns: `id`, `body_text`, `hint` (optional), `submitted_at`, `parser_audit JSONB`, `candidates JSONB` (overlap-detection result, persisted at parse time per Section 5 below).
- **`raw_document`** — uploaded-file intake. Columns: `id`, `filename`, `mime_type`, `sha256` (unique), `storage_path` (relative to `data/uploads/`), `submitted_at`, `parser_audit JSONB`, `candidates JSONB`, `attach_only BOOL` (skip-parse opt-in).
- **`tripit_oauth_credentials`** — single-row table holding `consumer_key`, `consumer_secret`, `access_token`, `access_token_secret`, `created_at`, `last_refreshed_at`, `last_error` (nullable).
- **`tripit_sync_state`** — single-row table tracking pull cursor: `last_modified_since` (unix timestamp), `last_pull_at`, `last_full_reconcile_at`, `last_error`.
- **`tripit_notification_log`** — inbound webhook audit. Columns: `id`, `received_at`, `raw_payload JSONB`, `processed_at` (nullable), `error` (nullable). Useful for debugging; pruned after 30 days by a saq cron.
- **`attach_audit`** — undo + forensic record. Columns: `id`, `tripit_trip_id`, `pushed_segment_ids JSONB`, `pushed_at`, `source_kind` (`email`/`text`/`document`), `source_id`, `undone_at` (nullable). Within `pushed_at + 10min AND undone_at IS NULL`, the row is actionable for undo.

### Tables kept mostly as-is

- **`expense`** — drop `created_by_id`. Otherwise unchanged.
- **`document`** — repurposed as post-parse archival blob, FK to either `segment.id` or `raw_document.id`.

### Single seeded user

A single `user` row (`id=1`, email from env `OWNER_EMAIL`) is created idempotently on first boot if absent. FK convenience only — no signup, no login form, no password reset. All previous `created_by`/owner FK columns are removed except where they would have referenced this row anyway.

### Email + intake table separation rationale

Three separate `raw_email` / `raw_text` / `raw_document` tables (rather than a polymorphic `raw_intake` with discriminator) because each has distinct required fields (email needs `from_address` + `subject`; text has neither; document has `mime_type` + `storage_path`). Three thin tables read better than one wide-with-many-nulls table.

---

## TripIt integration layer

New package: `src/trip_tracker/tripit/`.

### Module layout

- **`client.py`** — async HTTP client wrapping TripIt's REST API. Built on `httpx.AsyncClient`. OAuth 1.0a signing per request via `authlib`. Single class `TripItClient` with methods:
  - `list_trips_modified_since(unix_ts: int) -> list[TripItTrip]`
  - `list_all_trips() -> list[TripItTrip]` (for daily full reconcile)
  - `get_trip(tripit_trip_id: str) -> TripItTrip`
  - `create(payload: dict) -> TripItCreateResponse` (used for both new trips and adding segments to existing trips)
  - `replace_segment(segment_type: str, tripit_segment_id: str, payload: dict)`
  - `delete_segment(tripit_segment_id: str)`
  - `delete_trip(tripit_trip_id: str)`
  - `subscribe(notification_type: str = "trip")` — Notification API
  - `unsubscribe(notification_type: str | None = None)`

  All endpoints use the `/format/json` path-style format selector. Format is **not** a query parameter on TripIt's API.

- **`models.py`** — Pydantic models mirroring TripIt's response shapes (`TripItTrip`, `TripItAirSegment`, `TripItLodgingSegment`, `TripItCarSegment`, `TripItRailSegment`, `TripItTransportSegment`, `TripItActivitySegment`). String-typed numbers and inconsistent date formats normalized at the boundary.

- **`mappers.py`** — bidirectional translation between local `Segment`/`Trip` models and TripIt's payloads. `to_tripit_payload(segment) -> dict` and `from_tripit_response(tripit_trip) -> tuple[Trip, list[Segment]]`. Type-specific replace endpoint dispatch (`/v1/replace/air/id/...`, `/v1/replace/lodging/id/...`, etc.) lives here.

- **`oauth.py`** — one-time OAuth 1.0a dance, CLI-driven (see "Single-user identity and OAuth bootstrap" section). Uses `oauth_callback=oob` for out-of-band verification.

- **`sync.py`** — saq cron jobs:
  - `pull_tripit_changes(modified_since: int | None = None)` — incremental pull via `list_trips_modified_since`. Triggered both by webhook receipt and by the 60-minute fallback cron.
  - `daily_full_reconcile()` — 03:00 local; calls `list_all_trips`, soft-marks any local cached trip not in the response with `upstream_deleted_at = now()`. Surfaces these in the inbox for confirmation.
  - `prune_notification_log()` — daily; deletes `tripit_notification_log` rows older than 30 days.

- **`errors.py`** — typed exceptions:
  - `TripItAuthError` (token revoked → red banner, sync paused)
  - `TripItRateLimitError` (with Retry-After honored)
  - `TripItValidationError` (payload rejected → segment to inbox `push_failed` state)
  - `TripItUnavailableError` (5xx → exponential backoff via `tenacity`)

### Endpoints, normalized

| Operation | TripIt endpoint |
|---|---|
| List trips | `GET /v1/list/trip/format/json` |
| List trips modified since | `GET /v1/list/trip/modified_since/<unix-ts>/format/json` |
| Get a single trip | `GET /v1/get/trip/id/<tripit-trip-id>/format/json` |
| Create trip or add segment | `POST /v1/create/format/json` (XML/JSON body) |
| Replace segment (per type) | `POST /v1/replace/<type>/id/<segment-id>/format/json` where `<type>` is `air`/`lodging`/`car`/`rail`/`transport`/`activity` |
| Delete segment (generic) | `POST /v1/delete/segment/id/<segment-id>/format/json` |
| Delete trip | `POST /v1/delete/trip/id/<trip-id>/format/json` |
| Subscribe to notifications | `POST /v1/subscribe?type=trip` |
| Unsubscribe | `POST /v1/unsubscribe[?type=trip]` |

### Modified-since deletion gap

TripIt's docs are explicit: "deleted objects will not be considered as a change." The incremental pull therefore cannot detect deletions. Mitigation: the `daily_full_reconcile` cron at 03:00 local pulls the full trip list and soft-marks any cached trip not present as `upstream_deleted_at = now()`, surfacing it in the inbox.

### Notification webhook

`POST /api/tripit/notification` (HTTPS-required, no `?` or `&` in path per TripIt's constraint). Behavior:

1. Returns HTTP 200 always (per the FE webhook lesson — non-200 causes downstream retry storms or silent failure).
2. Logs payload to `tripit_notification_log`.
3. Enqueues `pull_tripit_changes(modified_since=last_modified_since)` to saq. The webhook payload tells us *something* changed but a `modified_since` pull is the cheapest way to handle bulk changes.
4. TripIt suppresses notifications for 10 minutes after an initial notification per their throttling — design accordingly: a webhook receipt is a hint, not a guarantee of all changes.

### `last_modified_since` cursor concurrency

Both the webhook handler and the 60-min fallback cron call `pull_tripit_changes`. Race-free updates are critical — losing a window means missed changes; double-pulling is wasted work but not corrupting. The pattern:

1. Read `tripit_sync_state.last_modified_since` and the response timestamp from TripIt's `<timestamp>` field.
2. Apply upserts.
3. Update `tripit_sync_state.last_modified_since = response_timestamp` only on successful upsert commit, gated by `WHERE last_modified_since < response_timestamp` so a slower-completing pull cannot regress a faster one.
4. Enqueue jobs are **deduplicated by saq's job key** so two webhook receipts within seconds collapse to one pull.

### Rate limiting and resilience

TripIt does not document its rate limit. We implement defensively:

- Token bucket at 30 req/min in `client.py` (conservative; can tune up if observed limit is higher).
- Exponential backoff on 429/5xx via `tenacity`.
- Circuit breaker that opens after 5 consecutive failures and surfaces a UI banner.
- Shared `httpx.AsyncClient` with `http2=True` and 10s timeout.

### Failed-push handling

When push to TripIt fails (validation, rate limit exhaustion, auth revocation), the segment stays in the inbox in a new state: `push_failed`. The inbox UI surfaces it with the error and a retry button. Source intake rows (raw email, raw text, raw document) are never destroyed until a successful push acknowledges receipt.

### Inbox state machine (canonical enumeration)

Every inbox row is in exactly one of these states at any time. There are no other states; an implementer encountering a need for a fifth state should treat that as a design escalation, not a free addition.

| State | Meaning | Entry trigger | Exit trigger |
|---|---|---|---|
| `needs_review` | Parse complete; `attach_decider` punted to human | `decide_attach` returns `NEEDS_REVIEW`, OR an undo restores a prior auto-attach | User clicks Confirm-and-attach / Confirm-create-new / Discard |
| `recently_attached` | Auto-attached or user-confirmed; pushed to TripIt successfully | Successful TripIt push acknowledged | Removed from inbox surface 24h after `pushed_at` (still queryable for forensic) |
| `push_pending` | Parse complete but TripIt not pushable right now (auth revoked, circuit broken) | TripIt circuit open or auth lost at push time | Auth restored / circuit closed → automatic retry → transitions to `recently_attached` or `push_failed` |
| `push_failed` | TripIt rejected the push (validation error, persistent failure beyond retries) | Push retries exhausted with non-transient error | User clicks Retry (back to push attempt) or Discard |

### Testing strategy

- **Unit:** `mappers.py` round-trips against captured TripIt response fixtures in `tests/fixtures/tripit/`.
- **Integration:** `FakeTripItServer` (in-process aiohttp app with dict-backed state) responds with realistic TripIt payloads. Sync cron and client tests run against it. No live TripIt calls in CI ever.
- **Live smoke:** `scripts/tripit_live_smoke.py` (gated behind `TRIPIT_SMOKE=1` env). Run manually after major changes.

---

## Intake pipelines

Three intake paths converge on a **single parse → `attach_decider` → push** pipeline. Differences are confined to the `raw_*` table written and the parser used.

### Path 1 — Email (existing, lightly modified)

`POST /api/ingest/forwardemail` stays as today through the parse step. Change is at the end: instead of writing parsed segments directly to a locally-owned trip, hand the parsed `(trip_shape, segments)` to `attach_decider`. Returns 200 always.

### Path 2 — Paste blob (new)

- `GET /inbox/paste` — page with a textarea (rows=20), optional "hint" field, submit button.
- `POST /inbox/paste` — writes a `raw_text` row, enqueues `parse_raw_text(raw_text_id)`, returns redirect to `/inbox`.
- `parse_raw_text` job: runs JSON-LD detection (some pasted hotel HTML retains it), then Haiku LLM with the same prompt as the email path. Confidence + segments → `attach_decider`.

### Path 3 — Document upload (new)

- `GET /inbox/upload` — drag-and-drop area + file picker. Multi-file supported.
- `POST /inbox/upload` — for each file:
  1. Validate MIME against allowlist (PDF, JPG, PNG, HEIC); 415 on rejection.
  2. Compute SHA-256.
  3. Dedup against existing `raw_document.sha256`; skip + flash if duplicate.
  4. Write file to `data/uploads/{yyyy}/{mm}/{sha256}.{ext}`.
  5. Insert `raw_document` row.
  6. Enqueue `parse_raw_document(raw_document_id)`. Optional `attach_only=true` form field skips parse entirely.
- `parse_raw_document` job:
  - **PDF with text layer:** extract via `pypdf`, feed text to Haiku LLM.
  - **PDF without text layer / image:** send file directly to Haiku vision API. Prompt asks for structured segment JSON. No tesseract step; vision handles layout.
  - **HEIC:** convert to JPEG via `pillow-heif` first, then vision.
  - Result → `attach_decider`.

### Storage

Local filesystem under `data/uploads/`. No S3 / R2 / MinIO for v1 — single-user, self-hosted, local FS is genuinely fine and avoids the credential/cost surface.

---

## Overlap detection and `attach_decider`

### `attach_decider`

```
async def decide_attach(parsed: ParsedIntake, source_kind: str, source_id: UUID) -> AttachOutcome:
    candidates = await find_tripit_overlap_candidates(parsed)
    if not candidates and parsed.confidence >= 0.85:
        return AttachOutcome.AUTO_NEW_TRIP
    if candidates and candidates[0].score == "HIGH" and parsed.confidence >= 0.85:
        return AttachOutcome.AUTO_ATTACH(candidates[0])
    return AttachOutcome.NEEDS_REVIEW(candidates)
```

`AUTO_*` outcomes immediately push to TripIt and write `attach_audit` for the 10-minute undo window. `NEEDS_REVIEW` lands in the inbox with the candidate list pre-rendered.

Threshold rationale: cost asymmetry. A wrongly-auto-attached segment costs ~30 seconds of undo; a wrongly-blocked-in-inbox item costs ~5 seconds of clicking confirm. Bias toward inbox; tighten only after telemetry shows >95% inbox-accept rate.

### Candidate search source

The local cache (`trip` table — populated by the 15-/60-min sync). Reads of TripIt's `/list/trip` per parse would burn rate limit and add 200–500ms latency per intake. Cache is at most ~60 minutes stale, irrelevant for "does this overlap an existing trip" since trip windows don't shift second-to-second.

Fallback: if no candidates found and confidence is low enough that we'd punt to inbox anyway, do a live `list_trips_modified_since(now() - 1h)` refresh before deciding.

### Scoring

| Score | Trigger |
|---|---|
| **HIGH** | Date window of parsed segments fully within trip's window AND any endpoint matches a city/airport already in the trip. |
| **MEDIUM** | Date window overlaps trip window AND endpoint match within 100 km (great-circle, using existing coords table). |
| **LOW** | Date window overlaps trip window, no endpoint match. |
| **(no candidate)** | No date overlap. |

### Difference from Phase 9

The home-anchored gap-agnostic deferral logic is dropped. Phase 9 needed it because trip dates were inferred from segments. With TripIt as source of truth, trip dates are user-set values; if a TripIt trip says June 1–30, anything in that window is a candidate.

### Tie-breaking

Multiple HIGH candidates: sort by (a) earliest date overlap distance, (b) most endpoint matches, (c) most recent `tripit_synced_at`. Surface top 3 in the inbox.

### Persistence (no N+1)

When `attach_decider` returns `NEEDS_REVIEW`, the candidate list is serialized to a JSONB column on the source row (`raw_email.candidates`, `raw_text.candidates`, `raw_document.candidates`). The inbox view reads this directly. If TripIt's trip list changes between parse and review, candidates may be stale; the user sees "create new" as the recovery path, which is self-healing.

This is the architectural improvement the v0.9.1-perf TODO contemplated, now natural under the new model.

### Padding

The candidate query pads by 7 days on each side (`end_date >= parsed.min_date - 7days`) to catch a hotel checked into the night before a flight, etc. Tight match (HIGH) requires within-window though — respects user-set TripIt boundaries.

---

## Viewer

### Reading model

Viewer routes read **only the local cache** — no live TripIt calls in the read path. Stale window bounded by sync cadence (~15 min worst-case under webhook+poll, ~60 min if webhook fails).

### Routes preserved (with internal changes)

| Route | New behavior |
|---|---|
| `GET /trips` | Trip list, no user filter, sourced from cache. Drops merged-out filter. Adds "synced N min ago" footer. |
| `GET /trips/{id}` | Same UI as today; sources joined: trip + segments from cache (TripIt-owned), expenses + documents from local tables. Drops 410 soft-delete branch. Header gets "Open in TripIt →" deep link to `https://www.tripit.com/trip/show/id/{tripit_trip_id}`. |
| `GET /trips/{id}/map` | Unchanged — reads from cache. |
| `GET /expenses/...` | Unchanged. |
| `GET /documents/...` | Unchanged on the surface. Internal FK rewiring per data model. |

### Routes deleted

- `POST /trips/{source}/merge-into/{target}` (Phase 9 C1)
- `POST /trips/{target}/undo-merge/{source}` (C2)
- `POST /trips/{id}/dismiss-merge/{other_id}` (C3)
- All routes that mutate trip identity locally: `GET /trips/new`, `POST /trips`, `PATCH /trips/{id}/dates`, etc.

### Routes added

- `POST /trips/{id}/refresh` — small button on trip detail; triggers a single-trip pull (`client.get_trip`). Rate-limited 1/min/trip.
- `GET /sync/status` — admin-ish page showing last pull time, last error, queue depth.
- `POST /api/tripit/notification` — Notification API webhook (per integration layer section).

### Per-segment freshness affordance

Each segment row gets a small TripIt-logo badge with sync-time tooltip ("synced from TripIt 8 min ago"). Sets the right mental model for cached reads. Segments that originated locally and are mid-`push_failed` get a "pending" badge with retry link.

---

## Affordances: undo + force-new

### Undo (10-min window)

- Every successful auto-attach or auto-new-trip push writes an `attach_audit` row.
- "Recently attached" inbox bucket and trip detail page header surface an Undo button when `now() - pushed_at < 10min AND undone_at IS NULL`.
- `POST /attach/{audit_id}/undo`:
  1. For each `pushed_segment_id`: `client.delete_segment(...)`.
  2. If attach was auto-new-trip and trip now has zero segments: `client.delete_trip(...)`.
  3. Set `attach_audit.undone_at = now()`. The row is **kept** (not deleted); `undone_at IS NOT NULL` is the audit trail of "this attach existed and was reversed."
  4. Restore source intake row (`raw_email` / `raw_text` / `raw_document`) to inbox in `needs_review` state, preserving `candidates` JSONB so the user can pick a different target without re-parsing.
  5. Enqueue immediate single-trip pull for the affected trip.
- Failure modes (TripIt down, segment already deleted in TripIt's app, etc.): surface partial-undo state in UI; do not silently swallow.

### Force-new

- "Confirm + create new TripIt trip" button on `needs_review` items posts to `POST /inbox/{raw_id}/confirm?force_new=true`.
- Bypasses `attach_decider`; calls `client.create(...)` with a fresh trip name derived from parsed segments (e.g., "Paris — Jun 14 2026").
- Same `attach_audit` write happens; 10-min undo applies symmetrically.

### Surfacing

- "Recently attached" badge counter in nav (visible from any page); resets on inbox visit.
- Self-notification email sent on every successful auto-attach: one-line summary plus undo link. Leverages existing SMTP-out infrastructure already wired for the FE webhook side.
- Flash banner after successful undo: "Attach reversed; back in review queue" with link to restored inbox row.

---

## Single-user identity and OAuth bootstrap

### Local identity layers

1. **Process-level identity:** Single seeded `user` row (`id=1`, `email=$OWNER_EMAIL`). FK convenience only. Created idempotently via Alembic data migration.
2. **Request authentication:** Single shared session secret in env (`OWNER_SESSION_TOKEN`, 32 random bytes hex-encoded). A dedicated `GET /auth/bootstrap?token=<secret>` route validates the query token, sets a long-lived signed cookie, and 302s to `/`. All other routes are gated by middleware that requires the cookie. The bootstrap route exists only to keep the cookie-setting logic in one place; subsequent visits go straight through middleware.

The single-token-cookie pattern is the lightest auth that prevents drive-by access on a self-hosted internet-reachable install. Token rotation is manual (regenerate env, restart) — acceptable trade for single-user simplicity.

### Removed

- Signup, login, logout routes
- Argon2 password hashing (`passlib` dep drops)
- Magic-link email flow if present
- All `current_user` checks branching on multi-user logic; collapse to authenticated-as-owner: yes/no

### OAuth bootstrap (one-time CLI)

```
$ uv run trip-tracker tripit-auth
Step 1/4: Generating request token...
   ✓ Request token: abc...
Step 2/4: Open this URL in a browser, log into TripIt, and authorize:
   https://www.tripit.com/oauth/authorize?oauth_token=abc...&oauth_callback=oob
Step 3/4: Paste the verification code TripIt shows you: 12345
   ✓ Exchanging for access token...
Step 4/4: Saving credentials to database...
   ✓ Saved. Sync will start within 1 minute.
```

- `oauth_callback=oob` is TripIt's mode for non-web-app OAuth — TripIt shows a verifier code on screen instead of redirecting.
- Consumer key/secret come from env (`TRIPIT_CONSUMER_KEY`, `TRIPIT_CONSUMER_SECRET`); registered with TripIt at app creation time (manual support@tripit.com email).
- Access token/secret written to `tripit_oauth_credentials` (single-row table).
- Re-running overwrites prior credentials.

### Token revocation handling

On TripIt 401:

1. Mark `tripit_oauth_credentials.last_error` with timestamp.
2. Disable further sync attempts (circuit-broken).
3. Surface red banner: "TripIt connection lost — run `trip-tracker tripit-auth` to reconnect."

Intake still works during revoked-auth state; pasted/forwarded items pile up in `push_pending`, flush automatically on re-auth.

### Env var inventory after pivot

**Surviving:** `DATABASE_URL`, `REDIS_URL`, `MEILI_HOST`, `MEILI_MASTER_KEY`, `ANTHROPIC_API_KEY`, `FORWARDEMAIL_*`, `OPEN_METEO_*`, SMTP-out vars.

**New:** `TRIPIT_CONSUMER_KEY`, `TRIPIT_CONSUMER_SECRET`, `OWNER_EMAIL`, `OWNER_SESSION_TOKEN`.

**Removed:** signup/password/magic-link related vars.

---

## Rollout

### Branch and version strategy

- `main` proceeds through the existing v0.9.0 wrap (C6, C7, C8, W1, W2). Tag `v0.9.0`. This closes the "TripIt clone" era.
- Cut `v2` branch from the v0.9.0 tag. All v1.0.0 work happens on `v2` with frequent small commits.
- Cutover: drop the prod DB schema (owner has moved data to TripIt — confirmed), `git merge v2 → main` (fast-forward when possible), tag `v1.0.0`.

### Critical-path Day-0 task

Owner sends `support@tripit.com` two simultaneous emails:

- Register an API consumer (consumer key/secret).
- Register the Notification API callback URL (`https://<owner-domain>/api/tripit/notification`).

These have unknown turnaround. Sending now lets TripIt's response and the design-and-plan work proceed in parallel. Phase 11/12 (single-user collapse + schema migration) can begin while waiting; Phase 10 (TripIt client + spike) waits on credentials.

### Phase sequencing on `v2`

| Phase | Scope | Rough size |
|---|---|---|
| **10 (spike + integration core)** | Day-0 spike: live OAuth dance, capture API fixtures, document quirks in `docs/tripit-api-notes.md`. Then build `client.py`, `mappers.py`, `oauth.py`, `errors.py`, `FakeTripItServer`, OAuth CLI. | 1 day spike + 3–4 days build |
| **11 (single-user collapse)** | Rip multi-user/signup/passwords; seed owner row; cookie auth; drop `trip_traveler`, `trip_merge_dismissal`; delete merge/undo/dismiss/410 routes | 2–3 days |
| **12 (schema migration)** | Drop trip CRUD fields; add `tripit_*_id`/`tripit_synced_at`/`upstream_deleted_at` to `trip` and `segment`; add `raw_text`, `raw_document`, `tripit_oauth_credentials`, `tripit_sync_state`, `tripit_notification_log`, `attach_audit` | 1–2 days |
| **13 (sync + viewer rewire)** | Pull cron + sync state + retry/circuit; Notification API webhook; daily reconcile; integrate cache reads into trip list + detail; "synced N min ago" footers + per-segment badges; refresh button; deep links; sync status page | 4–5 days |
| **14a (decider + push)** | `attach_decider`; rewire email path to push-not-store; inbox bucket reshape with state machine (needs_review / recently_attached / push_pending / push_failed); candidates persisted at parse | 3–4 days |
| **14b (undo + force-new + notify)** | 10-min undo flow with `attach_audit`; force-new affordance; email self-notification on auto-attach | 2–3 days |
| **15 (new intake modalities)** | Paste-blob page; document upload + Haiku vision parsing; HEIC support | 3–4 days |
| **Cutover** | Drop prod DB, deploy v2, smoke, tag v1.0.0 | 0.5 day |

Total: ~3–4 weeks of focused work; calendar-time longer with interruptions.

### Day-1 spike scope

Before Phase 10 proper, one focused day:

1. Confirm consumer key/secret from TripIt support has arrived.
2. In `scripts/spike_tripit.py` throwaway: OAuth dance, fetch own trip list, pretty-print a few trips, create a hardcoded test trip + segment, delete it.
3. Capture real API response payloads to `tests/fixtures/tripit/`.
4. Document surprises in `docs/tripit-api-notes.md` — naming inconsistencies, undocumented required fields, weird date formats, rate-limit headers behavior, notification webhook payload shape.

### Risk register

| Risk | Mitigation |
|---|---|
| TripIt support takes >2 weeks to register consumer | Send email Day 0; Phase 11/12 work in parallel; only Phase 10+13+14 block |
| TripIt deprecates/changes API mid-build | Pinned response fixtures + FakeTripItServer; live calls only in smoke script |
| TripIt rate limit tighter than expected | Token bucket + backoff; observed rate logged; tighten if needed |
| OAuth 1.0a quirks bite | `authlib` (mature) handles signing; never hand-roll |
| Vision parsing worse than expected | Phase 15 fallback: low-confidence vision → drop to inbox in `attach_only` mode for manual processing |
| Notification webhook unreliable | 60-min polling fallback always runs; daily full-list reconcile catches deletions |
| Mid-build pivot of requirements | Each phase independently revertable; v2 never touches main until cutover |

### Pre-flight checks before Phase 10 spike

- TripIt developer registration complete: consumer key/secret in password manager
- Notification API callback URL registered
- v0.9.0 tagged and shipped
- This design doc reviewed via `spec-document-reviewer` and any issues addressed

---

## Open questions

None blocking. The following will be settled during the Phase 10 spike:

- TripIt's actual rate limit ceiling (currently designed conservatively at 30 req/min).
- Notification API payload shape (designed to be tolerant; spike will confirm).
- Whether HEIC vision quality justifies the `pillow-heif` step or whether direct HEIC-to-vision works.
- Daily-reconcile timing — 03:00 local is a guess; may shift based on observed quiet windows in TripIt's API performance.
