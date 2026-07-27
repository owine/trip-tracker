# Phase 9 — Smarter Inbox + Trip Consolidation Design

**Status:** Brainstormed 2026-05-02 (owine + Claude); ready for plan.
**Predecessors:** Phase 1–8 + ForwardEmail adapter + JSON-LD coverage / auto-Expense / FE-200 fix (current main `b6dd211`).
**Successor (sketched):** Phase 9.1 — public read-only trip share pages.

This document supersedes the earlier draft of the same name dated 2026-05-02.
The earlier draft was written before brainstorming and contained unvalidated
defaults (e.g., "3 days + same country" for consolidation). All scope decisions
in §2 below are locked from the brainstorm.

---

## 1. Goal

Phase 9 makes the inbox — the daily user-facing surface — trustworthy, and gives
users a way to consolidate the trips that fall out of forwarded emails into the
trips they actually took.

The work ships across **two releases**:

- **v0.8.1 — Duplicate detection (parse-time gate).** Re-forwarded confirmations
  no longer create duplicate segments + duplicate auto-Expenses. New
  `parse_status='duplicate'` value, new inbox bucket, "not a duplicate" override
  path. Self-contained; ships first (~1 week).
- **v0.9.0 — Trip consolidation + merge.** Auto-infer "home" from segment
  endpoint frequency and use it to suggest "this looks like part of an existing
  trip" at confirm-time and on the trip-detail page. Manual `merge-into` action
  with soft-delete (7-day undo window). ICS feeds for merged source trips
  return 410 Gone immediately on merge. Ships ~2 weeks after v0.8.1.

### In scope (combined across both releases)

1. Parse-time dedup with strong (conf# + provider) and medium (type + time + endpoint) matching.
2. "Not a duplicate" reparse path for false positives.
3. Auto-inferred user home from last ~20 segments' endpoint frequency, with a 30%-dominance floor.
4. Consolidation candidate suggestions on inbox-confirm preview AND trip-detail page.
5. `POST /trips/<source>/merge-into/<target>` with single-transaction reassignment.
6. Soft-delete via `trips.merged_into_id` + `trips.merged_at`; periodic hard-delete after 7 days.
7. Per-pair dismissal so suggestions stop nagging.

### Explicitly NOT in scope (deferred)

- Public read-only trip share pages → Phase 9.1 sketch.
- Vendor-pack drift detector ("Haiku keeps beating this vendor pack" worker) → parked.
- Hint-aware reparse worker actually consuming `X-Tt-Hint` → Phase 9.2 chore.
- Splitting one trip into two → rare; defer until requested.
- User-overridable `home_city` setting → if auto-inference is wrong we'll add it; not pre-built.
- Fuzzy provider normalization for dedup (Levenshtein, etc.) → start with `lower().strip()`, tune from real data.

---

## 2. Scope decisions (locked during brainstorm 2026-05-02)

| # | Decision | Choice |
|---|---|---|
| 1 | Dedup layer | Parse-time gate (worker pre-persistence). Not confirm-time, not hybrid soft-flag. |
| 2 | Strong-match key | `(provider_normalized, confirmation_number)`, both non-null. `lower().strip()` only — no fuzzy. |
| 3 | Medium-match key | `(type, start_at±30min, start_iata, end_iata)` for flights/trains/transfers; `(type='lodging', date(start_at), hotel_name CI)` for lodging. |
| 4 | Mixed-drafts behavior | Partial dedup → persist fresh + `parse_status='review'` + record `X-Tt-Dedup-Partial`. NOT routed to duplicate bucket. |
| 5 | Consolidation rule | Home-anchored (auto-inferred) primary; geometric (date gap + endpoint city/distance) fallback. Country dropped. |
| 6 | Home inference | Top endpoint city across last ~20 confirmed segments, IF its share ≥ 30% of all endpoints; else None. Recomputed per query. |
| 7 | Home persistence | NOT persisted on `users`. No new column. |
| 8 | Geometric fallback gap | 3 days. Tunable via `Settings`. |
| 9 | Distance threshold for LOW match | 500km great-circle. Tunable via `Settings`. |
| 10 | Suggestion cap | Top 3 candidates, sorted HIGH→MEDIUM→LOW then most-recent. |
| 11 | Merge irreversibility | Soft delete with 7-day undo window, then periodic hard-delete. NOT hard-delete-on-action. |
| 12 | Merge-target URL behavior | Source trip GET returns 410 Gone (not 301 redirect). |
| 13 | Source ICS feed post-merge | 410 Gone immediately, no grace period, no redirect. |
| 14 | Undo chain rule | Refused (409) if target has been merged in the meantime; user must unwind from the top. |
| 15 | Hard-delete sweeper | Daily saq cron at 04:00 UTC; deletes trips where `merged_at < now() - 7 days`. |
| 16 | Filter clause everywhere | Explicit `WHERE merged_into_id IS NULL` on every Trip select — no SQLAlchemy event-listener magic. |
| 17 | Phase split | v0.8.1 = Track A (dedup) alone; v0.9.0 = Tracks B+C (consolidation+merge). Not a single combined v0.9.0. |

---

## 3. Architecture

### 3.1 Dedup architecture (v0.8.1)

New module `src/trip_tracker/parsers/dedup.py` exposing one pure function:

```python
async def find_existing_segment(
    db: AsyncSession,
    owner_user_id: uuid.UUID,
    draft: SegmentDraft,
) -> Segment | None:
```

Called from the parse worker between strategy execution and `persist_segments()`.
No web-layer dependency, fully unit-testable.

**Match rules (in order; first hit wins):**

1. **Strong match.** `confirmation_number = draft.confirmation_number` AND
   `lower(strip(provider)) = lower(strip(draft.provider))`, both non-null.
   Catches re-forwarded Trainline / Air France / Amtrak confirmations exactly.
   Zero false positives in our forward dataset.
2. **Medium match (flights/trains/transfers).** `type = draft.type` AND
   `start_at BETWEEN draft.start_at - 30min AND draft.start_at + 30min` AND
   `start_location->>'iata' = draft.start_location.iata` AND
   `end_location->>'iata' = draft.end_location.iata`. IATA pair + ±30min window.
3. **Medium match (lodging).** `type = 'lodging'` AND
   `date(start_at) = date(draft.start_at)` AND
   `lower(start_location->>'name') = lower(draft.start_location.name)`.
4. **No match below medium.** Fuzzy provider matching deliberately excluded.

Match candidates are scoped to the owner_user_id (no cross-user matching) and
exclude cancelled segments.

**Worker hook (single dedup pass for all drafts):**

```python
drafts = await run_strategies(raw)
matched: list[tuple[SegmentDraft, Segment]] = []
fresh: list[SegmentDraft] = []
for d in drafts:
    existing = await find_existing_segment(db, owner_user_id, d)
    if existing:
        matched.append((d, existing))
    else:
        fresh.append(d)

if drafts and not fresh:  # all drafts deduped
    raw.parse_status = "duplicate"
    raw.headers = {**(raw.headers or {}), "X-Tt-Dedup-Against": [str(s.id) for _, s in matched]}
    # NO segments persisted, NO auto-Expense
elif matched:  # mixed — partial dedup
    raw.parse_status = "review"
    raw.headers = {
        **(raw.headers or {}),
        "X-Tt-Dedup-Partial": [{"draft": d.summary(), "existing": str(s.id)} for d, s in matched],
    }
    persist_segments(fresh)
else:
    raw.parse_status = "review"  # current behavior
    persist_segments(fresh)
```

Note: `raw.headers` is rebound to a new dict (rather than mutated in place)
because SQLAlchemy's JSONB change-tracking does not detect in-place mutation
without `flag_modified()`. Whole-dict reassignment is the simpler convention
already used elsewhere in the codebase (see `routes/inbox.py::reask`).

The mixed-drafts path matters: a multi-segment forward where one leg is already
known and one is new should preserve the new leg, not reject the whole email.

**Inbox UX.**
- New `duplicate_rows` bucket on `/inbox`, ordered by `received_at` desc, limit 50.
- Each row links to the existing segment(s) it matched against.
- Two actions: **Discard** (existing path, sets `no_segments`) and **Not a duplicate** (new: sets `pending`, clears the dedup header, enqueues fresh parse — costs one LLM call).
- Confirm action is hidden on duplicate rows since there's nothing to confirm.

**Auto-Expense interaction.** No code change needed. The `/inbox/<id>/confirm`
handler iterates `Segment.where(raw_email_id=raw.id)`; dedup ensures no segments
were created.

### 3.2 Home inference + consolidation (v0.9.0)

New module `src/trip_tracker/trips/home.py`:

```python
async def infer_home(db: AsyncSession, user_id: uuid.UUID) -> str | None:
```

Aggregates `start_location->>'city'` and `end_location->>'city'` across the
user's last ~20 confirmed segments (ordered by `start_at desc`). Returns the
top city IF its share ≥ 30% of all endpoints; else `None`.

The 30% floor handles digital-nomad cases — if no city dominates, geometric
fallback takes over rather than picking a wrong "home." Recomputed on every
consolidation query. No persisted cache. Within a single
`consolidation_candidates` call, the `home` value is computed once locally and
reused — there is no cross-request cache (correctness > caching; the query is
already cheap given the partial index in §4.2).

New module `src/trip_tracker/trips/consolidation.py`:

```python
@dataclass(frozen=True)
class ConsolidationTarget:
    """Normalized view of either an existing Trip or in-flight drafts.

    Both surfaces (trip-detail page + inbox-confirm preview) need the same
    shape: a date range and the set of endpoint cities/IATAs. This adapter
    lets `consolidation_candidates` stay agnostic to which surface is calling.
    """
    start_date: date
    end_date:   date
    start_city: str | None
    end_city:   str | None
    endpoint_iatas: frozenset[str]
    trip_id: uuid.UUID | None  # None for in-flight drafts (no Trip row yet)

    @classmethod
    def from_trip(cls, trip: Trip, segments: Sequence[Segment]) -> "ConsolidationTarget": ...

    @classmethod
    def from_drafts(cls, drafts: Sequence[SegmentDraft]) -> "ConsolidationTarget": ...


async def consolidation_candidates(
    db: AsyncSession,
    user: User,
    target: ConsolidationTarget,
) -> list[ConsolidationCandidate]:
```

Both call-sites (`routes/trips.py::trip_detail` and the inbox-confirm preview)
build the `ConsolidationTarget` themselves and pass it in. The function never
sees raw drafts or Trip rows directly.

```
home = infer_home(user)
candidates: list[ConsolidationCandidate] = []

for trip in user_trips_within_window(user, target_dates, gap_days=3):
    if trip.id in dismissed_pairs(user, target):
        continue

    # Primary signal (home-anchored), only if home is known:
    if home is not None:
        if trip_is_open(trip, home):              # no segment ends at home after the last endpoint
            if target.start_city == trip.last_endpoint_city:
                candidates.append((trip, weight=HIGH))
                continue
            if target.end_city == home and trip.has_outbound_from_home:
                candidates.append((trip, weight=HIGH))   # closing leg
                continue

    # Fallback signal (geometric):
    if shared_endpoint_city(trip, target):
        candidates.append((trip, weight=MEDIUM))
    elif min_endpoint_distance_km(trip, target) <= 500:
        candidates.append((trip, weight=LOW))

return sorted(candidates, key=weight_desc)[:3]
```

The 3-day gap is the geometric-fallback constraint only. Home-anchored matching
is gap-agnostic (the bookend rule is "leaves home → returns home").

Two surfaces use the same query:

- **Inbox-confirm preview** (existing GET handler): if candidates non-empty,
  render a banner above the segment list with "Add to *Trip Title*" /
  "Create new trip" buttons. The "Add to" button POSTs to the existing
  confirm route with `?target_trip=<id>`.
- **Trip-detail page**: same query inverted — "other trips of yours that look
  adjacent to this one." Renders as a dismissible suggestion strip near the
  trip header with a "Merge them →" button.

Per-pair dismissal stored in `trip_merge_dismissals` (§4).

### 3.3 Merge with soft-delete (v0.9.0)

`POST /trips/<source_id>/merge-into/<target_id>` in `routes/trips.py`.

**Validation:**
- `require_user`; `source.created_by == user.id` AND `target.created_by == user.id`, else **403**.
- Both rows must exist, else **404**. (Order: existence check before ownership check, so non-existent IDs don't leak ownership info.)
- `source_id != target_id`, else **400**.
- If *either* trip is already merged (`merged_into_id IS NOT NULL`), return **400**.

**Single-transaction reassignment:**

```sql
UPDATE segments  SET trip_id = :target WHERE trip_id = :source;
UPDATE expenses  SET trip_id = :target WHERE trip_id = :source;
UPDATE documents SET trip_id = :target WHERE trip_id = :source;

INSERT INTO trip_travelers (trip_id, user_id, role, ...)
  SELECT :target, user_id, role, ...
    FROM trip_travelers
   WHERE trip_id = :source
ON CONFLICT (trip_id, user_id) DO NOTHING;
DELETE FROM trip_travelers WHERE trip_id = :source;

UPDATE trips SET
  start_date = LEAST(start_date, :source_start_date),
  end_date   = GREATEST(end_date, :source_end_date),
  updated_at = now()
WHERE id = :target;

UPDATE trips SET
  merged_into_id = :target,
  merged_at      = now()
WHERE id = :source;
```

`primary_destination` is NOT touched; the user already chose the target's
destination and merging shouldn't relabel it.

**Filter clause.** Every `select(Trip)` in the codebase gains
`WHERE merged_into_id IS NULL`. Done explicitly (no SA event-listener magic) so
it's grep-able. The user-facing `/trips/<id>` GET returns **410 Gone** if
`merged_into_id IS NOT NULL`, with a body naming the target trip.

**ICS feed.** `/ics/<trip_id>.ics` returns **410 Gone** with empty body
immediately if the trip is soft-deleted. No redirect, no grace period.
Subscribers fix their subscription. This is the explicit contract.

**Merge audit (for lossless undo of trip_travelers).** The merge transaction
inserts trip_traveler rows into the target via `ON CONFLICT DO NOTHING`, then
deletes them from the source. By the time undo runs, we cannot tell which
target rows were *added by this merge* versus which pre-existed — undo would
either leave them attached (lossy: collaborators of the source remain on
target) or remove all matching rows (lossy in the other direction: removes
collaborators who were independently on the target).

Resolution: store the merge audit on the soft-deleted source row itself.
Add `trips.merge_audit JSONB NULL` (column added in §4.2). At merge time,
populate it with the rows actually moved/added:

```python
merge_audit = {
    "source_segment_ids": [...],
    "source_expense_ids": [...],
    "source_document_ids": [...],
    "added_traveler_user_ids": [...],  # rows actually inserted into target
    "source_start_date": "...",  # for target date recomputation on undo
    "source_end_date": "...",
    "schema_version": 1,
}
```

The `added_traveler_user_ids` list captures the diff: for each source
trip_traveler row, include `user_id` only if it was NOT already present on
target (i.e., it was actually inserted, not skipped via `ON CONFLICT`).

When the source is hard-deleted by the sweeper after 7 days, the audit goes
with it — no garbage collection needed.

**Undo route.** `POST /trips/<target_id>/undo-merge/<source_id>`:
- Auth: target owned by user.
- Window: `now() - source.merged_at <= 7 days`, else 410.
- Refused (409) if the *target* itself has been merged since.
- Reverses FK reassignment using `source_segment_ids`, `source_expense_ids`,
  `source_document_ids` from `merge_audit` (idempotent: `WHERE id = ANY(...)`).
- Removes target trip_traveler rows where `user_id IN added_traveler_user_ids`.
- Re-inserts the source's original trip_traveler rows.
- Nulls `merged_into_id`, `merged_at`, `merge_audit` on source.
- Recomputes target's `start_date`/`end_date` excluding the source span.

**Hard-delete sweeper.** New saq cron task
`trip_tracker.workers.cleanup.purge_merged_trips` running daily at 04:00 UTC:

```python
async def purge_merged_trips(ctx):
    cutoff = datetime.now(UTC) - timedelta(days=7)
    result = await db.execute(
        delete(Trip).where(
            Trip.merged_into_id.isnot(None),
            Trip.merged_at < cutoff,
        )
    )
    log.info("purged_merged_trips", count=result.rowcount)
```

The hard-delete cascades through `segments`, `expenses`, `documents`,
`trip_travelers`, `trip_merge_dismissals` (all CASCADE). At sweep time the real
data has already been reassigned; only the empty shell is removed.

**UI.** "Merge into…" dropdown on the trip-detail page header. Lists user's
other (active) trips, sorted by date proximity. Selecting a target opens a
5-second confirm dialog showing segment/expense/document counts about to move.
Post-merge: redirect to target with a flash banner including an "Undo" button
visible during the 7-day window.

---

## 4. Data model changes

### 4.1 v0.8.1 migration

Single Alembic revision. No new tables, no new columns. The
`X-Tt-Dedup-Against` and `X-Tt-Dedup-Partial` payloads live in the existing
`raw_emails.headers` JSONB.

Schema change required: `raw_emails.parse_status` carries a CHECK constraint
`ck_raw_emails_parse_status` (defined in initial Phase 2 migration
`bbf3bbe09be9_phase2_ingestion`) that allows
`'pending', 'parsed', 'failed', 'no_segments', 'review'`. The migration
drops and recreates this constraint with `'duplicate'` appended. The
SQLAlchemy `CheckConstraint` declared on `RawEmail` must be updated in the
same commit to keep ORM and DB in sync.

Note on naming-convention drift: the ORM declares `CheckConstraint(...,
name="ck_raw_emails_parse_status")` (the full literal name including the
`ck_raw_emails_` prefix). Combined with `MetaData.naming_convention`'s
`ck = "ck_%(table_name)s_%(constraint_name)s"`, calls to
`Base.metadata.create_all` would emit
`ck_raw_emails_ck_raw_emails_parse_status` — a different name from what's
in the DB. This pre-existing drift does NOT bite in current tests
(no `create_all` path is exercised against a real Postgres in the suite).
The migration uses raw `op.execute(sa.text("ALTER TABLE ... DROP/ADD
CONSTRAINT ..."))` rather than `op.drop_constraint(...)` to bypass the
convention prefix and target the actual DB-side name. A follow-up debt
item is tracked to rename the ORM `name=` argument to `"parse_status"`
so the convention prefixes correctly.

### 4.2 v0.9.0 migration

Single Alembic revision combining all of these:

```sql
-- 1. Trip soft-delete columns + merge audit (for lossless undo, see §3.3)
ALTER TABLE trips
  ADD COLUMN merged_into_id uuid        NULL REFERENCES trips(id) ON DELETE SET NULL,
  ADD COLUMN merged_at      timestamptz NULL,
  ADD COLUMN merge_audit    jsonb       NULL;
-- Self-FK uses SET NULL (not CASCADE): if the target of a merge is later
-- hard-deleted, the source row should survive with a null pointer rather
-- than vanish in a cascade.
-- merge_audit is populated only on soft-deleted source rows; null elsewhere.
-- Schema documented in §3.3 ("Merge audit"); schema_version=1 initially.

-- 2. Index for the consolidation candidate query
-- Also serves as the only index needed for the WHERE merged_into_id IS NULL
-- filter clause everywhere — no separate ix_trips_active. Postgres uses this
-- partial index for both the candidate range query and active-only listings.
CREATE INDEX ix_trips_owner_dates ON trips (created_by, start_date, end_date)
  WHERE merged_into_id IS NULL;

-- 3. Per-pair dismissal table
CREATE TABLE trip_merge_dismissals (
  user_id      uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  trip_a_id    uuid        NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
  trip_b_id    uuid        NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
  dismissed_at timestamptz NOT NULL DEFAULT now()
);

-- Pair-uniqueness via expression UNIQUE INDEX (Postgres allows it here
-- even though it can't be a PK expression). Migration MUST include a comment
-- explaining the LEAST/GREATEST canonicalization for future contributors.
CREATE UNIQUE INDEX uq_trip_merge_dismissals_pair
  ON trip_merge_dismissals (
    user_id,
    LEAST(trip_a_id, trip_b_id),
    GREATEST(trip_a_id, trip_b_id)
  );
```

### 4.3 No changes to

- `segments` (`superseded_by` exists but is for amended bookings, not dedup).
- `expenses`, `documents`, `users`, `raw_emails` (beyond the parse_status value).

### 4.4 Backfill

Both migrations are pure DDL. `merged_into_id` defaults to NULL for all
existing rows, which is correct.

---

## 5. Routes

### 5.1 v0.8.1

| Method | Path | Notes |
|---|---|---|
| `GET` | `/inbox` | Existing handler; populates `duplicate_rows` from `parse_status='duplicate'` (auth-scoped, ordered desc, limit 50). |
| `POST` | `/inbox/<raw_id>/not-a-duplicate` | New. Sets `parse_status='pending'`, removes `X-Tt-Dedup-Against`, enqueues parse. CSRF + ownership check. **404 on non-owner** to match existing inbox convention (`_load_owned` raises 404, deliberately not leaking owned-but-different-user vs nonexistent). The trips routes (§5.2) use 403 — the inconsistency is a deliberate codebase convention split: inbox routes don't leak ownership, trips routes do. |

### 5.2 v0.9.0

| Method | Path | Notes |
|---|---|---|
| `GET` | `/trips/<id>` | Modified. Renders consolidation banner if candidates exist AND not dismissed. Returns **410 Gone** if soft-deleted; body names target. |
| `POST` | `/trips/<source>/merge-into/<target>` | New. Single-transaction reassignment. Redirects to target with flash. |
| `POST` | `/trips/<target>/undo-merge/<source>` | New. 7-day window; 409 if target re-merged. |
| `POST` | `/trips/<id>/dismiss-merge/<other_id>` | New. Inserts into `trip_merge_dismissals` (idempotent via UNIQUE INDEX). |
| `GET` | `/inbox/<id>` (preview) | Modified. Renders consolidation banner above segment list when candidates exist. |
| `POST` | `/inbox/<id>/confirm` | Modified. Accepts optional `?target_trip=<uuid>`. When set + valid + owned, segments inherit that trip_id; no new Trip created. |
| `GET` | `/ics/<trip_id>.ics` | Modified. Returns **410 Gone** with empty body if trip soft-deleted. No redirect. |

### 5.3 Templates

- `templates/inbox/list.html` — wire up existing `duplicate_rows` slot; per-row "view existing" link + actions.
- `templates/trips/detail.html` — consolidation suggestion strip (dismissible); "Merge into…" dropdown; 5-second confirm dialog.
- `templates/segments/_inbox_preview.html` (or wherever the confirm preview lives) — consolidation banner above segment list.
- New partial `templates/trips/_merge_undo_flash.html` — post-merge banner with countdown.

The 5-second confirm dialog is plain `<dialog>` + `setTimeout`-enabled submit
button; no new HTMX endpoints.

### 5.4 Codebase grep audit (Trip selects)

Every `select(Trip)` site needs `WHERE merged_into_id IS NULL`. The
implementation plan task list itemizes each. Initial inventory:

- `routes/trips.py` (list, detail, edit forms)
- `routes/segments.py` (trip dropdown)
- `routes/map.py`
- `routes/expenses.py` (trip selectors)
- `routes/documents.py`
- `routes/ics.py`
- `routes/inbox.py` (target_trip lookup; consolidation candidate queries)
- `search/sync.py` (Meilisearch index population)
- Any worker/job that touches Trip

Each one gets a one-line addition + a regression test confirming soft-deleted
trips don't appear.

---

## 6. Test plan

### 6.1 v0.8.1 tests (~19 new)

`tests/test_parsers_dedup.py` (~12 tests): strong match on conf# + provider;
strong match guards (different provider, null conf#); medium flight (within
30min vs 31min vs different IATA); medium train; medium lodging
(case-insensitive name match); cross-type guard (lodging conf# != flight
conf#); owner scoping; cancelled segments excluded.

`tests/test_workers_parse.py` additions (~4 tests): all-drafts-deduped path;
mixed drafts (fresh persisted, status=review); all-fresh regression; re-forward
integration (same Trainline fixture twice → 1 segment, 1 expense, second
RawEmail in duplicate bucket).

`tests/test_routes_inbox.py` additions (~3 tests): /inbox surfaces duplicate
rows; not-a-duplicate POST behavior; ownership 404.

### 6.2 v0.9.0 tests (~38 new)

`tests/test_trips_home.py` (~6 tests): top endpoint at ≥30% returns city;
below 30% returns None; last-20 window respected; cancelled excluded; empty
history None; both start and end endpoints contribute.

`tests/test_trips_consolidation.py` (~10 tests): home-anchored HIGH on
last-endpoint match; closing-leg HIGH detection; geometric fallback when home
unset (MEDIUM on shared endpoint, LOW on ≤500km); gap > 3 days excludes
geometric (home-anchored unaffected); multi-country chain via home-anchored;
top-3 cap; dismissed pairs excluded; soft-deleted trips excluded; sort order.

`tests/test_routes_trips_merge.py` (~10 tests): happy-path reassignment +
soft-delete + date recomputation; 403 on non-owner (source); 403 on non-owner
(target); 400 on self-merge; 400 if source already merged; 400 if target
already merged; 404 on nonexistent ids; source `/trips/<id>` returns 410;
source ICS returns 410; trip listings exclude soft-deleted.

`tests/test_routes_trips_undo_merge.py` (~7 tests): within-window restore;
after-window 410; 409 on chain (target re-merged); source 410 lifted post-undo;
ICS live again post-undo; **trip_travelers undo restores source-only rows
without removing target's pre-existing collaborators** (audit-driven);
**target trip_travelers added by merge are removed on undo** (audit-driven).

`tests/test_workers_cleanup.py` (~3 tests): sweeper deletes past-window;
preserves in-window; cascades clean shell on hard-delete.

`tests/test_routes_inbox_confirm_target_trip.py` (~4 tests): valid + owned
inherits trip_id; 403 on non-owner; 400 if target soft-deleted; omitted →
existing new-Trip behavior.

### 6.3 Targets

- v0.8.1: ~19 new tests, repo total ~526.
- v0.9.0: ~40 new tests, repo total ~566.
- Coverage ≥88% maintained both releases.

### 6.4 Manual end-to-end smoke

**v0.8.1.** Forward a Trainline confirmation → confirm → auto-Expense fires.
Forward the same email again → verify duplicate bucket entry references the
existing segment; verify no new Expense.

**v0.9.0.** Forward outbound JFK→CDG → confirm → Trip 1 (Paris). Forward CDG
hotel for the same week → confirm preview shows banner → click "Add to Trip 1"
→ verify single trip with two segments. Manually create a second adjacent trip
→ merge it in → verify undo flash → click undo within window.

---

## 7. Risks

| # | Risk | Mitigation |
|---|---|---|
| 1 | Strong-match provider normalization too narrow ("Air France" vs "Air France KLM") | Start with `lower().strip()`. Track real misses in worker logs (`dedup_miss` event with both provider strings). Revisit if signal appears. |
| 2 | Medium-match ±30min window too tight for delays/reschedules | Tunable via `Settings.dedup_time_window_minutes`, default 30. Adjust after first month of real data. |
| 3 | Home inference picks wrong city for users with two homes | 30% dominance floor reduces wrong-pick risk; geometric fallback when no city dominates. If reports come in, add `users.home_city` override (out of scope until requested). |
| 4 | Filter clause `WHERE merged_into_id IS NULL` missed in some Trip query | Codebase grep audit during implementation; the plan task list itemizes every Trip select. Partial index `ix_trips_owner_dates` (which has the same `WHERE merged_into_id IS NULL` predicate) makes the active-only path the cheap one. |
| 5 | Undo race: two clients open trip-detail, one merges, other clicks undo | Idempotent — undo checks `merged_into_id IS NOT NULL`; second click is a no-op. |
| 6 | ICS subscribers churn on every merge (410 Gone immediately) | Accepted per scope decision #13. Document the contract in user-facing docs alongside the merge UI. |
| 7 | Hard-delete sweeper deletes a trip mid-undo-click | Race window is microseconds; user gets 410 + "this trip has been permanently deleted" message. Acceptable; advisory locks would over-engineer a near-zero scenario. |
| 8 | Auto-Expense fires before dedup gate due to ordering bug | Mitigated by integration test §6.1 (re-forward → 1 expense). Caught at PR review. |

---

## 8. Memory references

- `feedback_forwardemail-requires-200.md` — webhook contract. Phase 9 adds no new webhooks; FE adapter unchanged.
- `feedback_remote-agent-shared-infra.md` — any CCR-delegated subtask MUST forbid edits to `tests/conftest.py`, `.pre-commit-config.yaml`, `Dockerfile*`, `.github/workflows/*`, and `pyproject.toml` (deps go through dependency-currency check first).
- `project_duplicate-detection-gap.md` — Phase 9 closes this gap directly. Update post-v0.8.1.
- `feedback_af-text-plain-needs-llm.md` — informs vendor-pack drift detector (parked), not Phase 9 scope.

---

## 9. Release sequencing

**v0.8.1 (Track A only):**
1. Implement dedup module + worker hook + inbox bucket + "not a duplicate" route.
2. Tests, manual smoke, tag.
3. Update `project_duplicate-detection-gap.md` → shipped.
4. Schedule release-verification routine 7 days post-merge.

**v0.9.0 (Tracks B + C):**
1. Implement home inference + consolidation candidates.
2. Implement merge route + undo route + dismiss route + sweeper.
3. UI: banners, dropdown, confirm dialog, undo flash.
4. Codebase grep audit for every `select(Trip)`.
5. Tests, manual smoke, tag.
6. Create `project_phase9-status.md` with HEAD + tag.
7. Schedule release-verification routine 7 days post-merge.

The v0.9.0 milestone tag is the one that closes Phase 9. v0.8.1 is a point
release inside Phase 9, not its own phase boundary.
