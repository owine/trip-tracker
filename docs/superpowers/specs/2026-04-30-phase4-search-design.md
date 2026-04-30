# Phase 4 — Search Design

**Status:** Design (pre-implementation). Approved 2026-04-30.

**Builds on:** Phase 3 (Parsers v0). Latest commit on main at time of writing: `8e6cd4d`. v0.3.0 tag corresponds to Phase 3 §11 complete.

**Parent spec:** [`2026-04-26-trip-tracker-design.md`](./2026-04-26-trip-tracker-design.md). All section references in this document are to the parent spec unless prefixed.

**Prior phase specs:**
- [`2026-04-27-phase2-ingestion-v0-design.md`](./2026-04-27-phase2-ingestion-v0-design.md)
- [`2026-04-29-phase3-parsers-design.md`](./2026-04-29-phase3-parsers-design.md)

**Coupled migration plan:** [`2026-04-30-arq-to-saq-migration.md`](../plans/2026-04-30-arq-to-saq-migration.md) — executed as Task 1 of this phase.

---

## 1. Goal

Add typo-tolerant, sub-50ms full-text search across trips and segments via a Meilisearch derived index, surfaced through a ⌘K command palette. After v0.4.0:

- Pressing ⌘K (or Ctrl+K) anywhere in the app opens a search modal.
- Typing matches across trip titles, primary destinations, segment providers, confirmation numbers, cities, flight/train numbers, and segment notes.
- Results are grouped by entity type (Trips, Segments) with keyboard navigation. Selecting a result deep-links to the relevant view.

This phase also resolves a pre-existing technical-debt item: **migrating from `arq` (maintenance-only mode per upstream issue #510) to `saq` (active fork with modern redis-py support)**. The migration is bundled as Task 1 of Phase 4 because the new `sync_meili` background task will be authored against saq's API rather than retrofitted onto arq.

This phase is the fourth of sixteen per spec §12. It is shippable on its own: existing functionality continues to work; ⌘K is purely additive.

---

## 2. Scope

### In scope

- **arq → saq migration** (Task 1) — execute the prior plan at `docs/superpowers/plans/2026-04-30-arq-to-saq-migration.md`. Replaces ARQ with saq. Bumps `redis` from `>=5,<6` to `>=7,<8` (now reachable). Replaces `ARQ` references in the README. Existing `parse_raw_email` task body is unchanged.
- **Meilisearch container** — `trip-tracker-search` service in docker-compose, internal-network only, master key via env. Two indexes: `trips` and `segments`.
- **Sync subsystem** — `enqueue_meili_sync(entity, id)` helper called explicitly after every Trip/Segment write commit. saq task `sync_meili` reads the entity from Postgres and upserts to Meili.
- **`/api/search/<index>` proxy route** — FastAPI handler that authorizes via the existing Authelia session cookie, applies a server-side `traveler_ids = <user.id>` filter, and forwards the query to Meili using the master key. Browser never sees the master key.
- **⌘K palette UI** — Alpine.js component embedded in `base.html`, keyboard shortcut, search-as-you-type from char 1, grouped results, keyboard navigation, deep-link on select.
- **`#segment-<id>` anchors** on segment rows so search results can deep-link into a specific segment within a trip detail page.
- **`reindex` CLI command** — `python -m trip_tracker reindex`. Walks Postgres, batch-upserts everything. Idempotent.
- **Tests:** ≥85% coverage. Mock Meili in unit tests; one integration test exercises a real Meili container.

### Out of scope (deferred — phase noted)

| Item | Phase |
|---|---|
| Nominatim hotel-address geocoding | 4.1 / 5 |
| Nightly drift-detection cron (count recon Postgres ↔ Meili) | 4.1+ |
| Per-index reindex (`--index trips` / `--index segments`) | 5+ |
| Cross-traveler search (`traveler_ids IN [me, ...co_travelers]` instead of just `me`) | 5+ (multi-user) |
| Documents index (3rd Meili index) | 5 (vault + OCR phase) |
| Browser-direct Meili access (per master spec §8.5) | Permanently deviated — proxied through `/api/search` to keep Traefik routing simple |
| New vendor parser packs beyond Phase 3's 11 | Added incrementally as user encounters new senders; not bundled in v0.4.0 |
| Saq's web UI for queue inspection | Optional, not enabled by default |

---

## 3. Architecture

```
   Browser
     │ Ctrl+K / ⌘K
     ▼
  ⌘K palette modal (Alpine.js)
     │ POST /api/search/segments  { q: "Paris" }   (Authelia cookie)
     ▼
   FastAPI app
     │ session → user.id
     │ apply filter: traveler_ids = <user.id>
     ▼
  Meilisearch (internal network only)
     │
     ▼
   results JSON ──► palette renders grouped list
                    select → /trips/<id> or /trips/<tid>#segment-<sid>


   Write paths (existing):
     POST /segments        ┐
     POST /trips           ├──► db.commit() ──► await enqueue_meili_sync(entity, id)
     POST /trips/.../edit  │                          │
     POST /inbox/.../discard                          ▼
     parse_raw_email worker                       saq queue (Redis)
                                                       │
                                                       ▼
                                                  saq worker process
                                                       │ load entity from Postgres
                                                       │ index payload
                                                       ▼
                                                  Meilisearch upsert
```

**Touched Phase 3 surfaces:**
- `worker.py` — replaced wholesale by saq settings dict + `parse_raw_email` (unchanged body) + new `sync_meili` task.
- `ingest/webhook.py` — `enqueue_parse` rewritten against saq's `Queue.from_url` + `q.enqueue(name, **kwargs, retries=N)`.
- `__main__.py` — `parse_pending` rewritten against saq enqueue API; new `reindex` subcommand added.
- Every route that writes Trip/Segment rows — adds one `await enqueue_meili_sync(...)` line after commit.
- `templates/base.html` — adds the ⌘K palette include + Alpine.js script tag.
- `templates/segments/_row.html` — adds `id="segment-{{ s.id }}"` for deep-link targets.

---

## 4. Index design

### 4.1 `trips` index

| Field | Type | Searchable | Filterable | Sortable |
|---|---|---|---|---|
| `id` | string (UUID) | – | – | – |
| `title` | string | ✓ | – | – |
| `primary_destination` | string \| null | ✓ | – | – |
| `start_date` | int (UTC days from epoch) | – | ✓ | ✓ |
| `end_date` | int (UTC days from epoch) | – | ✓ | – |
| `traveler_ids` | array<string (UUID)> | – | ✓ | – |

Searchable attributes (Meili weight order): `title`, `primary_destination`.

### 4.2 `segments` index

| Field | Type | Searchable | Filterable | Sortable |
|---|---|---|---|---|
| `id` | string (UUID) | – | – | – |
| `trip_id` | string (UUID) | – | ✓ | – |
| `traveler_ids` | array<string (UUID)> | – | ✓ | – |
| `type` | string | – | ✓ | – |
| `provider` | string \| null | ✓ | – | – |
| `confirmation_number` | string \| null | ✓ | – | – |
| `start_at_unix` | int (UTC seconds) | – | ✓ | ✓ |
| `start_city` | string \| null | ✓ | – | – |
| `end_city` | string \| null | ✓ | – | – |
| `vehicle_number` | string \| null | ✓ | – | – |
| `notes` | string \| null | ✓ | – | – |

Searchable attributes (Meili weight order): `provider`, `confirmation_number`, `vehicle_number`, `start_city`, `end_city`, `notes`.

`vehicle_number` flattens `details->>'flight_number' or 'train_number'` from the Phase 2 JSONB payload. `notes` flattens `details->>'notes'`. Other JSONB fields (`seat`, `room_type`, etc.) are too type-specific to flatten and are not indexed.

`traveler_ids` is denormalized from each segment's parent trip's `trip_travelers` rows at index time. Re-index on TripTraveler mutation is NOT required for v0.4.0 (single-user — `traveler_ids` is always `[user.id]`); revisit when cross-traveler features land.

### 4.3 Index settings

Both indexes use Meili defaults except:
- `pagination.maxTotalHits = 200` — keep result sets bounded.
- `typoTolerance.enabled = true` (default).
- No synonyms in v0.4.0.
- `filterableAttributes` and `sortableAttributes` configured per the tables above on first creation.

---

## 5. Sync model

### 5.1 Trigger

After every successful Postgres `db.commit()` that writes a Trip or Segment row, the route or worker explicitly calls:

```python
await enqueue_meili_sync(settings, entity="segment", entity_id=seg.id)
```

`entity` ∈ `{"trip", "segment"}`. `entity_id` is the row's UUID.

The 8 write sites (catalogued at v0.4.0 design time):

| Site | Entity |
|---|---|
| `routes/trips.py::create_trip` (existing form action) | trip |
| `routes/trips.py::edit_trip` | trip |
| `routes/trips.py::delete_trip` | trip + cascade-delete its segments |
| `routes/segments.py::create_segment` | segment + (sometimes) trip |
| `routes/segments.py::update_segment` | segment + (if widened) trip |
| `routes/segments.py::delete_segment` | segment |
| `routes/inbox.py::discard` | segment (delete: enqueue with `op="delete"`) |
| `worker.py::parse_raw_email` | segment + (if create_new) trip |

A missed call site results in stale search results until the next reindex; the recovery path is `python -m trip_tracker reindex`.

### 5.2 Coalescing

Each `enqueue_meili_sync` call uses saq's `unique=True` with `key=f"meili_sync:{entity}:{entity_id}"`. If a job with that key is already queued, the duplicate enqueue is dropped silently. Effect: a user editing a trip 3 times in 5 seconds triggers 1 Meili upsert, not 3.

### 5.3 The `sync_meili` task

```python
async def sync_meili(ctx: dict[str, Any], *, entity: str, entity_id: str) -> None:
    """Upsert one Trip or Segment to Meili. On delete, the entity is gone
    from Postgres — the task removes it from Meili instead."""
    settings: Settings = ctx["settings"]
    engine = ctx["engine"]
    SessionMaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    client = ctx["meili"]  # singleton from startup

    rid = uuid.UUID(entity_id)
    async with SessionMaker() as db:
        if entity == "trip":
            row = await db.get(Trip, rid)
            if row is None:
                await client.index("trips").delete_document(str(rid))
            else:
                doc = trip_to_doc(row, db=db)  # awaits trip_travelers
                await client.index("trips").update_documents([doc])
        elif entity == "segment":
            row = await db.get(Segment, rid)
            if row is None:
                await client.index("segments").delete_document(str(rid))
            else:
                doc = segment_to_doc(row, db=db)
                await client.index("segments").update_documents([doc])
        else:
            raise ValueError(f"unknown entity: {entity}")
```

`trip_to_doc` and `segment_to_doc` live in `search/sync.py`; they read related rows (e.g., `trip_travelers` for the segment's trip) and produce the dict matching the index schema.

### 5.4 Failure handling

| Failure | Behavior |
|---|---|
| Meili down | saq's per-job `retries=5` with exponential backoff (default ~1s, 2s, 4s, 8s, 16s). Final failure logs + drops; reindex covers recovery. |
| Postgres `db.get` returns `None` | Treated as "row was deleted" — issue Meili `delete_document(id)`. Idempotent. |
| Index doesn't exist (cold start) | sync_meili creates it on demand via `client.create_index(...)` if a 404 surfaces. Configured via the `reindex` command on first deploy too. |
| Meili master key wrong | Worker container errors out at startup (singleton client init fails fast). Operator visibility is good. |

---

## 6. Search proxy: `/api/search/<index>`

### 6.1 Route shape

```
POST /api/search/trips      { q: str, limit: int = 20 }   → 200 { hits: [...], estimatedTotalHits: int }
POST /api/search/segments   { q: str, limit: int = 20 }   → 200 { hits: [...], estimatedTotalHits: int }
```

Auth: existing `require_user` dependency (Authelia session cookie). No tenant tokens, no Bearer headers from the browser.

### 6.2 Server-side filter injection

The handler always injects `filter = "traveler_ids = <user.id>"` regardless of any client-supplied filter. The browser cannot search outside the authenticated user's own trips/segments.

```python
@router.post("/api/search/{index}")
async def search(
    index: Literal["trips", "segments"],
    body: SearchRequest,
    user: User = Depends(require_user),
    meili: MeiliClient = Depends(get_meili),
) -> SearchResponse:
    results = await meili.index(index).search(
        query=body.q,
        opt_params={
            "filter": f"traveler_ids = '{user.id}'",
            "limit": min(body.limit, 50),
        },
    )
    return SearchResponse(hits=results["hits"], total=results.get("estimatedTotalHits", 0))
```

### 6.3 Proxying considerations

- **Single round-trip overhead:** browser → app → Meili → app → browser. At LAN speeds and Meili's <50ms target, ~1–3ms added. Imperceptible.
- **No streaming response needed.** Result sets are small (≤50).
- **Rate limits:** none in v0.4.0. Meili is internal-network only and the user can only DOS themselves.

---

## 7. ⌘K palette UI

### 7.1 Component shape

`templates/_search_palette.html` is included from `base.html`. Single Alpine.js component:

```html
<div x-data="searchPalette()" @keydown.window.meta.k.prevent="open()" @keydown.window.ctrl.k.prevent="open()">
  <div x-show="isOpen" @keydown.escape="close()" class="...modal styling...">
    <input x-model="query" @input.debounce.150ms="search()" placeholder="Search trips and segments…">
    <ul>
      <template x-for="hit in trips"><li @click="goto(hit)" :class="{ 'bg-zinc-100': isActive(hit) }">…</li></template>
      <template x-for="hit in segments"><li @click="goto(hit)" :class="{ 'bg-zinc-100': isActive(hit) }">…</li></template>
    </ul>
  </div>
</div>
```

The Alpine component manages: `isOpen` (bool), `query` (string), `trips` (array), `segments` (array), `activeIdx` (int for keyboard nav).

### 7.2 Behavior

- **Open:** ⌘K (Mac) or Ctrl+K (Linux/Windows). `Escape` closes. Click-outside closes.
- **Search:** debounced 150ms after each keystroke. From char 1 (no minimum query length). Empty query clears results, no recent-trips state.
- **Results:** two `<ul>` sections, headers "Trips" and "Segments". Up to 5 of each.
- **Keyboard navigation:** ↑/↓ moves highlight across both lists (concatenated). Enter activates.
- **Deep-link on activate:**
  - Trip → `window.location = "/trips/<hit.id>"`.
  - Segment → `window.location = "/trips/<hit.trip_id>#segment-<hit.id>"`.

### 7.3 Anchor IDs

`templates/segments/_row.html` gets `id="segment-{{ s.id }}"` on the outer `<li>`. The browser's native `#fragment` scroll is sufficient; no JS smooth-scroll required.

### 7.4 Failure modes

- **Network error fetching `/api/search/...`:** show "Search unavailable" inline. No retry — the user can re-type.
- **Empty results:** show "No matches" instead of an empty list.
- **First open after a fresh deploy with empty Meili:** behaves like empty results. The reindex command populates Meili; until then, ⌘K shows nothing.

---

## 8. CLI: `reindex`

### 8.1 Invocation

```bash
docker compose exec trip-tracker-app python -m trip_tracker reindex
```

Optional flags:
- `--dry-run` — log what would be upserted, don't actually call Meili.
- `--batch-size N` — defaults to 100; tune for very large backlogs.

### 8.2 Behavior

1. Connect to Meili.
2. Delete both indexes (`trips`, `segments`) if they exist. Re-create with the configured filterable + sortable attributes (§4.3).
3. Stream Trip rows from Postgres in batches of N. For each batch: render docs, batch-upsert via `client.index("trips").update_documents(batch)`.
4. Stream Segment rows similarly.
5. Print summary: `"trips: 42 indexed | segments: 187 indexed | duration 3.1s"`.

Idempotent: running it twice produces the same final index state.

### 8.3 When to run

- After first deploy of v0.4.0 (initial population).
- After Meili upgrade.
- After a missed `enqueue_meili_sync` call (recovery).
- After a destructive Postgres operation that bypassed the ORM (rare).

NOT needed: after individual writes — those are handled by the saq sync task.

---

## 9. Configuration

New environment variables:

```env
# --- Required ---
MEILI_URL=http://trip-tracker-search:7700
MEILI_MASTER_KEY=<32-byte secret; generate with `openssl rand -hex 32`>
```

`Settings` (Pydantic) gains corresponding fields. `MEILI_MASTER_KEY` is a `SecretStr`.

### docker-compose updates

```yaml
trip-tracker-search:
  image: getmeili/meilisearch:v1.13
  restart: unless-stopped
  environment:
    MEILI_MASTER_KEY: ${MEILI_MASTER_KEY}
    MEILI_ENV: production
  volumes:
    - trip-tracker-meili:/meili_data
  networks: [internal]
  # NOT exposed via Traefik. Internal-network only. Browser hits /api/search/* on the app.

# Add MEILI_URL + MEILI_MASTER_KEY to trip-tracker-app and trip-tracker-worker env blocks.

volumes:
  trip-tracker-meili:
```

Meili's data volume is unlike Redis (which has none): the index *is* persistent on disk because rebuild-from-Postgres takes longer than restart-from-disk. The volume reduces post-restart cold-start time. The `reindex` command remains the canonical recovery path.

---

## 10. Test strategy

**Five layers** mirror Phase 3:

1. **Unit tests for `search/sync.py`** — `trip_to_doc(trip, db) == {...}`; `segment_to_doc(seg, db) == {...}`. Pure functions over DB rows. Real Postgres via `db_session` fixture.
2. **Mocked saq task tests** — `tests/test_search_sync_task.py` patches the Meili client; verifies `update_documents` is called with the right doc; verifies delete-on-missing-row.
3. **Proxy route tests** — `tests/test_routes_search.py`: hit `/api/search/segments` with a session cookie, mock Meili, assert the `traveler_ids = <user.id>` filter is injected regardless of any client-supplied params, assert results pass through.
4. **Integration test against a real Meili container** — `tests/test_search_integration.py`, marked `@pytest.mark.live_meili` (skipped in CI by default; runs locally before tagging). Spins up a Meili container via `pytest-docker` or relies on `docker-compose up`; runs reindex; verifies queries return expected hits.
5. **⌘K palette UI smoke test** — Playwright, part of the Task 11 verification gate. Open the app, press ⌘K, type "Paris", assert a result appears, click it, assert URL navigates.

**Coverage:** ≥85% project-wide.

**Mock pattern for Meili:** A small `MeiliClient` Protocol in `search/client.py` with the methods we use (`index(name).update_documents`, `index(name).delete_document`, `index(name).search`). Tests inject a fake `MeiliClient` via the `Depends(get_meili)` override.

---

## 11. Migration (v0.3.0 → v0.4.0)

- **arq → saq** — all in-flight ARQ jobs are dropped at cutover. Redis is volume-less (per Phase 3 design); ephemeral by intent. After deploy, run `python -m trip_tracker parse_pending` to re-enqueue any stranded RawEmails.
- **Meili index initial population** — after deploy, run `python -m trip_tracker reindex` once. From then on, sync is automatic.
- **redis-py 5 → 7** — included in Task 1 (saq migration). No persistent state; ephemeral queue means no data conversion.
- **No schema breaks.** Postgres schema is unchanged. Meilisearch is a new system; no migration from a prior search system.

---

## 12. Done definition for Phase 4

- All 11 plan tasks merged to `main`.
- CI green (lint + typecheck + test + security + docker + djlint).
- Coverage ≥ 85 %.
- saq migration verified: worker container starts under saq, webhook → saq enqueue → worker → DB write end-to-end works.
- Meilisearch container running, internal-network only.
- `/api/search/segments` returns matches for a known segment; rejects requests without an authenticated session.
- ⌘K palette opens on Cmd/Ctrl+K, search-as-you-type works, deep-links to `/trips/.../#segment-...` correctly.
- `python -m trip_tracker reindex` rebuilds both indexes from Postgres; subsequent searches return correct results.
- Playwright smoke test of ⌘K passes.
- `v0.4.0` tag pushed; release workflow produces signed multi-arch GHCR image; release-verification scheduled agent confirms tag landed cleanly.

After this lands, return to brainstorming/writing-plans for **Phase 5 — Documents + OCR** (vault, PDF text extraction, Tesseract worker, document index as Meili's 3rd index, presigned download URLs).
