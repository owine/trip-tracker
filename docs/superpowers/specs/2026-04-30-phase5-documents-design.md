# Phase 5 — Documents (text-PDF) Design

**Status:** Approved (brainstorm 2026-04-30, owine + Claude).
**Target tag:** `v0.5.0`.
**Predecessors:** Phase 1 (auth), Phase 2 (raw-email webhook), Phase 3 (parsers + saq worker), Phase 4 (Meilisearch + ⌘K palette).
**Successor (sketched):** Phase 5.1 — OCR (Tesseract + pdf2image + image attachments).

---

## 1. Goal

Enable a single user to attach **text-extractable PDFs** (boarding passes, hotel confirmations, e-tickets, vouchers) to trips and segments, search their full text via the Phase 4 ⌘K palette, and download them with auth-scoped URLs. Documents arrive via two paths: manual upload from the UI, and auto-extraction from email attachments persisted by an extended Phase 2 webhook.

**Out of scope for v0.5.0:** OCR, image attachments (PNG/JPG/HEIC), S3 storage, user-controlled categories, drag-and-drop, document versioning, attachment thumbnails, deep-linking from raw-email body to its attached doc.

---

## 2. Scope decisions (locked during brainstorm)

| # | Decision | Choice |
|---|---|---|
| 1 | Ingestion entry points | Manual upload **+** email attachments (extends Phase 2 webhook) |
| 2 | Storage backend at v0.5.0 | Local filesystem only (`StorageBackend` Protocol future-proofs S3) |
| 3 | Extraction scope | `pdfplumber` only — no OCR, no image attachments |
| 4 | Doc → segment auto-linking | Filename-heuristic match (confirmation # / vehicle # / date) |
| 5 | UI placement | Both trip-level (`Trip → Documents` tab) and segment-inline |
| 6 | Categorization | None — Meili indexes filename + extracted text, no `category` column |
| 7 | Dedup + delete | `UNIQUE (owner_user_id, sha256)`; cascade on trip delete; SET NULL on segment delete |

---

## 3. Architecture overview

```
                ┌────────────────────────────────────┐
                │ Phase 2 webhook (/api/ingest/email)│
                │  + multipart attachment extractor  │
                └─────────────────┬──────────────────┘
                                  │ for each PDF part
                                  ▼
manual upload  ──▶  ┌─────────────────────────────┐  ──▶  saq enqueue
(trip / segment)    │  documents.create_document  │       extract_document
                    └─────────────────┬───────────┘
                                      ▼
                ┌────────────────────────────────────┐
                │  StorageBackend.put(sha256, bytes) │
                │  → LocalFsStorage(/data/documents) │
                └────────────────────────────────────┘
                                  ▲
                                  │ open(storage_key)
                                  │
                  ┌───────────────┴───────────────────┐
                  │ saq: extract_document(doc_id)     │
                  │   pdfplumber → extracted_text     │
                  │   → enqueue_meili_sync(documents) │
                  └───────────────────────────────────┘
                                  │
                                  ▼
                  ┌───────────────────────────────────┐
                  │ Meili index "documents" (3rd idx) │
                  │ ← /api/search/documents (proxy)   │
                  │ ← ⌘K palette                      │
                  └───────────────────────────────────┘
```

---

## 4. Data model

### 4.1 New table — `documents`

```sql
CREATE TABLE documents (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_user_id   uuid NOT NULL REFERENCES users(id)      ON DELETE CASCADE,
  trip_id         uuid     REFERENCES trips(id)           ON DELETE CASCADE,
  segment_id      uuid     REFERENCES segments(id)        ON DELETE SET NULL,
  raw_email_id    uuid     REFERENCES raw_emails(id)      ON DELETE SET NULL,
  filename        text NOT NULL,
  mime_type       text NOT NULL,
  size_bytes      bigint NOT NULL,
  sha256          text NOT NULL,                          -- 64 hex chars
  storage_key     text NOT NULL,                          -- '<sha[:2]>/<sha>'
  extracted_text  text,
  extract_status  text NOT NULL DEFAULT 'pending',
                  -- 'pending' | 'extracted' | 'empty' | 'unsupported' | 'failed'
  extract_method  text,                                   -- 'pdfplumber' | NULL
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (owner_user_id, sha256)
);
CREATE INDEX ix_documents_trip_id    ON documents(trip_id);
CREATE INDEX ix_documents_segment_id ON documents(segment_id);
CREATE INDEX ix_documents_owner      ON documents(owner_user_id);
```

Alembic migration: `phase5_documents`. SQLAlchemy ORM uses `mapped_column(server_default="pending")` for `extract_status` to mirror DB-side default at the Python layer (per the Phase 3 lesson — `LlmBudget.cost_cents` had only DB-side default and bit us).

### 4.2 Cascade semantics

- **Trip delete → documents delete** via SQL `ON DELETE CASCADE` on `trip_id`. Disk files are removed by a SQLAlchemy `after_delete` event listener that calls `storage.delete(storage_key)`. The listener is registered in `trip_tracker.documents.events` and tested against a real Postgres + tmp-path FS.
- **Segment delete → segment_id set NULL** (`ON DELETE SET NULL`). The doc survives the segment delete and remains attached to the trip. Rationale: deleting a flight segment shouldn't destroy the boarding-pass PDF.
- **User delete → documents delete** (`ON DELETE CASCADE` on `owner_user_id`). Disk cleanup via the same listener.

### 4.3 Uniqueness — `UNIQUE (owner_user_id, sha256)`

One row per user per file content. Re-uploading the same PDF returns the existing row (HTTP 303 redirect to its detail view with a flash). Re-forwarding the same email reuses the existing `documents` row and just updates `raw_email_id` (last-write-wins) — see §6.2 for the exact UPSERT behavior.

---

## 5. Storage subsystem

### 5.1 Protocol

New module `src/trip_tracker/documents/storage.py`:

```python
class StorageBackend(Protocol):
    async def put(self, sha256: str, content: AsyncIterator[bytes]) -> str:
        """Stream content to storage. Returns the storage_key."""
    async def open(self, storage_key: str) -> AsyncIterator[bytes]:
        """Open for reading. Caller is responsible for closing."""
    async def delete(self, storage_key: str) -> None:
        """Idempotent: missing key is not an error."""
    def absolute_path(self, storage_key: str) -> str | None:
        """Return a local FS path for X-Accel, or None if backend can't."""
```

### 5.2 `LocalFsStorage`

Same module. Constructor takes `root: Path` (= `settings.documents_dir`, default `/data/documents`).

- **Key shape:** `<sha256[:2]>/<sha256>` — first two hex chars as subdirectory. Caps any single dir at ~256 sibling subdirs and keeps each leaf dir at ~16 entries on average for a corpus of ~4k docs.
- **`put`** writes to `<root>/<key>.tmp` then `os.rename`s to `<root>/<key>` for atomicity. Creates the subdir if missing. If `<root>/<key>` already exists (concurrent identical write), discards the temp and returns the key — content-addressed = idempotent.
- **`open`** validates the key against the regex `^[0-9a-f]{2}/[0-9a-f]{64}$` before any FS access (path-traversal guard); rejects with `ValueError` otherwise.
- **`delete`** unlinks the file; missing-file is not an error. Does not prune empty parent dirs (cheap, not worth the rmdir-race complexity).
- **`absolute_path`** returns `<root>/<key>` for X-Accel mode; same regex guard.

### 5.3 Settings

Two new env vars:

- `DOCUMENTS_DIR` — default `/data/documents`.
- `MAX_UPLOAD_BYTES` — default `26214400` (25 MiB).
- `USER_QUOTA_BYTES` — default `5368709120` (5 GiB) per `owner_user_id`.
- `DOCUMENTS_X_ACCEL_PREFIX` — optional. When set (e.g., `/internal-documents`), the download handler emits `X-Accel-Redirect`; when unset, it streams via `FileResponse`.

---

## 6. Ingestion paths

### 6.1 Manual upload

New module `src/trip_tracker/routes/documents.py`:

| Method + Path | Purpose |
|---|---|
| `POST /trips/{trip_id}/documents` | Upload to trip; `segment_id` NULL |
| `POST /segments/{segment_id}/documents` | Upload pre-linked to segment (derives trip_id) |
| `GET /trips/{id}/documents` | HTMX partial: list + upload form |
| `GET /documents/{id}/download` | Serve file (auth-checked) |
| `DELETE /documents/{id}` | Delete row + file + Meili doc |
| `POST /documents/{id}/link` | Attach to a segment (form: `segment_id`) |
| `POST /documents/{id}/unlink` | Set `segment_id` to NULL |

**Upload pipeline** (shared between trip- and segment-scoped POST):

1. Auth: `require_user`; ownership check on the target trip/segment via `TripTraveler`.
2. Read multipart in chunks; reject if cumulative bytes > `MAX_UPLOAD_BYTES` (return 413).
3. Compute streaming sha256 to a `BytesIO` (or `SpooledTemporaryFile` for ≥1 MiB).
4. **MIME + magic check.** First 4 bytes must equal `b"%PDF"`. `Content-Type` from the form is advisory only.
5. Quota check: `SELECT COALESCE(SUM(size_bytes), 0) FROM documents WHERE owner_user_id=:u`. If `+size_bytes > USER_QUOTA_BYTES`, return 413.
6. Try `INSERT ... ON CONFLICT (owner_user_id, sha256) DO NOTHING RETURNING id`. If conflict (returned no id), fetch the existing doc by `(owner_user_id, sha256)` and redirect to its detail view with `flash("Already uploaded")`. If new, `await storage.put(...)`, then `await db.commit()`.
7. `await enqueue_meili_sync("documents", id)` is **NOT** called here — the doc has no extracted text yet. Sync happens after extraction. (Exception: re-uploading an already-extracted file: nothing to sync.)
8. `await queue.enqueue("extract_document", document_id=str(id))`.

### 6.2 Email attachments (extends Phase 2)

Phase 2's webhook handler currently parses MIME parts, persists `raw_email.body_html` / `body_text`, dedupes by body hash, enqueues `parse_raw_email`. Phase 5 extends `_persist_raw_email` (or whatever Phase 2's helper is named in `src/trip_tracker/ingest/webhook.py`) to also enumerate attachments via `email.message.EmailMessage.iter_attachments()`.

For each attachment:

1. **Filter to PDFs only.** `Content-Type` must start with `application/pdf` AND the first 4 bytes must equal `b"%PDF"`. Non-PDFs (HEIC, JPG, DOCX) are silently dropped in v0.5.0 (logged at INFO, not ERROR — these aren't bugs, they're intentionally unsupported until Phase 5.1).
2. Compute sha256 of the attachment payload.
3. **Auto-link heuristic** (§7) against the segments produced from this email by Phase 3's parser. Run *before* the INSERT so we can populate `segment_id` and derive `trip_id`.
4. UPSERT:
   ```sql
   INSERT INTO documents (...) VALUES (...)
   ON CONFLICT (owner_user_id, sha256) DO UPDATE
     SET raw_email_id = EXCLUDED.raw_email_id,
         updated_at   = now()
   RETURNING id, (xmax = 0) AS inserted;
   ```
   If `inserted = true`, this is a new row — call `storage.put` and enqueue extraction. If `inserted = false`, just attach the new `raw_email_id` and skip storage write + extraction (the file is identical and already extracted, or extraction is in flight from the prior insert).

The webhook stays synchronous on the wire; storage writes for typical 100–500 KiB boarding passes are ~10ms.

**Phase 2 ↔ Phase 5 interaction note.** Phase 3's `parse_raw_email` saq job runs *after* the webhook returns, so segments may not yet exist when the webhook persists attachments. Therefore: the webhook persists attachments with `segment_id = NULL` and `trip_id = NULL`, and the auto-link step runs *inside `parse_raw_email`* after segments are created. `parse_raw_email` already loads the raw_email row; it can `SELECT id FROM documents WHERE raw_email_id = :rid AND segment_id IS NULL`, run the heuristic, and `UPDATE` matching docs with `segment_id` and `trip_id`. This keeps the webhook fast and gives the parser the chance to find segments first.

---

## 7. Auto-link heuristic

New pure function `match_attachment_to_segment(filename: str, segments: Sequence[Segment]) -> uuid.UUID | None` in `src/trip_tracker/documents/autolink.py`.

Three layered rules, **first match wins**:

1. **Confirmation number.** For each segment with a `confirmation_number`, compile `re.compile(rf"\b{re.escape(seg.confirmation_number)}\b", re.IGNORECASE)` and search the filename. Match → return `seg.id`.
2. **Vehicle / flight / train number.** For each segment with `details->>'flight_number'` (flight) or `details->>'train_number'` (train), apply the same `\b...\b` regex.
3. **Date.** Extract `YYYY-MM-DD` or `YYYYMMDD` from the filename. If exactly one segment has `start_at::date == that date`, return its id. Two or more matches → no auto-link (ambiguous).

If no rule matches, return `None` → caller leaves `segment_id` NULL (= "trip-level only" per Q4 fallback).

**Test corpus** (drive coverage via table-driven tests): real boarding-pass filenames from each Phase 3 vendor pack — Air France (`AF7237_BoardingPass_AB12CD.pdf`), American (`AA-eTicket-XYZ123.pdf`), Amtrak (`Amtrak_Ticket_2026-06-01.pdf`), Chase Travel (`Itinerary_K8YH3M_2026.pdf`), etc. Add fixtures under `tests/fixtures/autolink/` if needed.

---

## 8. Extraction (saq task)

New module `src/trip_tracker/documents/extract.py`:

```python
async def extract_document(ctx: ExtractCtx, *, document_id: str) -> None:
    ...
```

Registered in `worker.settings["functions"]` alongside `parse_raw_email` and `sync_meili`. `ExtractCtx` has the same shape as the existing parser context: `engine`, `settings`, `storage` (new — built at worker startup). The startup hook (`worker.startup`) gains:

```python
ctx["storage"] = LocalFsStorage(Path(settings.documents_dir))
```

**Body:**

1. Open a session, load the document by id.
2. If `extract_status != 'pending'`, log `INFO` and return — idempotent re-run.
3. `if doc.mime_type != 'application/pdf': set extract_status = 'unsupported'`, commit, return.
4. Open `storage.open(doc.storage_key)` → spool to a `BytesIO` (pdfplumber needs seekable).
5. With `asyncio.wait_for(loop.run_in_executor(None, _extract_pdf, buf), timeout=60.0)`:
   - `_extract_pdf` opens via `pdfplumber.open(buf)`, iterates pages, joins page text with `"\n\n"`, returns `(text, page_count)`.
   - On `pdfplumber.PDFSyntaxError` / `PSException` / generic `Exception` → re-raise as `ExtractFailure`.
6. If extraction returns ≥1 char of text: `extract_status='extracted'`, `extract_method='pdfplumber'`, `extracted_text=<text>`. If returns 0 chars: `extract_status='empty'`. On `ExtractFailure` or `asyncio.TimeoutError`: `extract_status='failed'`, `extracted_text=NULL`.
7. Commit.
8. `await enqueue_meili_sync("documents", document_id)` (only when `extract_status` ∈ `{extracted, empty}` — failed/unsupported docs aren't worth searching).

**saq retry policy:** default 3 retries with exponential backoff. pdfplumber failures are usually deterministic, but transient FS issues (e.g., NAS mount blip) are real.

---

## 9. Search integration

### 9.1 Third Meili index — `documents`

`ensure_indexes_configured` (Phase 4 Task 9) gains a third loop entry:

```python
_DOCUMENTS_FILTERABLE = ["traveler_ids", "trip_id", "segment_id", "owner_user_id"]
_DOCUMENTS_SORTABLE   = ["created_at_unix"]
```

### 9.2 `document_to_doc(doc, db) -> dict`

New function in `src/trip_tracker/search/sync.py`:

```python
{
  "id": str(doc.id),
  "owner_user_id": str(doc.owner_user_id),
  "trip_id":     str(doc.trip_id)     if doc.trip_id     else None,
  "segment_id":  str(doc.segment_id)  if doc.segment_id  else None,
  "traveler_ids": [<str(uid) for uid in trip's TripTraveler rows>],
  "filename": doc.filename,
  "extracted_text": doc.extracted_text or "",
  "mime_type": doc.mime_type,
  "created_at_unix": int(doc.created_at.timestamp()),
}
```

`traveler_ids` derived the same way as `trip_to_doc` / `segment_to_doc` — query `TripTraveler` for the doc's trip. If `trip_id` is NULL (unlikely after the parser auto-links, but possible for an orphan upload), `traveler_ids = [str(doc.owner_user_id)]`.

### 9.3 `enqueue_meili_sync("documents", id)` — wiring sites

Two new write sites only:

- `extract_document` saq task (after persisting text).
- `delete_document` route handler (before deleting the row, so the Meili `delete_document` call still has a valid id).

The `sync_meili` saq task already has a match dispatch on entity name; add a `case "documents"` arm that calls `document_to_doc` (or `meili.index("documents").delete_document(id)` for the delete path — same dual-mode dispatch as trips/segments).

### 9.4 Proxy + palette

`POST /api/search/{index}` (Phase 4 Task 7) currently typed `Literal["trips", "segments"]`. Broaden to `Literal["trips", "segments", "documents"]`. The `traveler_ids = '<user>'` filter injection works unchanged.

`_search_palette.html` already issues parallel `fetch` to all known indexes — add `"documents"` to the indexes array. Result rendering for documents:

```
📄 <filename>  ·  <trip title>
<snippet from Meili _formatted highlighting>
```

Click action:
- If the doc has `segment_id`: navigate to `/trips/{trip_id}#segment-{segment_id}`.
- Else if `trip_id`: navigate to `/trips/{trip_id}/documents` (the docs tab).
- Else (orphan): navigate to `GET /documents/{id}/download` directly.

### 9.5 Reindex CLI extension

`reindex_all` (Phase 4 Task 10) gains a third walk: `for doc in (await db.execute(select(Document))).scalars().all(): batch.append(await document_to_doc(doc, db=db))`. Same delete-then-recreate pattern. The CLI stays one command — `python -m trip_tracker reindex` — and reports `trips=N segments=M documents=K`.

---

## 10. Serving — `GET /documents/{id}/download`

```python
@router.get("/documents/{document_id}/download")
async def download(document_id: UUID, user: User = Depends(require_user), ...):
    doc = await get_or_404(db, Document, document_id)
    # Ownership check: doc.owner_user_id == user.id OR user is a traveler on doc.trip_id
    if not _can_access(doc, user, db): raise HTTPException(403)

    if settings.documents_x_accel_prefix:
        # Production behind a reverse proxy
        return Response(
            status_code=204,
            headers={
                "X-Accel-Redirect": f"{settings.documents_x_accel_prefix}/{doc.storage_key}",
                "Content-Disposition": f'attachment; filename="{escape(doc.filename)}"',
                "Content-Type": doc.mime_type,
            },
        )

    # Dev / no proxy — stream directly
    path = storage.absolute_path(doc.storage_key)
    return FileResponse(path, media_type=doc.mime_type, filename=doc.filename)
```

`_can_access` returns true if the user is the owner OR the user is in `TripTraveler` for `doc.trip_id`. Always re-fetch from DB; never trust the URL.

**Reverse-proxy setup (documented in README).** The example Traefik / Nginx snippet for `internal-documents` location maps `/internal-documents/<key>` to a non-public mount of `/data/documents/<key>`, marked `internal;` (Nginx) or with an equivalent middleware (Traefik). The README explicitly calls out: **the `internal-documents` location MUST NOT be reachable from the public side** — otherwise URL-guessing a `storage_key` bypasses auth. Self-hosters who don't want to wire this up can leave `DOCUMENTS_X_ACCEL_PREFIX` unset and accept the FastAPI streaming penalty (typical home-lab traffic doesn't notice).

---

## 11. UI

### 11.1 Trip detail — Documents tab/section

`src/trip_tracker/templates/trips/_documents.html` (new partial). Listed under the existing trip page, either as a tab or a collapsible section (decide during plan/implementation by looking at current trips/_detail.html structure). Each row:

```
📄 boarding-pass-AF007.pdf            [📧 Email]  [→ AF007 Paris flight]
   1.2 MB · ✅ extracted · uploaded 2026-04-30
   [Download]  [Link to segment ▼]  [Delete]
```

Source badge: `📧 Email` if `raw_email_id IS NOT NULL`, else `📤 Upload`. Segment chip is clickable — navigates to the segment row anchor (Phase 4 added `#segment-<id>` on segment rows). Status icons: `✅ extracted`, `⏳ pending`, `🚫 empty/unsupported/failed` (one icon per non-extracted state with a tooltip explaining).

Upload widget at the top: standard `<input type="file" accept=".pdf">` POSTs to `/trips/{id}/documents`. After successful upload, HTMX swaps in the updated list partial.

### 11.2 Segment detail — inline documents list

Below the segment fields on segment-detail (or its edit form), a `Documents` section listing docs where `segment_id == this.id`. Same row layout minus the segment chip. Plus an "Upload to this segment" form posting to `/segments/{id}/documents`.

### 11.3 Empty states

- Trip has no docs: "No documents yet. Upload boarding passes, hotel confirmations, or vouchers — they'll be searchable via ⌘K."
- Segment has no docs: "No documents linked. Upload one, or link an existing trip-level doc via the Trip → Documents tab."

---

## 12. Done definition

- [ ] Manual upload of a PDF from trip-level + segment-level routes works end-to-end (auth, magic-byte check, sha256 dedup, 25 MiB cap, quota check).
- [ ] A forwarded email with one PDF attachment auto-creates a `documents` row, runs the auto-link heuristic, persists the file, populates `extracted_text` after the saq job runs.
- [ ] Re-forwarding the same email reuses the existing doc row (UPSERT on `(owner_user_id, sha256)`); does not double-write storage; does not double-enqueue extraction.
- [ ] Re-uploading the same PDF returns 303 to the existing doc with a flash; does not duplicate row or file.
- [ ] ⌘K finds documents by filename and by extracted text, scoped to the authenticated user's `traveler_ids`. Verified end-to-end with a real boarding-pass PDF.
- [ ] `GET /documents/{id}/download` serves the file with proper `Content-Disposition`; returns 403 for non-owners, 401 for anonymous.
- [ ] X-Accel mode emits `X-Accel-Redirect` with no body and the right `Content-Disposition`. FileResponse fallback works with `DOCUMENTS_X_ACCEL_PREFIX` unset.
- [ ] `DELETE /documents/{id}` removes DB row + disk file + Meili doc.
- [ ] Deleting a trip cascades to its documents (DB rows + disk files via `after_delete` listener).
- [ ] Deleting a segment sets the document's `segment_id` to NULL; the doc and file survive.
- [ ] `python -m trip_tracker reindex` walks documents and rebuilds the third index from Postgres.
- [ ] Path traversal guard: a forged `storage_key` of `../etc/passwd` is rejected by `LocalFsStorage` validation.
- [ ] `MAX_UPLOAD_BYTES`, `USER_QUOTA_BYTES`, `DOCUMENTS_DIR`, `DOCUMENTS_X_ACCEL_PREFIX` settings are loaded from env via `Settings`.
- [ ] README "Documents (Phase 5)" section documents upload, the auto-link heuristic, the `MAX_UPLOAD_BYTES`/quota envs, and the `internal-documents` reverse-proxy setup.
- [ ] 85% project-wide coverage holds. No new bandit findings. Strict mypy + ruff target=py313 clean. djlint clean.
- [ ] Signed tag `v0.5.0` pushed; release workflow produces signed multi-arch GHCR image; release-verification scheduled agent confirms.

---

## 13. Risks & mitigations

| # | Risk | Mitigation |
|---|---|---|
| 1 | Path traversal via crafted `storage_key` | LocalFsStorage validates regex `^[0-9a-f]{2}/[0-9a-f]{64}$` before any FS call. Unit-tested. |
| 2 | MIME spoofing (attacker-controlled `Content-Type`) | Magic-byte check (`%PDF-` first 4 bytes) at ingest time. Reject mismatches. |
| 3 | Disk fill from large/many uploads | `MAX_UPLOAD_BYTES=25MiB` per file + `USER_QUOTA_BYTES=5GiB` per owner. Both env-tunable. 413 on exceed. |
| 4 | X-Accel reverse-proxy misconfig leaks files | README explicitly requires `internal;` (Nginx) / equivalent middleware (Traefik). Compose snippet included as a starting point. |
| 5 | pdfplumber memory blowup on pathological PDFs | 60s timeout via `asyncio.wait_for` around extraction; saq retry covers transient FS hiccups but not deterministic PDF errors. |
| 6 | Phase 2 dedup interaction (re-forwarded email) | UPSERT on `(owner_user_id, sha256)` reuses the row; `(xmax = 0)` distinguishes new-vs-existing so we don't double-storage-write or double-enqueue. |
| 7 | Auto-link heuristic links to wrong segment for ambiguous filenames | Date rule requires *exactly one* segment match; conf# / vehicle# rules are essentially exact-match. UI always lets the user re-link manually. |
| 8 | Migration on a populated DB (you, on Synology) | The migration only adds a new table — no in-place transforms. Backwards-compatible: rolling back drops `documents` and any uploaded files become orphan inodes (harmless; `bin/cleanup-orphans` script can be added in v0.5.1 if needed). |

---

## 14. Sequencing (rough — full plan from `writing-plans`)

~10 tasks, sized for subagent dispatch:

| # | Task | Model | Notes |
|---|---|---|---|
| 1 | Schema + Alembic migration + ORM model + cascade event listener | sonnet | Touches multiple files; cascade behavior is subtle |
| 2 | StorageBackend Protocol + LocalFsStorage + path-traversal guard | haiku | Pure module, easy to test against tmp_path |
| 3 | sha256 streaming hasher + magic-byte check + size + quota helpers | haiku | Pure utility module |
| 4 | Manual-upload routes (`POST /trips/.../documents`, `POST /segments/.../documents`, `DELETE /documents/{id}`, `POST /link`, `POST /unlink`) | sonnet | Auth-sensitive |
| 5 | Download route (X-Accel + FileResponse fallback) + ownership re-check | sonnet | Security-sensitive |
| 6 | Webhook attachment extraction (extends Phase 2 webhook) | sonnet | Modifies existing code |
| 7 | Auto-link heuristic + parser-pipeline integration (deferred linking inside `parse_raw_email`) | haiku | Pure function + small ORM update |
| 8 | Extraction saq task + worker startup wiring (storage in ctx) + pdfplumber dep | haiku | Mostly mechanical; adds new dep |
| 9 | Meili 3rd index: `document_to_doc`, sync wiring, palette + proxy broadening, reindex CLI extension | sonnet | Touches several Phase 4 surfaces |
| 10 | UI: trip-level docs tab partial + segment-inline list + upload forms | sonnet | HTMX + djlint clean |
| 11 | README + verification gate + tag v0.5.0 | inline | Same shape as v0.2.0/v0.3.0/v0.4.0 |

---

## 15. Future phases (Phase 5.x)

- **Phase 5.1 — OCR.** Tesseract + lang packs + `poppler-utils` + `pdf2image`. Image attachments (PNG/JPG/HEIC). New `extract_status='ocr'` value. Behind an `OCR_ENABLED` flag at first.
- **Phase 5.2 — S3 storage backend.** `S3Storage(bucket, endpoint, …)` via `aioboto3`. MinIO sidecar in compose for self-hosters who want it. Backend selectable via `STORAGE_BACKEND=local|s3` env.
- **Phase 5.3 — Document categories.** Add `category` enum + auto-detect heuristics, only if usage data shows users want to filter by it.
- **Phase 5.4 — Drag-and-drop UI + thumbnails.** Polish; `pdf2image` first page → 240×320 thumbnail cached on disk.
