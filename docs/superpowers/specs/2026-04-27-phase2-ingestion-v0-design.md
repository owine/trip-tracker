# Phase 2 — Ingestion v0 Design

**Status:** Design (pre-implementation). Approved 2026-04-27.

**Builds on:** Phase 1 (skeleton + auth + CI green). Latest commit on main: `1fb3aa7` at time of writing. v0.1.0 tag corresponds to spec §12.1 complete.

**Parent spec:** [`2026-04-26-trip-tracker-design.md`](./2026-04-26-trip-tracker-design.md). All section references in this document are to the parent spec unless prefixed.

---

## 1. Goal

Stand up the ingestion entry point and the manual itinerary entry path so that the household can:

1. Forward confirmation emails to `<alias>@trips.<domain>` and have them captured (raw MIME stored, deduped, owner identifiable) — even though no parser exists yet.
2. Browse trips and segments and add segments by hand via per-type forms — establishes the data the eventual parsers (Phase 3+) will produce.

This phase is the second of sixteen per spec §12. It is shippable on its own: forwarding works end-to-end and a human can record an itinerary.

---

## 2. Scope

### In scope

- Webhook endpoint `POST /api/ingest/email` (HMAC-SHA256 verified, raw MIME body, 25 MB cap, replay protection).
- Schema: `forwarding_aliases`, `trips`, `trip_travelers`, `segments`, `raw_emails`, `webhook_replay_cache`.
- Manual itinerary UI: trips list, trip detail, type picker, six per-type segment forms (flight / lodging / car / train / transfer / activity), trip + segment edit, segment delete.
- Admin UI: forwarding-alias CRUD, raw-emails list + detail.
- Tests: ≥85 % coverage, real Postgres, ASGI through httpx, pytest-postgresql.

### Out of scope (deferred — phase noted)

| Item | Phase |
|---|---|
| Parsers (json-ld / provider rules / Haiku) | 3 |
| ARQ + Redis + worker container | 3 |
| Re-parse endpoint and bulk job | 3 |
| Auto-clustering of segments into trips | 4 |
| Day-grouped timeline trip view | 5 |
| Inbox / review queue UI | 6 |
| `segment_versions` audit trail | 6 |
| Meilisearch + ⌘K palette | 7 |
| PWA shell, web push | 8 |
| Map + weather | 9 |
| Documents (storage, OCR/extract) | 10 |
| Expenses + FX | 11 |
| Public share tokens, ICS feed | 12–13 |
| Provider parsers beyond JSON-LD seed | 3 + 14 |

---

## 3. Architecture

Phase 2 introduces no new processes, containers, or external services. Everything runs in the existing FastAPI process from Phase 1.

```
                        ┌──────────────────────────┐
   forwardemail.net ───▶│ POST /api/ingest/email   │ ───▶ raw_emails (mime_blob)
   (HMAC-signed MIME)   │  • verify HMAC           │
                        │  • replay-cache check    │
                        │  • parse MIME headers    │
                        │  • INSERT raw_emails     │
                        │  • return 202            │
                        └──────────────────────────┘

   Authenticated user (Phase 1 OIDC)
        │
        ▼
   ┌───────────┐    ┌──────────────────┐    ┌──────────────────┐
   │ /trips    │───▶│ /segments/new?   │───▶│ POST /segments   │
   │  list     │    │   type=<...>     │    │ (per-type schema)│
   └───────────┘    │  + trip selector │    │ ↳ create trip if │
                    └──────────────────┘    │   new            │
                                            │ ↳ create segment │
                                            └──────────────────┘
                                                    │
                                                    ▼
                                            ┌──────────────────┐
                                            │ /trips/:id detail│
                                            │  (flat list)     │
                                            └──────────────────┘

   Admin (current_user.is_admin)
        │
        ▼
   /admin/aliases  (CRUD)         /admin/raw-emails  (list + detail)
```

### Architectural decisions and rationale

| Decision | Choice | Rationale |
|---|---|---|
| Webhook handler synchronous vs queued | Synchronous, no ARQ in Phase 2 | No work to defer until parsers arrive. Critical path is <50 ms. ARQ wires into Phase 3 alongside the parser. |
| Replay-protection cache backing | Postgres table | Survives restarts, no new infrastructure, ten-line interface that Phase 3 can replace with Redis. |
| Forwarding-alias provisioning | Admin UI CRUD only | First-user pain (must visit `/admin/aliases` after first login before forwarding works) is a one-time cost. Avoids the surprise of auto-claimed local-parts. |
| Trip creation flow | Implicit on segment creation | Matches spec invariant "trips are derived, not entered". Auto-derive `start_date`/`end_date`/`primary_destination` from the segment. User edits trip metadata later if desired. |
| Segment form shape | Per-type forms, six templates | Produces structured data closer to what parsers will produce in Phase 3 (IATA codes, hotel addresses, car pickup/dropoff). Generic single form would have to be retrofitted. |
| Unknown alias handling | Persist `raw_emails` row anyway, owner derived lazily via JOIN | Spec §4 step 2 says "flag for review"; Phase 6 inbox surfaces this naturally. Phase 2 surfaces it via `/admin/raw-emails`. Never lose an email. |
| Webhook payload format | Raw MIME (`Content-Type: message/rfc822`) | `raw_emails.mime_blob` is bytea per spec — store the canonical bytes. Re-parse in Phase 3+ needs the original MIME (json-ld extraction, custom headers, attachment ordering). HMAC over raw bytes is straightforward. |

---

## 4. Data model

All schema additions in a single Alembic migration. Constraint naming follows the convention from `models/base.py` established in Phase 1 (`pk_<table>`, `uq_<table>_<col>`, `ix_<table_col>`, `fk_<table>_<col>_<ref>`).

### `forwarding_aliases`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | `default uuid.uuid4` |
| `user_id` | uuid FK → `users.id` | NOT NULL, ON DELETE CASCADE |
| `local_part` | varchar(64) | UNIQUE, NOT NULL, lowercase, RFC-5321 valid local-part chars only |
| `created_at` | timestamptz | server_default `now()` |
| `updated_at` | timestamptz | server_default `now()`, onupdate `now()` |

### `trips`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | `default uuid.uuid4` |
| `title` | varchar(255) | NOT NULL |
| `start_date` | date | NOT NULL |
| `end_date` | date | NOT NULL |
| `primary_destination` | varchar(255) | NULL |
| `notes` | text | NULL |
| `cover_color` | varchar(16) | NULL (hex like `#a78bfa`) |
| `created_by` | uuid FK → `users.id` | NOT NULL, ON DELETE RESTRICT |
| `created_at`, `updated_at` | timestamptz | as above |

CHECK constraint: `end_date >= start_date`.

### `trip_travelers`

| Column | Type | Notes |
|---|---|---|
| `trip_id` | uuid FK → `trips.id` | NOT NULL, ON DELETE CASCADE |
| `user_id` | uuid FK → `users.id` | NOT NULL, ON DELETE CASCADE |
| `role` | varchar(16) | NOT NULL, CHECK IN (`'owner'`, `'companion'`) |
| `created_at` | timestamptz | server_default `now()` |
| PK | (trip_id, user_id) | composite |

### `segments`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | `default uuid.uuid4` |
| `trip_id` | uuid FK → `trips.id` | NOT NULL, ON DELETE CASCADE |
| `owner_user_id` | uuid FK → `users.id` | NOT NULL, ON DELETE RESTRICT |
| `type` | varchar(16) | NOT NULL, CHECK IN (`flight`, `lodging`, `car`, `train`, `transfer`, `activity`) |
| `status` | varchar(16) | NOT NULL, CHECK IN (`confirmed`, `cancelled`, `tentative`), default `confirmed` |
| `confirmation_number` | varchar(64) | NULL |
| `provider` | varchar(128) | NULL |
| `start_at` | timestamptz | NOT NULL |
| `start_tz` | varchar(64) | NOT NULL (IANA name, validated against `zoneinfo.available_timezones()` at write) |
| `end_at` | timestamptz | NULL (nullable for instant events like activity start-only) |
| `end_tz` | varchar(64) | NULL |
| `start_location` | jsonb | NULL, shape `{name, iata?, lat?, lon?, address?, city?, country?}` |
| `end_location` | jsonb | NULL, same shape |
| `details` | jsonb | NOT NULL, default `'{}'::jsonb`, type-specific fields |
| `parse_source` | varchar(64) | NOT NULL (Phase 2: always `'manual'`); width chosen so future values like `'llm-vision:haiku-4-5-20260101'` fit |
| `parse_confidence` | double precision | NOT NULL, CHECK BETWEEN 0 AND 1 (Phase 2: always `1.0`); double rather than real to avoid Phase 3 widening |
| `search_text` | tsvector | GENERATED column (see below), GIN index |
| `raw_email_id` | uuid FK → `raw_emails.id` | NULL ON DELETE SET NULL (Phase 2 manual entries: always NULL) |
| `superseded_by` | uuid FK → `segments.id` | NULL self-FK ON DELETE SET NULL (used by Phase 3 re-parse versioning; Phase 2 never sets) |
| `created_at`, `updated_at` | timestamptz | as above |

Indexes (all explicitly named per the `models/base.py` convention):

- `ix_segments_trip_id` on `(trip_id)`
- `ix_segments_owner_user_id_start_at` on `(owner_user_id, start_at)`
- `ix_segments_start_at` on `(start_at)`
- `ix_segments_search_text` on `(search_text)` `USING gin` — Alembic emits `op.create_index('ix_segments_search_text', 'segments', ['search_text'], postgresql_using='gin')`. SQLAlchemy's metadata naming convention does not auto-name expression-based or alternative-method indexes reliably, so this name is set explicitly.

`search_text` is a generated column. The expression must be IMMUTABLE for Postgres to accept it in a `GENERATED ALWAYS AS ... STORED` column:

```sql
GENERATED ALWAYS AS (
  to_tsvector(
    'simple'::regconfig,
    coalesce(provider, '')                       || ' ' ||
    coalesce(confirmation_number, '')            || ' ' ||
    coalesce(start_location ->> 'name', '')      || ' ' ||
    coalesce(end_location   ->> 'name', '')      || ' ' ||
    coalesce(start_location ->> 'city', '')      || ' ' ||
    coalesce(end_location   ->> 'city', '')
  )
) STORED
```

Notes for the implementer:
- The `'simple'::regconfig` cast (rather than the bare string) is what makes the two-argument `to_tsvector(regconfig, text)` overload resolve to the IMMUTABLE variant.
- `->>` returns `text`, so all `||` operands are `text` — no implicit casts that would break IMMUTABLE.
- A migration test (`tests/test_models_segment.py::test_search_text_generated`) must INSERT a row, then `SELECT search_text` to verify the GENERATED expression actually executed and is non-empty when source fields are non-null.

Phase 2 has no UI consumer for `search_text`, but emitting the column now means the Meilisearch sync in Phase 7 doesn't require a schema migration.

### `raw_emails`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | `default uuid.uuid4` |
| `received_at` | timestamptz | NOT NULL, server_default `now()` |
| `to_address` | varchar(320) | NOT NULL |
| `from_address` | varchar(320) | NOT NULL |
| `subject` | text | NULL |
| `message_id` | varchar(998) | UNIQUE NOT NULL — RFC 5322 max length |
| `mime_blob` | bytea | NOT NULL |
| `headers` | jsonb | NOT NULL — full header dict |
| `parse_status` | varchar(16) | NOT NULL, CHECK IN (`pending`, `parsed`, `failed`, `no_segments`, `review`), default `pending` |
| `parse_error` | text | NULL |
| `created_at` | timestamptz | server_default `now()` |

Indexes: `(received_at DESC)`, `(parse_status)`, `(to_address)`.

### `webhook_replay_cache`

| Column | Type | Notes |
|---|---|---|
| `ts_seconds` | bigint | epoch seconds from `X-Webhook-Timestamp` (column named `ts_seconds`, not `timestamp`, to avoid shadowing the SQL keyword) |
| `nonce` | varchar(64) | from `X-Webhook-Nonce` |
| `expires_at` | timestamptz | NOT NULL — `now() + interval '24 hours'` at insert |
| PK | (ts_seconds, nonce) | composite |

Index: `ix_webhook_replay_cache_expires_at` on `(expires_at)` for cleanup queries.

Cleanup strategy: a periodic-ish opportunistic prune runs at most once every 60 s (gated by a tiny in-memory timestamp guard in the webhook handler) — it is **not** colocated with the nonce-insert transaction. Reasoning: a cleanup-then-insert ordering can race with a 24h-and-a-second replay between two webhook calls; relying on the PK unique constraint as the only enforcement point is correct under all orderings, and cleanup is purely about table size, not correctness. The first webhook in any given minute issues a single `DELETE FROM webhook_replay_cache WHERE expires_at < now()` *before* its own work, in its own short transaction. Subsequent calls within the minute skip the DELETE.

---

## 5. Webhook flow — `POST /api/ingest/email`

### Request shape

- Method: `POST`
- Headers required:
  - `Content-Type: message/rfc822` (raw MIME body)
  - `<configurable>: sha256=<hex>` — HMAC-SHA256 of the raw body, hex-encoded. Header name is `X-Webhook-Signature` by default; configurable via `WEBHOOK_SIGNATURE_HEADER` env to match whatever forwardemail.net actually sends in production.
  - `X-Webhook-Timestamp: <epoch_seconds>` — when forwardemail.net signed.
  - `X-Webhook-Nonce: <opaque_token>` — single-use random token from forwardemail.net.
- Body: raw `.eml` byte stream, ≤25 MB.

### Configuration (env)

| Env var | Type | Default | Notes |
|---|---|---|---|
| `WEBHOOK_SECRET` | `SecretStr` | (required) | Shared secret with forwardemail.net |
| `WEBHOOK_SIGNATURE_HEADER` | str | `X-Webhook-Signature` | Header name to read |
| `WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS` | int | `300` | ±5 min skew tolerated |
| `WEBHOOK_MAX_BODY_BYTES` | int | `26214400` | 25 MiB |

All added to `Settings` in `config.py`. Pydantic validators reject:
- empty / whitespace-only `WEBHOOK_SIGNATURE_HEADER`
- `WEBHOOK_SIGNATURE_HEADER` matching `^(authorization|cookie|host|content-length|content-type|x-forwarded-.*)$` case-insensitive (collision with reserved/proxy headers)
- `WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS` ≤ 0 or > 3600
- `WEBHOOK_MAX_BODY_BYTES` ≤ 0 or > 100 MiB

### Algorithm (in order)

1. **Read body with a streaming size cap.** Iterate `request.stream()` collecting into a `bytearray`. After each chunk, check `len(buf) > WEBHOOK_MAX_BODY_BYTES`; if so, return `413` and discard buffer. Do **not** call `request.body()`, which would buffer without bound. End of loop yields the full body bytes.

2. **Verify HMAC.** Read the configured signature header. If missing or empty → `401`. The header value MUST start with `"sha256="`; if not → `401`. Strip prefix:
   ```python
   raw = headers.get(SIG_HEADER, "")
   if not raw.startswith("sha256="):
       return 401
   provided_hex = raw.removeprefix("sha256=")
   expected_hex = hmac.new(secret_bytes, body, "sha256").hexdigest()
   if not hmac.compare_digest(provided_hex, expected_hex):
       return 401
   ```
   `hmac.compare_digest` is constant-time and accepts equal-length strings; both are 64 hex chars.

3. **Verify timestamp + nonce headers.** Read `X-Webhook-Timestamp` (integer seconds since Unix epoch — the implementer must validate via `int(value)`; non-integer → `400`). Read `X-Webhook-Nonce` (max 64 chars; missing/empty → `400`). If `abs(int(time.time()) - ts) > WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS` → `400`.

4. **Periodic cache prune (best-effort).** Maintain a process-local `_last_prune_at` epoch. If `time.time() - _last_prune_at > 60`, run `DELETE FROM webhook_replay_cache WHERE expires_at < now()` in its own short transaction and update `_last_prune_at`. This is purely table-size hygiene and explicitly **not** load-bearing for replay protection (the PK constraint in step 5 is).

5. **Open a single transaction for steps 5–6.** Inside `async with session.begin():`:
   ```sql
   INSERT INTO webhook_replay_cache (ts_seconds, nonce, expires_at)
   VALUES (:ts, :nonce, now() + interval '24 hours')
   ON CONFLICT (ts_seconds, nonce) DO NOTHING
   ```
   Detect the conflict via the row count of the result (`result.rowcount == 0` ⇒ replay).

6. **Within the same transaction**, parse MIME with `email.parser.BytesParser(policy=email.policy.default).parsebytes(body)`. Extract Message-ID, To, From, Subject, Date, and the full header set as a dict (header values are `str`, with leading/trailing whitespace stripped). Missing Message-ID → synthesize as the literal string `<sha256:<64-hex>@trip-tracker.local>` where the hex is `hashlib.sha256(body).hexdigest()`. **The stored value includes the angle brackets**, per RFC 5322. Then:
   ```sql
   INSERT INTO raw_emails (...)
   VALUES (...)
   ON CONFLICT (message_id) DO NOTHING
   ```
   Detect duplicate via `result.rowcount`.

7. **Decide response code.** Both inserts are now committed (or both rolled back if either threw). Outcome:
   - replay (step 5 conflict) → return `202 Accepted` empty body.
   - duplicate Message-ID (step 6 conflict, step 5 fresh) → return `202 Accepted` empty body.
   - both fresh → return `202 Accepted` empty body.

   Returning `202` uniformly once HMAC + timestamp pass eliminates the side-channel where 200-vs-202 leaks "have I seen this nonce before". forwardemail.net treats both as success regardless.

### Error response shapes

| Code | Cause | Body |
|---|---|---|
| 202 | accepted (whether stored or deduped) | empty |
| 400 | missing/bad timestamp/nonce headers | `{"error": "bad_request", "detail": "..."}` |
| 401 | missing/wrong HMAC, missing `sha256=` prefix | `{"error": "unauthorized"}` |
| 413 | body too large (caught streaming) | `{"error": "payload_too_large", "max_bytes": ...}` |
| 500 | unexpected | `{"error": "internal"}` (logged with stack, no body) |

All non-202 responses are `application/json`; 202 has empty body.

### Logging

Every webhook call emits one structlog `info` line with: `event="ingest_webhook"`, `status=<code>`, `to_address`, `from_address`, `message_id` (truncated to 64), `body_bytes`, `replay=<bool>`, `duplicate_message_id=<bool>`. No body content. No header values beyond the address fields above. Self-host single-tenant context: address fields are household members' own emails, so logging them is acceptable; this is documented here so a future multi-tenant migration knows to revisit.

---

## 6. UI

### Routes summary

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/` | any | Phase 1 home |
| GET | `/trips` | logged-in | List user's trips |
| GET | `/trips/:id` | logged-in + traveler | Trip detail (flat segment list) |
| GET | `/trips/:id/edit` | logged-in + traveler | Edit trip metadata form |
| POST | `/trips/:id` | logged-in + traveler | Update trip |
| POST | `/trips/:id/delete` | logged-in + traveler | Delete trip |
| GET | `/segments/new` | logged-in | Type picker |
| GET | `/segments/new?type=<type>` | logged-in | Per-type form |
| POST | `/segments` | logged-in | Create segment (and trip if new) |
| GET | `/trips/:id/segments/:sid/edit` | logged-in + traveler | Edit segment form |
| POST | `/trips/:id/segments/:sid` | logged-in + traveler | Update segment |
| POST | `/trips/:id/segments/:sid/delete` | logged-in + traveler | Delete segment |
| GET | `/admin/aliases` | logged-in + admin | Alias list |
| GET | `/admin/aliases/new` | logged-in + admin | New alias form |
| POST | `/admin/aliases` | logged-in + admin | Create alias |
| GET | `/admin/aliases/:id/edit` | logged-in + admin | Edit alias form |
| POST | `/admin/aliases/:id` | logged-in + admin | Update alias |
| POST | `/admin/aliases/:id/delete` | logged-in + admin | Delete alias |
| GET | `/admin/raw-emails` | logged-in + admin | Raw email list (paginated) |
| GET | `/admin/raw-emails/:id` | logged-in + admin | Raw email detail |
| POST | `/api/ingest/email` | HMAC | Webhook ingest |

Auth dependency: `require_user` from Phase 1 covers logged-in. New `require_admin` raises 403 if `not current_user.is_admin`. New `require_traveler(trip_id)` raises 404 if user is not in `trip_travelers` for that trip (404 instead of 403 to avoid revealing trip existence).

### Per-type segment form fields

All six forms share these common fields:

- **Trip selector** — `<select>` of existing trips that `current_user` belongs to + `+ New trip` toggle revealing a single `Trip title` text input (max 255). When new-trip mode active, `start_date`/`end_date`/`primary_destination` of the trip are auto-derived on submit.
- **Status** — defaults to `confirmed`, dropdown for `confirmed`/`cancelled`/`tentative`.
- **Confirmation #** — text, max 64.
- **Provider** — text, max 128 (e.g., "Delta", "Marriott Bonvoy", "Hertz").
- **Start datetime** — `<input type="datetime-local">` paired with IANA tz `<select>`.
- **End datetime** — same; nullable for activity.
- **Notes** — textarea; stored at `details.notes`.

Per-type additions:

| Type | Extra fields | Maps to |
|---|---|---|
| flight | Flight number, Airline (provider hint), Origin (IATA), Origin city, Destination (IATA), Destination city, Seat | `details.flight_number`, `provider` (if blank), `start_location.{iata,city}`, `end_location.{iata,city}`, `details.seat` |
| lodging | Hotel name, Address, City, Country, Room type | `start_location.{name,address,city,country}` (also copied to `end_location` since check-out happens at the same place), `details.room_type` |
| car | Pickup location, Pickup city, Dropoff location, Dropoff city, Car class | `start_location.{name,city}`, `end_location.{name,city}`, `details.car_class` |
| train | Operator (provider hint), Train number, Origin station, Destination station, Coach/seat | `provider`, `details.train_number`, `start_location.name`, `end_location.name`, `details.seat` |
| transfer | Pickup location, Dropoff location | `start_location.name`, `end_location.name` |
| activity | Venue name, Address (optional), City | `start_location.{name,address,city}` |

`parse_source='manual'`, `parse_confidence=1.0`, `raw_email_id=NULL` for every Phase 2 entry.

### Trip-detail page layout (Phase 2 simplification)

A single flat table-like list ordered by `start_at`:

```
[icon] Provider / route • Confirmation #          [Edit] [Delete]
       Mon May 12, 2026 09:25 EDT — 12:10 BST
       JFK → LHR
```

No day grouping, no calendar visualization. (Both arrive in Phase 5.)

### Datetime + timezone UX

Server-rendered: form has `<input type="datetime-local">` (browser-local time) plus a `<select>` of common IANA tz names ordered with a tiny piece of inline JS that pre-selects `Intl.DateTimeFormat().resolvedOptions().timeZone` if found in the list, fallback `UTC`.

**Timezone list source:** the static fixture `src/trip_tracker/static/iana_timezones.json` — committed to the repo, generated once from `zoneinfo.available_timezones()` filtered to names containing exactly one `/` and not starting with `Etc/`/`posix/`/`right/`/`SystemV/` (drops platform aliases). Regenerated only when needed via a `make update-tz` target (out-of-scope script for Phase 2; document the regen recipe in the file's docstring).

**Backend conversion (stdlib only — no `pytz`):**
```python
from datetime import datetime
from zoneinfo import ZoneInfo

local_dt = datetime.fromisoformat(form.start_local)         # naive
aware_dt = local_dt.replace(tzinfo=ZoneInfo(form.start_tz)) # localize via tzinfo attach
utc_dt = aware_dt.astimezone(ZoneInfo("UTC"))               # convert
# store: start_at=utc_dt, start_tz=form.start_tz
```
The Pydantic form model validates `form.start_tz in zoneinfo.available_timezones()` and rejects unknown names with a clear field error. (Note: the stdlib idiom is `replace(tzinfo=...)`, **not** `.localize()` — that's a pytz API.)

---

## 7. Module structure

```
src/trip_tracker/
├── ingest/
│   ├── __init__.py
│   ├── webhook.py             # POST /api/ingest/email FastAPI router
│   ├── hmac_verify.py         # verify_signature(), prune_replay_cache(), record_nonce()
│   └── mime.py                # parse_mime(body) -> ParsedEmail dataclass
├── models/
│   ├── forwarding_alias.py
│   ├── trip.py
│   ├── trip_traveler.py
│   ├── segment.py
│   ├── raw_email.py
│   └── webhook_replay.py
├── routes/
│   ├── trips.py               # /trips, /trips/:id, /trips/:id/edit, /trips/:id/delete
│   ├── segments.py            # /segments/new(/?type=*), /segments, /trips/:id/segments/:sid(/edit)
│   └── admin.py               # /admin/aliases/*, /admin/raw-emails/*
├── schemas/
│   ├── __init__.py
│   ├── segment_forms.py       # FlightSegmentForm, LodgingSegmentForm, CarSegmentForm, ...
│   └── trip_forms.py          # TripForm, NewTripFromSegment
├── auth/
│   └── deps.py                # add require_admin(), require_traveler(trip_id)
└── templates/
    ├── base.html              # Phase 1 — extend nav for Trips / Admin links
    ├── trips/
    │   ├── list.html
    │   ├── detail.html
    │   ├── edit.html
    │   └── _row.html          # partial used in list
    ├── segments/
    │   ├── type_picker.html
    │   ├── _common_fields.html  # partial
    │   ├── flight_form.html
    │   ├── lodging_form.html
    │   ├── car_form.html
    │   ├── train_form.html
    │   ├── transfer_form.html
    │   ├── activity_form.html
    │   └── _row.html
    └── admin/
        ├── alias_list.html
        ├── alias_form.html
        ├── raw_email_list.html
        └── raw_email_detail.html

migrations/versions/2026_05_NN_NNNN_phase2_ingestion.py    # one migration, six tables
```

---

## 8. Testing

Test patterns match Phase 1: `pytest-asyncio` in auto mode, real Postgres via `pytest-postgresql`, FastAPI through `httpx.AsyncClient` with `app.router.lifespan_context(app)` so `init_db` runs. No external HTTP in Phase 2 → `respx` not needed.

### New test files

| File | Coverage focus |
|---|---|
| `test_ingest_webhook.py` | Happy path (202), HMAC absent (401), HMAC mismatch (401), HMAC header missing `sha256=` prefix (401), body >25 MB streaming abort (413), timestamp skew (400), non-integer timestamp (400), missing nonce (400), replay returns 202 silently, duplicate Message-ID returns 202 silently, unknown alias still persists (202, owner derived NULL via JOIN), missing Message-ID synthesizes `<sha256:...@trip-tracker.local>`, body containing CRLF + UTF-8 BOM round-trips HMAC correctly (fixture-based against a captured forwardemail.net sample under `tests/fixtures/webhooks/`). |
| `test_ingest_mime.py` | Multipart/alternative, attachments, malformed encoding, missing Message-ID, very long subject (>998 chars truncates safely), header value whitespace stripped. |
| `test_ingest_hmac.py` | `verify_signature` on fixture body returns True for matching hex / False for mismatched hex / False for missing `sha256=` prefix. `prune_replay_cache` clears rows past `expires_at`. `record_nonce` returns False on PK conflict. |
| `test_config_webhook_validators.py` | Pydantic Settings rejects empty signature header / collision header names / out-of-range tolerance / out-of-range max-body. |
| `test_models_forwarding_alias.py` | Uniqueness, FK cascade on user delete, lowercase normalization. |
| `test_models_trip.py` | CRUD, `end_date >= start_date` constraint, FK cascade on owner delete (RESTRICT actually — verify trip can't be orphaned). |
| `test_models_trip_traveler.py` | Composite PK, role check constraint, owner-vs-companion semantics. |
| `test_models_segment.py` | CRUD per type, jsonb roundtrip, generated `search_text` column populated and non-empty when source fields set, `start_tz` IANA validation rejects unknown name, `parse_confidence` boundaries (0, 1, -0.001 rejected, 1.001 rejected). |
| `test_models_raw_email.py` | Message-ID uniqueness, jsonb headers roundtrip, parse_status check constraint. |
| `test_models_webhook_replay.py` | Composite PK, expires_at index pruning. |
| `test_routes_trips.py` | List shows only trips the user travels; detail 404 for non-traveler; edit/update/delete; nav link visibility. |
| `test_routes_segments.py` | Type picker page; each per-type form renders; happy POST per type creates segment + (when new trip) creates trip + trip_traveler in one transaction; trip-creation transactional rollback if segment INSERT fails; existing-trip path auto-widens trip dates when segment falls outside; primary_destination derivation per type (flight ⇒ end city, lodging ⇒ start city, etc.); validation errors re-render form with field messages (200 + form HTML, not 422 — server-rendered convention); edit/update/delete; ownership scoping (can't edit segment in a trip you don't travel — 404). |
| `test_routes_admin.py` | Non-admin gets 403 on `/admin/*`; alias CRUD round-trip; raw-email list paginates and shows owner via JOIN (and `—` for orphans); raw-email detail decodes MIME correctly. |
| `test_auth_deps_admin.py` | `require_admin` raises 403 (not 404) — admin pages may exist; `require_traveler` raises 404 for non-traveler. |

### Coverage target

≥ 85 % (matches Phase 1, pyproject `tool.coverage.report.fail_under = 85`). `__main__.py` already excluded.

### Performance notes

Webhook handler critical path target: <50 ms end-to-end (excluding network). Single query batch: timestamp/nonce upsert + raw_emails upsert. Both can run in one transaction.

---

## 9. Operational concerns

### Forwarding configuration (manual, deploy-time)

For each user with an alias `<local>`:
1. Add a forwardemail.net forwarding rule: `<local>@trips.<domain>` → webhook URL.
2. Webhook URL: `https://trips.<domain>/api/ingest/email`.
3. Configure HMAC secret in forwardemail.net to match `WEBHOOK_SECRET` env.
4. Confirm signature header name matches `WEBHOOK_SIGNATURE_HEADER` env (default `X-Webhook-Signature` — verify with their docs/portal at config time and override the env if they use a different name).

This is one-time per alias, manual. Documented in README addendum landed in this phase.

### Replay cache size

Worst case 24 h × emails-per-hour. Household scale: <100 emails/day → cache stays under a few KB.

### Schema migrations

One migration only. Since Phase 1 has no domain tables besides `users`, this is essentially a clean schema for the application. Forward-only; no data backfill (no parsers yet).

### Security review checklist

- [x] HMAC compared with `compare_digest` (constant-time); explicit `sha256=` prefix enforcement.
- [x] Timestamp skew check rejects replays older than 5 min (defense in depth alongside nonce cache).
- [x] Body size capped via streaming read to bound memory before HMAC compute.
- [x] `WEBHOOK_SECRET` in `Settings` as `SecretStr` (Phase 1 pattern).
- [x] `WEBHOOK_SIGNATURE_HEADER` validated at startup against collision/empty/dangerous values.
- [x] Admin pages use `require_admin` not just `current_user.is_admin` (centralized).
- [x] `require_traveler` returns 404 (not 403) on non-trip-member access to avoid trip-existence enumeration.
- [x] Email body is stored verbatim — no log lines emit body content.
- [x] Raw email detail page's text/plain body rendered server-side, never the HTML body in Phase 2 (defer iframe sandbox to Phase 6 inbox; Phase 2 admin sees text only). HTML body is in `mime_blob` if needed.
- [x] Webhook returns `202 Accepted` uniformly once HMAC + timestamp pass (replay vs duplicate-Message-ID indistinguishable from an attacker's perspective).
- [x] Replay-cache nonce-insert + raw_emails insert in a single DB transaction so a half-state cannot exist.

---

## 10. Acceptance criteria

Phase 2 is "done" when:

1. New `main` commit passes CI green (lint + typecheck + test + security + docker scan).
2. Forwarding-alias CRUD works end-to-end; admin can create/edit/delete aliases.
3. `POST /api/ingest/email` accepts a real forwardemail.net-shaped payload and persists a `raw_emails` row; the same email re-sent (same Message-ID) is a no-op.
4. Replay cache rejects same `(timestamp, nonce)` for 24h; rows past `expires_at` get pruned on the next ingest.
5. A user can create a flight via `/segments/new?type=flight`, picking "+ New trip" with a title, and land on `/trips/:id` showing the new segment.
6. Editing a trip and editing a segment both round-trip correctly.
7. Coverage ≥ 85 %.
8. README has an "Email forwarding setup" subsection.

---

## 11. Open questions / explicit non-decisions

- **forwardemail.net signature header name** — confirmed at deploy-time, configurable via env. No spec dependency.
- **Do we need rate limiting on `/api/ingest/email`?** Defer — forwardemail.net retries are bounded; HMAC failures are trivially detectable in logs. Add Phase 3+ if abuse appears.
- **Should admins see a "Re-process" button on `/admin/raw-emails/:id`?** Defer to Phase 3 when there's something to re-process *with*.
- **HTMX for forms?** Pure HTML POST forms with full-page redirects in Phase 2. HTMX wires in Phase 6 with the inbox. Smaller scope now.
- **Soft-delete vs hard-delete?** Hard delete in Phase 2 (no audit trail until Phase 6). Trip delete cascades to segments via FK.
- **Forwarding-alias `disabled_at` for soft-disable?** Deferred. Compromised aliases can be hard-deleted in Phase 2 (orphaned `raw_emails` retain `to_address` and survive — admin still sees them in the list). If/when we get a use case for "preserve history but stop ingesting", add a nullable `disabled_at` column.
- **`trips.cover_color` format validation?** Deferred. Phase 2 has no UI that picks a color; the column is reserved for Phase 5's timeline. Add a hex-format CHECK constraint when the color picker lands.
- **Trip-detail flat list vs day-grouped:** flat list ordered by `start_at ASC` only. Phase 5 introduces day-grouping. `start_at` is NOT NULL on segments so no null-ordering concern.
- **Body-mismatch on duplicate Message-ID:** if an attacker spoofs an existing Message-ID with different body bytes, the second insert is a no-op (`ON CONFLICT (message_id) DO NOTHING`). The original `mime_blob` is preserved. The webhook returns 202 in both cases (per the silent-uniform 202 policy in §5). No spoofing-via-replay risk because the nonce cache catches the replay first.
