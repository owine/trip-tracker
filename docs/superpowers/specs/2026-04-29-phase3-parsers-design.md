# Phase 3 — Parsers Design

**Status:** Design (pre-implementation). Approved 2026-04-29.

**Builds on:** Phase 2 (Ingestion v0 — webhook + manual segment entry). Latest commit on main at time of writing: `d4a8187`. v0.2.0 tag corresponds to Phase 2 §17 complete.

**Parent spec:** [`2026-04-26-trip-tracker-design.md`](./2026-04-26-trip-tracker-design.md). All section references in this document are to the parent spec unless prefixed.

**Prior phase spec:** [`2026-04-27-phase2-ingestion-v0-design.md`](./2026-04-27-phase2-ingestion-v0-design.md).

---

## 1. Goal

Replace the Phase 2 manual `/segments/new` flow as the primary path from email to itinerary. After v0.3.0, forwarding a confirmation email to `<alias>@trips.<domain>` results in:

- A structured `Segment` row written automatically for the common case (~90% of inbox volume).
- Auto-clustering into an existing trip — or auto-creation of a new trip — without user intervention when confidence is high.
- Routing to the new `/inbox` review queue when parsers can't extract with confidence, with the existing Phase 2 segment form pre-filled by parser output for one-click confirm.

This phase is the third of sixteen per spec §12. It is shippable on its own: parsers cover the user's actual standardized senders (American, United, Air France, Fairmont, Avis, National, Amtrak, SNCF, Uber, Blacklane — ten packs), with Anthropic Haiku 4.5 as a fallback for the long tail. Explicit "Haiku territory" includes: direct-from-host vacation rentals, direct-to-property hotel confirmations, hotel-arranged shuttles, and any sender outside the v0.3.0 vendor pack set.

---

## 2. Scope

### In scope

- **Parser subsystem** under `src/trip_tracker/parsers/`:
  - JSON-LD strategy via `extruct` (vendor-agnostic, runs first).
  - **Ten vendor rule packs** (filesystem-discovered plugin architecture): Air France, American, United, Fairmont, Avis, National, Amtrak, SNCF, Uber, Blacklane.
  - Anthropic Haiku 4.5 LLM fallback (prompt-cached system prompt + tool-use schema). Catches direct-from-host vacation rentals, direct-to-property hotel bookings, hotel-arranged shuttles, and any other sender not covered by a vendor pack.
- **ARQ + Redis worker** — same Docker image as the app, separate container/command. Runs `parse_raw_email(id)` tasks enqueued by the webhook handler. Includes `parse_pending` admin command for one-shot backfill of Phase 2 RawEmails.
- **Trip clustering** — geo-distance via `airports.csv` (200km threshold) when both endpoints have coords, normalized city-name match otherwise; ±1 day adjacency window; <20% score-gap tiebreak routes to `/inbox`; auto-title `"{primary_destination} {month year}"` for new trips.
- **Inbox UI** at `/inbox` — three buckets (low-confidence parses, no-segments emails, possible duplicates) with five actions (Confirm / Edit / Re-ask Claude with hint / Split / Discard).
- **Segments form prefill path** — `/segments/new?from_raw_email=<id>` and `/segments/<id>/edit` accept parser output, render ✨ AI-suggested indicators next to AI-set fields, drop indicators on user save, write `change_reason='inbox_confirm'` on confirmation.
- **Daily LLM budget cap** — soft $1/day Haiku spend cap tracked in a new `llm_budget` table; over-budget RawEmails skip Haiku (JSON-LD + vendor rules still try) and route to `review`.
- **Tests:** ≥85% coverage; parameterized vendor-fixture regression suite; mocked Anthropic SDK in CI; one `@pytest.mark.live_llm` smoke test gated on `ANTHROPIC_API_KEY`.

### Out of scope (deferred — phase noted)

| Item | Phase |
|---|---|
| Meilisearch + post-commit search sync | 4 (was Phase 3 in master spec; explicitly deferred) |
| Nominatim hotel-address geocoding | 4 |
| Vendor parsers beyond v0.3.0's ten | 4+ (added incrementally via plugin architecture) |
| Document vault, OCR, PDF text extraction | 5 |
| Expense tracking, FX freezing | 6 |
| Public share links | 7 |
| PWA shell, web push for parser confirms | 8 |
| ICS subscribable feed, world map, weather | 9 |
| `segment_versions` audit trail (table created in Phase 2; populated here for inbox confirms only — full audit/undo UI is Phase 6) | 3 (partial) / 6 (full) |

---

## 3. Architecture

The parser subsystem sits between Phase 2's webhook ingestion and Phase 2's segment-creation routes. New infrastructure: Redis 7 + ARQ worker (same Docker image, different command). All parsing is asynchronous; the webhook continues to return 2xx as soon as the RawEmail is committed.

```
forwardemail webhook ──► /api/ingest/email
                          │
                          ▼ stores RawEmail (Phase 2, unchanged)
                          │ enqueues parse_raw_email(id)  ◄── NEW
                          │
                  ┌───────▼─────────┐
                  │  ARQ worker     │  ◄── NEW (same image, `arq` command)
                  │                 │
                  │  parsers/       │
                  │  ├─ jsonld.py   │ strategy 1 (extruct)
                  │  ├─ vendors/*   │ strategy 2 (10 packs, plugin-discovered)
                  │  └─ llm.py      │ strategy 3 (Haiku 4.5, prompt-cached)
                  └───────┬─────────┘
                          │ writes Segment(s) + Trip clustering
                          ▼
                  ┌──────────────────┐
                  │  /inbox          │  ◄── NEW UI
                  │  (3 buckets)     │
                  └──────────────────┘
```

**Touched Phase 2 surfaces:**

- `RawEmail.parse_status` enum values gain meaningful population: `'parsed'`, `'review'`, `'no_segments'`. (Column already exists.)
- `Segment.parse_source` populated as `'json-ld'` / `'rules:<vendor_name>'` / `'llm:haiku-4-5'`. (Currently always `'manual'`.)
- `Segment.parse_confidence` actually used. (Currently always `1.0`.)
- `/segments/new` form **stays** — it's the canonical structured-segment entry UI, reused by Inbox edit/confirm/split flows and by future "no email source" segment creation.

---

## 4. Plugin architecture for vendor parsers

Adding a new vendor parser six months from now must be: write one file, drop a fixture, run tests. No core code changes, no schema migration, no test-file edits.

### Directory layout

```
src/trip_tracker/parsers/vendors/
├── __init__.py              # imports each subpackage to trigger registration
├── air_france/
│   ├── __init__.py          # class AirFranceParser(VendorParser)
│   ├── README.md            # what formats it handles, when last verified
│   └── fixtures/
│       ├── confirmation_2026.eml
│       └── confirmation_2026.expected.json
├── american/...
├── united/...
├── fairmont/...
├── avis/...
├── national/...
├── amtrak/...
├── sncf/...
├── uber/...                 # ride-receipt → type='transfer'
└── blacklane/...            # private-car receipt → type='transfer'
```

### The contract (`parsers/base.py`)

```python
class SegmentDraft(BaseModel):
    """Pydantic schema mirrored to the Segment ORM shape, no DB columns."""

    type: SegmentType  # flight | lodging | car | train | transfer | activity
    status: Literal["confirmed", "tentative", "cancelled"] = "confirmed"
    confirmation_number: str | None = None
    provider: str | None = None
    start_at: datetime
    start_tz: str
    end_at: datetime | None = None
    end_tz: str | None = None
    start_location: dict[str, Any] | None = None
    end_location: dict[str, Any] | None = None
    details: dict[str, Any] = {}


class ParseResult(BaseModel):
    segments: list[SegmentDraft]  # 0 or more — empty == "no segments here"
    confidence: float  # 0..1
    source: str  # "json-ld" | "rules:air_france" | "llm:haiku-4-5"
    warnings: list[str] = []


class VendorParser(ABC):
    name: ClassVar[str]  # unique key
    sender_patterns: ClassVar[list[re.Pattern[str]]]  # From: matchers
    confidence_floor: ClassVar[float] = 0.85  # below = fall through to next strategy

    @abstractmethod
    def parse(self, msg: EmailMessage) -> ParseResult: ...

    @classmethod
    def matches(cls, from_address: str) -> bool:
        return any(p.search(from_address) for p in cls.sender_patterns)
```

### Registration & dispatch

`parsers/vendors/__init__.py` imports each subpackage; each subpackage's `__init__.py` defines a `VendorParser` subclass. Subclassing auto-registers via a metaclass-like `__init_subclass__` hook in `VendorParser`.

The dispatcher (`parsers/dispatch.py`) sorts registered parsers by **most-specific sender pattern first** (longest pattern wins on ties) so a future vendor with a narrower regex can shadow a broader one cleanly.

### Adding a vendor — the workflow this enables

1. Create `parsers/vendors/<name>/__init__.py` with a `VendorParser` subclass.
2. Drop `fixtures/<scenario>.eml` (anonymized real email) + `<scenario>.expected.json` (expected segment shape).
3. Add `README.md` documenting what email formats this pack handles.
4. `tests/test_parsers_vendors.py` is **parameterized over every `fixtures/*.eml`** — your new fixture auto-runs through the parser and asserts against `.expected.json`. No test code changes.
5. CI green → ship.

**Fixture policy:** every vendor parser must ship at least one fixture before merging. The parameterized test enforces the gate (no parser test = no parser).

---

## 5. Pipeline behavior

### Strategy order

For each `RawEmail`:

1. **JSON-LD via `extruct`** — looks for `FlightReservation`, `LodgingReservation`, `RentalCarReservation`, `EventReservation`. Confidence ~0.95 on hit. If `segments=[]` returned (no JSON-LD found), continue.
2. **Matched vendor rule pack** — registry filters parsers by `from_address` match; first matching parser runs (deduplicated post-sort by specificity). If parser returns `confidence < confidence_floor`, fall through. Confidence on a successful match: ~0.9.
3. **Anthropic Haiku 4.5** — only if both prior strategies missed AND `LlmBudget.cost_cents` for today is below `LLM_DAILY_BUDGET_CENTS`. Tool-use schema mirrors `SegmentDraft`. Self-rated confidence clamped to ≤0.85 *intentionally* — this preserves a "high-confidence overrides Haiku" signal so a future re-parse with new vendor coverage (~0.9) wins over a stored Haiku result. Without the clamp, an old Haiku parse at 0.95 would beat a fresher rules-based parse and never surface for upgrade.

### Confidence thresholds

| Confidence | parse_status | UI behavior |
|---|---|---|
| `≥ 0.7` | `'parsed'` | Segment auto-attached; not surfaced in inbox |
| `< 0.7` (any strategy) | `'review'` | Inbox bucket 1; segment is written but flagged for confirmation |
| `segments=[]` from all strategies (high confidence "nothing here") | `'no_segments'` | Inbox bucket 2 |

### Trip clustering rule

For each `SegmentDraft` produced:

- **Find candidate trips** where `(date_overlap OR adjacent ±1 day) AND (location_proximity OR shared_traveler_with_overlapping_trip)`.
- **Location proximity:**
  - If both endpoints have airport coords (typically flight↔flight): geo-distance via `airports.csv` lat/lon, threshold **200km**.
  - Otherwise: normalized city-name match (case-insensitive, whitespace-collapsed; exact match required).
- **Tiebreak:** if multiple candidates, pick the one whose date-range center is closest to `segment.start_at`.
- **Score-gap rule:** if next-best candidate's score is ≥80% of best (gap < 20%), the segment is written but its `trip_id` is left null and it surfaces in `/inbox` for manual disambiguation rather than being auto-clustered.
- **No candidates:** create a new trip with `title = "{primary_destination} {start.month_year}"` (e.g. `"Paris May 2026"`), `start_date = segment.start_at.date()`, `end_date = (segment.end_at or segment.start_at).date()`, `primary_destination` derived per the Phase 2 rule (end-side for flight/train/transfer, else start-side).

### Failure handling

| Failure | Behavior |
|---|---|
| `extruct` raises | Log + skip JSON-LD, continue to next strategy |
| Vendor parser raises | Log (with vendor `name` for triage) + continue to next strategy |
| Anthropic 429/5xx/network timeout | ARQ exponential backoff, max 5 retries over ~30 min. Final failure → `parse_status='review'` |
| Daily budget exhausted | LLM step skipped; if no other strategy produced segments → `parse_status='review'` (NOT `error_llm` — single bucket per Q5 design decision) |
| All strategies return empty | `parse_status='no_segments'` → Inbox bucket 2 |
| Possible duplicate detected (same `confirmation_number` on existing segment, or near-identical fields) | Inbox bucket 3 |

### Daily budget cap

```sql
CREATE TABLE llm_budget (
    day date PRIMARY KEY,
    cost_cents integer NOT NULL DEFAULT 0,
    request_count integer NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);
```

Each Haiku call is preceded by a `SELECT cost_cents FROM llm_budget WHERE day = current_date` check. If `cost_cents >= LLM_DAILY_BUDGET_CENTS` (default 100 = $1), the call is skipped. After each successful Haiku call, the row is upserted with the actual response usage cost (`(input_tokens × 0.025 + output_tokens × 0.125) / 100` cents per the Haiku 4.5 pricing). Auto-resets at UTC midnight by virtue of the `day` primary key.

---

## 6. Inbox UI

`/inbox` is admin-protected (any authenticated user with at least one `TripTraveler` row, OR per-user filtering scoped to the user's RawEmails — see §6.3 below). Three buckets shown as collapsible sections at the top of the page.

### 6.1 Bucket 1 — Low-confidence parses (`parse_status='review'`)

Each row shows:
- The RawEmail's `subject` + `from_address` + `received_at`.
- The draft Segment fields (provider, dates, locations) inline, with ✨ icons next to fields the parser populated.
- Five actions:
  - **Confirm** — `parse_status='parsed'`, dismiss. POST `/inbox/<raw_email_id>/confirm`.
  - **Edit** — opens `/segments/<segment_id>/edit?from_raw_email=<raw_email_id>` — the existing Phase 2 form, prefilled, with the ✨ indicators carried over. On save, indicators drop, `segment_versions` row written with `change_reason='inbox_confirm'`, `parse_status` flips to `'parsed'`.
  - **Re-ask Claude with hint** — one-line text input ("This is a return flight, not outbound"). HTMX POST to `/inbox/<raw_email_id>/reask` with the hint; Haiku is re-called with the hint appended to the prompt; response repopulates the form on-page (HTMX swap). Counts against daily budget.
  - **Split** — opens `/segments/new?split_from_raw_email=<raw_email_id>` with N stacked segment forms (an "Add another segment" button adds a form). On submit, N segments are created and linked back to the same RawEmail; original `parse_status` flips to `'parsed'`.
  - **Discard** — `parse_status='no_segments'`, segment row deleted (if any was written), dismiss.

### 6.2 Bucket 2 — No-segments emails (`parse_status='no_segments'`)

Marketing emails, receipts, non-itinerary threads. Three actions:
- **View raw** — opens `/admin/raw-emails/<id>` (existing Phase 2 page).
- **Re-parse** — re-enqueues `parse_raw_email(id)` (e.g. after a vendor rule was added that should now match).
- **Add segment manually** — opens `/segments/new?from_raw_email=<id>`, only the sender→provider guess and email date prefilled.
- **Discard** — confirms the no-segments classification (currently no-op since status already matches; future: hide from bucket).

### 6.3 Bucket 3 — Possible duplicates

Detected by: same `confirmation_number` (case-insensitive) on an existing `Segment`, OR near-identical (same `type` + same `start_at` ±2h + same start city) on the same trip. Three actions:
- **Merge** — keep existing, delete new.
- **Keep both** — dismiss the duplicate flag.
- **Discard new** — equivalent to merge but more explicit.

### 6.4 Auth scoping

Inbox shows RawEmails the current user owns. Mapping: extract the local-part of `RawEmail.to_address` via `split_part(to_address, '@', 1)`, lower-case it (since aliases are stored lowercase but headers preserve case — see Phase 2 v0.2.0 fix `233b8f3` rebased to `574b505`), join to `forwarding_aliases.local_part`, filter where `forwarding_aliases.user_id = current_user.id`. Admins (`is_admin=True`) bypass the filter and see all. This is the same join shape as the Phase 2 admin raw-emails list.

---

## 7. Worker model

ARQ worker runs in a separate container from the FastAPI app, sharing the same image. docker-compose adds:

```yaml
trip-tracker-worker:
  image: ${TRIP_TRACKER_IMAGE:-...}    # same as app
  command: ["arq", "trip_tracker.worker.WorkerSettings"]
  depends_on: [trip-tracker-redis, trip-tracker-db]
  environment: { ... same as app ... }
  networks: [internal]

trip-tracker-redis:
  image: redis:7-alpine
  restart: unless-stopped
  networks: [internal]
  # no volume — queue is ephemeral, parse_pending recovers backlog
```

`worker.py` exports a `WorkerSettings` class with:
- `functions = [parse_raw_email]`
- `max_tries = 5` (per the aggressive-retry decision)
- `redis_settings = RedisSettings.from_dsn(settings.redis_url)`
- `keep_result = 0` (results aren't read; logs carry diagnostics)

**Webhook integration:** after the `async with db.begin():` block exits (committing the RawEmail) in the existing `/api/ingest/email` handler, a single `await ctx.enqueue_job("parse_raw_email", raw_email.id)` line is added. Enqueue happens *outside* the DB transaction so a Redis-availability blip doesn't block the webhook ack — but we'd lose the parse trigger; the `parse_pending` admin command is the recovery path. Webhook still returns 2xx within ~50ms (no parser work blocks the response).

**`parse_pending` admin command:**

```bash
python -m trip_tracker parse_pending [--max-emails 1000] [--dry-run]
```

Iterates `RawEmail` rows with `parse_status='pending'`, enqueues `parse_raw_email(id)` for each (ARQ exponential-backoff prevents bursts from hammering Anthropic). One-shot, idempotent: re-running is safe (already-parsed RawEmails will short-circuit on the worker side).

---

## 8. Configuration

New environment variables:

```env
# --- Required ---
ANTHROPIC_API_KEY=sk-ant-...           # for Haiku LLM fallback
REDIS_URL=redis://trip-tracker-redis:6379/0

# --- Optional ---
LLM_DAILY_BUDGET_CENTS=100             # $1.00 USD/day soft cap
LLM_MODEL=claude-haiku-4-5-20251001    # pinned per master spec
LLM_CONFIDENCE_FLOOR=0.7               # below = review
```

`Settings` (Pydantic) gains corresponding fields, mostly with defaults so only `ANTHROPIC_API_KEY` and `REDIS_URL` are strictly required to add to `.env`.

---

## 9. Test strategy

**Five layers** (per design §4):

1. **Strategy unit tests** — `test_parsers_jsonld.py`, `test_parsers_vendors.py` (parameterized over `vendors/*/fixtures/*.eml`), `test_parsers_llm.py` (mocked).
2. **Live-LLM smoke test** — `test_parsers_llm_live.py`, `@pytest.mark.live_llm`, skipped in CI, runs once before each release with real API key.
3. **Pipeline integration** — `test_worker.py` exercises webhook → ARQ tick → DB write end-to-end via `httpx.ASGITransport` + ARQ's testing helpers (in-memory queue).
4. **Inbox UI** — `test_routes_inbox.py` covers all 3 buckets × 5 actions matrix.
5. **Trip clustering** — `test_parsers_cluster.py` table-driven across geo-hits/misses, ±1d boundaries, <20%-gap, single-match, no-match-create-new.

**Coverage:** ≥85% project-wide (same gate as Phases 1+2). Per-vendor: each parser's `parse()` ≥90% covered by its fixtures (soft norm, not enforced by separate gate).

**Fixture hygiene:**
- All `.eml` fixtures anonymized (PII redacted), with a header comment naming the scenario.
- `.expected.json` files committed alongside; CI normalizes (sorted keys, placeholder UUIDs) before comparison.
- Adding a new vendor PR checklist: parser file + `__init__.py` + `README.md` + ≥1 `.eml` + matching `.expected.json`.

---

## 10. Migration (v0.2.0 → v0.3.0)

- **Existing `RawEmail` rows with `parse_status='pending'`** — handled by manual `python -m trip_tracker parse_pending` after deploy. Idempotent. NOT auto-run at container start (avoids surprise API spend on first deploy of a backlog).
- **Existing `Segment` rows** — untouched. They have `parse_source='manual'` and `parse_confidence=1.0` from Phase 2 and will continue to render correctly.
- **`/segments/new` form** — stays. Reused by inbox edit/confirm/split actions, plus future "manual segment, no email" flows.
- **No schema breaks.** Single new table `llm_budget` only. `parse_status` enum already permits all needed values.

---

## 11. Done definition for Phase 3

- All ~19 plan tasks merged to `main` (estimated; `writing-plans` will finalize).
- CI green (lint + typecheck + test + security + docker + djlint).
- Coverage ≥85%; all 10 vendor parsers — Air France, American, United, Fairmont, Avis, National, Amtrak, SNCF, Uber, Blacklane — ship with ≥1 fixture each.
- `python -m trip_tracker parse_pending` successfully reprocesses any Phase 2 leftover RawEmails.
- One real Air France confirmation (the upcoming-travel email) round-trips: webhook → ARQ → segment auto-created → trip auto-clustered → visible at `/trips`.
- One unknown-sender email round-trips through Haiku → lands in `/inbox` bucket 1 with confidence in [0.7, 0.85] → ✨ prefilled edit form works → Confirm dismisses correctly.
- One direct-from-host vacation rental email round-trips through Haiku → produces `type='lodging'` segment with the host's name as the property name → auto-clusters or routes to `/inbox` per confidence.
- A same-day Uber receipt during an existing trip auto-attaches as a `type='transfer'` segment (capture-everything rule, no filtering).
- Daily-budget cap demonstrated: temporarily set `LLM_DAILY_BUDGET_CENTS=1`, send 5 emails, confirm 4 of them route to `review` after budget consumption.
- `v0.3.0` tag pushed; release workflow produces signed multi-arch GHCR image; release-verification scheduled agent confirms tag landed cleanly (same pattern as v0.2.0).

After this lands, return to brainstorming/writing-plans for **Phase 4 — Search & geocoding** (Meilisearch index + post-commit sync, Nominatim hotel geocoding, expanded vendor pack catalog).
