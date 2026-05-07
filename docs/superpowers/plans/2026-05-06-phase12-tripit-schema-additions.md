# Phase 12: TripIt Schema Additions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the TripIt-cache columns to `trips`/`segments` and create the six new tables (`raw_text`, `raw_document`, `tripit_oauth_credentials`, `tripit_sync_state`, `tripit_notification_log`, `attach_audit`) that the v1.0.0 wrapper architecture depends on. Pure schema + ORM work; no route logic.

**Architecture:** Each new table gets its own SQLAlchemy model file under `src/trip_tracker/models/` and a re-export from `models/__init__.py`. Trip + Segment get new nullable columns appended (existing rows continue to work). One Alembic migration adds everything in one atomic transaction. Constants like `OWNER_USER_ID` from Phase 11 reappear here as `OAUTH_CREDENTIALS_ID` and `SYNC_STATE_ID` (well-known fixed IDs for the single-row operational tables, with a CHECK constraint enforcing the singleton invariant at the DB level).

**Tech Stack:** Python 3.14 · SQLAlchemy 2.0 async (Mapped/mapped_column) · Alembic · PostgreSQL (JSONB, CITEXT, UUID) · pytest.

---

## Pre-flight assumptions

1. Phase 11 has shipped on `v2` (HEAD `2b992ed` or later); 532 tests passing.
2. The local docker-compose Postgres is at migration `f175b03585e7` (Phase 11). Confirm with `uv run alembic current` before starting.
3. `OWNER_EMAIL` and `OWNER_SESSION_TOKEN` are set in `.env` (used by Phase 11 migration; no new env vars in Phase 12).
4. **No live TripIt access required for Phase 12.** This phase only adds schema; Phase 10 (still blocked on TripIt support email) wires the actual TripIt client.
5. Test against the dev DB twice (apply + restore-and-reapply) per the Phase 11 pattern.

---

## File map

### Files modified

- `src/trip_tracker/models/trip.py` — append four nullable columns: `tripit_trip_id`, `tripit_synced_at`, `tripit_etag`, `upstream_deleted_at`. Add a unique index on `tripit_trip_id`.
- `src/trip_tracker/models/segment.py` — append three nullable columns: `tripit_segment_id`, `tripit_segment_type` (text enum at the column level for now; SQLAlchemy `Enum(...)` if it fits the existing style), `tripit_synced_at`. Add a unique index on `tripit_segment_id`.
- `src/trip_tracker/models/__init__.py` — re-export all six new model classes.

### Files created

- `src/trip_tracker/models/raw_text.py` — `RawText` model. Mirrors `RawEmail` shape minus email-specific headers. Carries `candidates JSONB` (overlap candidates persisted at parse time per spec §5).
- `src/trip_tracker/models/raw_document.py` — `RawDocument` model. Includes `sha256` (unique) for dedup, `storage_path` (relative to `data/uploads/`), `attach_only BOOL`, plus `candidates JSONB`.
- `src/trip_tracker/models/tripit_oauth_credentials.py` — `TripItOAuthCredentials` model. Single-row table (CHECK constraint pinning `id` to a well-known UUID). Carries the four OAuth tokens + `last_error`/`last_refreshed_at`.
- `src/trip_tracker/models/tripit_sync_state.py` — `TripItSyncState` model. Single-row table for pull cursor (`last_modified_since`, `last_pull_at`, `last_full_reconcile_at`, `last_error`).
- `src/trip_tracker/models/tripit_notification_log.py` — `TripItNotificationLog` model. Inbound webhook audit (`raw_payload JSONB`, `processed_at`, `error`).
- `src/trip_tracker/models/attach_audit.py` — `AttachAudit` model. Tracks the 10-min undo window (`pushed_segment_ids JSONB`, `pushed_at`, `source_kind` enum, `source_id`, `undone_at`).
- `migrations/versions/2026_05_06_NNNN_<id>_phase12_tripit_schema.py` — single Alembic migration for all the above.

### Test files created

- `tests/test_models_trip_tripit_columns.py` — asserts the four new columns on Trip exist with correct types/nullability.
- `tests/test_models_segment_tripit_columns.py` — asserts the three new columns on Segment.
- `tests/test_models_raw_text.py`
- `tests/test_models_raw_document.py`
- `tests/test_models_tripit_oauth_credentials.py` — also asserts the singleton CHECK constraint via an ORM-level integration test.
- `tests/test_models_tripit_sync_state.py` — same singleton assertion.
- `tests/test_models_tripit_notification_log.py`
- `tests/test_models_attach_audit.py`

### Constants to add

- `OAUTH_CREDENTIALS_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")` — well-known id for the singleton oauth credentials row.
- `SYNC_STATE_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")` — well-known id for the singleton sync state row.

These live in their respective model files (each model exports its own well-known id constant). The pattern mirrors `OWNER_USER_ID` from `src/trip_tracker/auth/session.py`.

---

## Tasks

### Task 1: Extend Trip + Segment with TripIt-cache columns

**Files:**
- Modify: `src/trip_tracker/models/trip.py`
- Modify: `src/trip_tracker/models/segment.py`
- Test: `tests/test_models_trip_tripit_columns.py` (create)
- Test: `tests/test_models_segment_tripit_columns.py` (create)

- [ ] **Step 1: Inspect current model shape**

```bash
cd /Users/owine/Git/trip-tracker
cat src/trip_tracker/models/trip.py
cat src/trip_tracker/models/segment.py
```

Confirm what exists post-Phase-11 so the additions fit the existing style.

- [ ] **Step 2: Write failing tests**

Create `tests/test_models_trip_tripit_columns.py`:

```python
"""Phase 12 schema additions on the Trip model — TripIt-cache columns."""
from __future__ import annotations


def test_trip_has_tripit_trip_id_unique_nullable():
    from trip_tracker.models.trip import Trip
    col = Trip.__table__.columns["tripit_trip_id"]
    assert col.nullable is True
    assert col.unique is True or any(
        ix.unique and "tripit_trip_id" in [c.name for c in ix.columns]
        for ix in Trip.__table__.indexes
    )


def test_trip_has_tripit_synced_at_nullable():
    from trip_tracker.models.trip import Trip
    col = Trip.__table__.columns["tripit_synced_at"]
    assert col.nullable is True


def test_trip_has_tripit_etag_nullable():
    from trip_tracker.models.trip import Trip
    col = Trip.__table__.columns["tripit_etag"]
    assert col.nullable is True


def test_trip_has_upstream_deleted_at_nullable():
    from trip_tracker.models.trip import Trip
    col = Trip.__table__.columns["upstream_deleted_at"]
    assert col.nullable is True
```

Create `tests/test_models_segment_tripit_columns.py`:

```python
"""Phase 12 schema additions on the Segment model — TripIt-cache columns."""
from __future__ import annotations


def test_segment_has_tripit_segment_id_unique_nullable():
    from trip_tracker.models.segment import Segment
    col = Segment.__table__.columns["tripit_segment_id"]
    assert col.nullable is True
    assert col.unique is True or any(
        ix.unique and "tripit_segment_id" in [c.name for c in ix.columns]
        for ix in Segment.__table__.indexes
    )


def test_segment_has_tripit_segment_type_nullable():
    from trip_tracker.models.segment import Segment
    col = Segment.__table__.columns["tripit_segment_type"]
    assert col.nullable is True


def test_segment_has_tripit_synced_at_nullable():
    from trip_tracker.models.segment import Segment
    col = Segment.__table__.columns["tripit_synced_at"]
    assert col.nullable is True
```

- [ ] **Step 3: Run tests, expect failure**

```bash
uv run pytest tests/test_models_trip_tripit_columns.py tests/test_models_segment_tripit_columns.py -v
```

Expected: KeyError on the new column names.

- [ ] **Step 4: Add columns to Trip model**

In `src/trip_tracker/models/trip.py`, append after the existing columns (likely after `updated_at`):

```python
# --- Phase 12: TripIt cache pointers ---
# `unique=True` on a String column gives PostgreSQL a UNIQUE constraint backed
# by a UNIQUE INDEX automatically — no need for index=True. Keep them in sync
# with the migration: the alembic upgrade() should create only the UNIQUE
# constraint (not a separate non-unique index) to avoid duplicate DDL.
tripit_trip_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
tripit_synced_at: Mapped[datetime | None] = mapped_column(nullable=True)
tripit_etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
# Set by the daily reconcile cron when this trip is no longer in TripIt's /list/trip;
# surfaces in the inbox for owner confirmation. Spec §3 / §6.
upstream_deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)
```

Adapt the import line at the top to include `String` if not already imported.

- [ ] **Step 5: Add columns to Segment model**

In `src/trip_tracker/models/segment.py`, append:

```python
# --- Phase 12: TripIt cache pointers ---
tripit_segment_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
# Discriminator for the TripIt segment type. Values: "air", "lodging", "car",
# "rail", "transport", "activity". Stored as text rather than ENUM so we don't
# pin the migration when TripIt adds a new type.
tripit_segment_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
tripit_synced_at: Mapped[datetime | None] = mapped_column(nullable=True)
```

- [ ] **Step 6: Run tests, expect pass**

```bash
uv run pytest tests/test_models_trip_tripit_columns.py tests/test_models_segment_tripit_columns.py -v
```

- [ ] **Step 7: mypy check**

```bash
uv run mypy src/trip_tracker/models/
```

Should be clean.

- [ ] **Step 8: Commit**

```bash
git add src/trip_tracker/models/trip.py src/trip_tracker/models/segment.py \
        tests/test_models_trip_tripit_columns.py tests/test_models_segment_tripit_columns.py
git commit -m "feat(phase12): add TripIt cache columns to Trip + Segment models"
```

---

### Task 2: RawText + RawDocument models

**Files:**
- Create: `src/trip_tracker/models/raw_text.py`
- Create: `src/trip_tracker/models/raw_document.py`
- Modify: `src/trip_tracker/models/__init__.py` (re-exports)
- Test: `tests/test_models_raw_text.py`
- Test: `tests/test_models_raw_document.py`

- [ ] **Step 1: Inspect existing `raw_email.py` for naming style + base class usage**

```bash
cat src/trip_tracker/models/raw_email.py
```

Mirror its conventions in the new files (e.g., uuid id default, `Base` import path, server-side default for `created_at`).

- [ ] **Step 2: Write failing tests**

Create `tests/test_models_raw_text.py`:

```python
"""Phase 12: RawText model — pasted-blob intake row."""
from __future__ import annotations


def test_raw_text_has_required_columns():
    from trip_tracker.models.raw_text import RawText
    cols = {c.name for c in RawText.__table__.columns}
    assert {"id", "body_text", "submitted_at", "parser_audit", "candidates"} <= cols


def test_raw_text_optional_hint_column():
    from trip_tracker.models.raw_text import RawText
    col = RawText.__table__.columns["hint"]
    assert col.nullable is True
```

Create `tests/test_models_raw_document.py`:

```python
"""Phase 12: RawDocument model — uploaded-file intake row."""
from __future__ import annotations


def test_raw_document_has_required_columns():
    from trip_tracker.models.raw_document import RawDocument
    cols = {c.name for c in RawDocument.__table__.columns}
    expected = {
        "id", "filename", "mime_type", "sha256", "storage_path",
        "submitted_at", "parser_audit", "candidates", "attach_only",
    }
    assert expected <= cols


def test_raw_document_sha256_is_unique():
    from trip_tracker.models.raw_document import RawDocument
    col = RawDocument.__table__.columns["sha256"]
    assert col.unique is True or any(
        ix.unique and "sha256" in [c.name for c in ix.columns]
        for ix in RawDocument.__table__.indexes
    )


def test_raw_document_attach_only_default_false():
    from trip_tracker.models.raw_document import RawDocument
    col = RawDocument.__table__.columns["attach_only"]
    assert col.default is not None
```

- [ ] **Step 3: Run, expect failure**

```bash
uv run pytest tests/test_models_raw_text.py tests/test_models_raw_document.py -v
```

Expected: ModuleNotFoundError on the model imports.

- [ ] **Step 4: Implement `src/trip_tracker/models/raw_text.py`**

```python
"""RawText: pasted-blob intake.

A row is created when the owner pastes raw confirmation text into the inbox
paste textarea (Phase 15). Mirrors RawEmail's role for the email-intake path.
The `candidates` JSONB column is populated at parse time with the overlap
candidates list (per spec §5).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from trip_tracker.db import Base


class RawText(Base):
    __tablename__ = "raw_text"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    body_text: Mapped[str] = mapped_column(Text)
    hint: Mapped[str | None] = mapped_column(String(500), nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(server_default=func.now())
    parser_audit: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    candidates: Mapped[list] = mapped_column(JSONB, server_default="[]")
```

(Confirm the actual `Base` import path matches `raw_email.py`. The above assumes `from trip_tracker.db import Base`.)

- [ ] **Step 5: Implement `src/trip_tracker/models/raw_document.py`**

```python
"""RawDocument: uploaded-file intake.

A row is created when the owner uploads a PDF / JPG / PNG / HEIC via the
inbox upload page (Phase 15). The actual blob lives at `storage_path` on the
local filesystem; sha256 enables dedup. `attach_only=True` skips the parse
pipeline (the file is just kept attached, no segments extracted).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from trip_tracker.db import Base


class RawDocument(Base):
    __tablename__ = "raw_documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    filename: Mapped[str] = mapped_column(String(500))
    mime_type: Mapped[str] = mapped_column(String(100))
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    # Path relative to data/uploads/ root (e.g. "2026/05/abcd1234....pdf")
    storage_path: Mapped[str] = mapped_column(String(500))
    submitted_at: Mapped[datetime] = mapped_column(server_default=func.now())
    parser_audit: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    candidates: Mapped[list] = mapped_column(JSONB, server_default="[]")
    attach_only: Mapped[bool] = mapped_column(Boolean, server_default="false")
```

- [ ] **Step 6: Re-export from `__init__.py`**

In `src/trip_tracker/models/__init__.py`:

```python
from trip_tracker.models.raw_document import RawDocument
from trip_tracker.models.raw_text import RawText
```

(Append in alphabetical position; match existing style.)

- [ ] **Step 7: Run tests, expect pass**

```bash
uv run pytest tests/test_models_raw_text.py tests/test_models_raw_document.py -v
```

- [ ] **Step 8: mypy + commit**

```bash
uv run mypy src/trip_tracker/models/
git add src/trip_tracker/models/raw_text.py src/trip_tracker/models/raw_document.py \
        src/trip_tracker/models/__init__.py \
        tests/test_models_raw_text.py tests/test_models_raw_document.py
git commit -m "feat(phase12): add RawText + RawDocument models for paste/upload intake"
```

---

### Task 3: TripIt operational tables — OAuth credentials + sync state + notification log

**Files:**
- Create: `src/trip_tracker/models/tripit_oauth_credentials.py`
- Create: `src/trip_tracker/models/tripit_sync_state.py`
- Create: `src/trip_tracker/models/tripit_notification_log.py`
- Modify: `src/trip_tracker/models/__init__.py`
- Test: `tests/test_models_tripit_oauth_credentials.py`
- Test: `tests/test_models_tripit_sync_state.py`
- Test: `tests/test_models_tripit_notification_log.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_models_tripit_oauth_credentials.py`:

```python
"""Phase 12: TripItOAuthCredentials — singleton row holding OAuth 1.0a tokens."""
from __future__ import annotations

import uuid


def test_tripit_oauth_credentials_has_required_columns():
    from trip_tracker.models.tripit_oauth_credentials import TripItOAuthCredentials
    cols = {c.name for c in TripItOAuthCredentials.__table__.columns}
    expected = {
        "id", "consumer_key", "consumer_secret", "access_token",
        "access_token_secret", "created_at", "last_refreshed_at", "last_error",
    }
    assert expected <= cols


def test_tripit_oauth_credentials_singleton_id_constant():
    from trip_tracker.models.tripit_oauth_credentials import (
        OAUTH_CREDENTIALS_ID, TripItOAuthCredentials,
    )
    assert OAUTH_CREDENTIALS_ID == uuid.UUID("00000000-0000-0000-0000-000000000002")
    # The CHECK constraint pinning id is verified via the migration's
    # CREATE TABLE statement; integration test in T6 (alembic apply) covers it.


def test_tripit_oauth_credentials_secrets_use_text_or_string():
    """Tokens can be long; ensure they're not column-truncated."""
    from trip_tracker.models.tripit_oauth_credentials import TripItOAuthCredentials
    for col_name in ("consumer_secret", "access_token_secret"):
        col = TripItOAuthCredentials.__table__.columns[col_name]
        # Either Text (unbounded) or String with length >= 256
        assert col.type.length is None or col.type.length >= 256
```

Create `tests/test_models_tripit_sync_state.py`:

```python
"""Phase 12: TripItSyncState — singleton row holding the pull cursor."""
from __future__ import annotations

import uuid


def test_tripit_sync_state_has_required_columns():
    from trip_tracker.models.tripit_sync_state import TripItSyncState
    cols = {c.name for c in TripItSyncState.__table__.columns}
    expected = {
        "id", "last_modified_since", "last_pull_at",
        "last_full_reconcile_at", "last_error",
    }
    assert expected <= cols


def test_tripit_sync_state_singleton_id_constant():
    from trip_tracker.models.tripit_sync_state import SYNC_STATE_ID
    assert SYNC_STATE_ID == uuid.UUID("00000000-0000-0000-0000-000000000003")
```

Create `tests/test_models_tripit_notification_log.py`:

```python
"""Phase 12: TripItNotificationLog — inbound webhook audit table."""
from __future__ import annotations


def test_tripit_notification_log_has_required_columns():
    from trip_tracker.models.tripit_notification_log import TripItNotificationLog
    cols = {c.name for c in TripItNotificationLog.__table__.columns}
    expected = {"id", "received_at", "raw_payload", "processed_at", "error"}
    assert expected <= cols


def test_tripit_notification_log_received_at_indexed():
    """Cron prunes by received_at < now-30d; index is essential."""
    from trip_tracker.models.tripit_notification_log import TripItNotificationLog
    has_received_at_index = any(
        "received_at" in [c.name for c in ix.columns]
        for ix in TripItNotificationLog.__table__.indexes
    )
    assert has_received_at_index
```

- [ ] **Step 2: Run, expect failure**

```bash
uv run pytest tests/test_models_tripit_oauth_credentials.py tests/test_models_tripit_sync_state.py tests/test_models_tripit_notification_log.py -v
```

- [ ] **Step 3: Implement `src/trip_tracker/models/tripit_oauth_credentials.py`**

```python
"""TripItOAuthCredentials: singleton row holding OAuth 1.0a credentials.

Populated by the one-time CLI bootstrap (`trip-tracker tripit-auth` — Phase 10).
The CHECK constraint at the DB level pins id to OAUTH_CREDENTIALS_ID, which
guarantees there can never be more than one row regardless of how the app
behaves. Same pattern as OWNER_USER_ID for users.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trip_tracker.db import Base

# Well-known UUID for the singleton oauth credentials row. Stable across
# env wipes; mirrors OWNER_USER_ID = uuid.UUID(int=1) from auth/session.py.
OAUTH_CREDENTIALS_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000002")


class TripItOAuthCredentials(Base):
    __tablename__ = "tripit_oauth_credentials"
    __table_args__ = (
        CheckConstraint(
            f"id = '{OAUTH_CREDENTIALS_ID}'",
            name="ck_tripit_oauth_credentials_singleton",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=lambda: OAUTH_CREDENTIALS_ID
    )
    consumer_key: Mapped[str] = mapped_column(Text)
    consumer_secret: Mapped[str] = mapped_column(Text)
    access_token: Mapped[str] = mapped_column(Text)
    access_token_secret: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_refreshed_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 4: Implement `src/trip_tracker/models/tripit_sync_state.py`**

```python
"""TripItSyncState: singleton row holding the pull cursor.

Updated by the sync cron (Phase 13) after each successful incremental pull.
`last_modified_since` is a Unix timestamp passed to TripIt's
GET /v1/list/trip/modified_since/<unix-ts>. `last_full_reconcile_at` tracks
the daily reconcile that catches deletions (which the incremental endpoint
does not surface).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trip_tracker.db import Base

SYNC_STATE_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000003")


class TripItSyncState(Base):
    __tablename__ = "tripit_sync_state"
    __table_args__ = (
        CheckConstraint(
            f"id = '{SYNC_STATE_ID}'",
            name="ck_tripit_sync_state_singleton",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=lambda: SYNC_STATE_ID
    )
    # Unix timestamp; nullable while the cursor has never advanced
    last_modified_since: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_pull_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_full_reconcile_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 5: Implement `src/trip_tracker/models/tripit_notification_log.py`**

```python
"""TripItNotificationLog: inbound webhook audit trail.

Every POST to /api/tripit/notification (Phase 13) appends a row here. The
webhook body is parked in raw_payload; processed_at + error track whether
the post-receipt sync job ran cleanly. Pruned daily after 30 days by a
saq cron.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from trip_tracker.db import Base


class TripItNotificationLog(Base):
    __tablename__ = "tripit_notification_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    received_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), index=True
    )
    raw_payload: Mapped[dict] = mapped_column(JSONB)
    processed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 6: Re-export + run tests**

In `src/trip_tracker/models/__init__.py`:

```python
from trip_tracker.models.tripit_notification_log import TripItNotificationLog
from trip_tracker.models.tripit_oauth_credentials import (
    OAUTH_CREDENTIALS_ID,
    TripItOAuthCredentials,
)
from trip_tracker.models.tripit_sync_state import SYNC_STATE_ID, TripItSyncState
```

```bash
uv run pytest tests/test_models_tripit_oauth_credentials.py tests/test_models_tripit_sync_state.py tests/test_models_tripit_notification_log.py -v
```

- [ ] **Step 7: mypy + commit**

```bash
uv run mypy src/trip_tracker/models/
git add src/trip_tracker/models/tripit_oauth_credentials.py \
        src/trip_tracker/models/tripit_sync_state.py \
        src/trip_tracker/models/tripit_notification_log.py \
        src/trip_tracker/models/__init__.py \
        tests/test_models_tripit_oauth_credentials.py \
        tests/test_models_tripit_sync_state.py \
        tests/test_models_tripit_notification_log.py
git commit -m "feat(phase12): add TripIt operational tables — oauth credentials + sync state + notification log"
```

---

### Task 4: AttachAudit model

**Files:**
- Create: `src/trip_tracker/models/attach_audit.py`
- Modify: `src/trip_tracker/models/__init__.py`
- Test: `tests/test_models_attach_audit.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_models_attach_audit.py`:

```python
"""Phase 12: AttachAudit — 10-minute undo window + forensic record for pushes."""
from __future__ import annotations


def test_attach_audit_has_required_columns():
    from trip_tracker.models.attach_audit import AttachAudit
    cols = {c.name for c in AttachAudit.__table__.columns}
    expected = {
        "id", "tripit_trip_id", "pushed_segment_ids", "pushed_at",
        "source_kind", "source_id", "undone_at",
    }
    assert expected <= cols


def test_attach_audit_pushed_at_indexed():
    """The 'is this still undoable' query filters by pushed_at >= now()-10min."""
    from trip_tracker.models.attach_audit import AttachAudit
    has_pushed_at_index = any(
        "pushed_at" in [c.name for c in ix.columns]
        for ix in AttachAudit.__table__.indexes
    )
    assert has_pushed_at_index


def test_attach_audit_source_kind_check_constraint():
    """source_kind must be one of: email / text / document."""
    from trip_tracker.models.attach_audit import AttachAudit
    constraints = [c for c in AttachAudit.__table__.constraints]
    has_source_kind_check = any(
        getattr(c, "name", None) == "ck_attach_audit_source_kind"
        for c in constraints
    )
    assert has_source_kind_check
```

- [ ] **Step 2: Run, expect failure**

```bash
uv run pytest tests/test_models_attach_audit.py -v
```

- [ ] **Step 3: Implement `src/trip_tracker/models/attach_audit.py`**

```python
"""AttachAudit: tracks every successful push to TripIt, both for the 10-min
undo window and as a forensic record after.

A row is inserted by `attach_decider` (Phase 14a) whenever it pushes parsed
segments to TripIt — auto-attach, auto-new-trip, force-new, and inbox-confirm
all write here. Within `pushed_at + 10min AND undone_at IS NULL`, the row
is actionable for undo (Phase 14b). After that, it remains for forensic
value.

source_kind + source_id form a polymorphic FK back to whichever raw_*
table the parse came from.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from trip_tracker.db import Base


class AttachAudit(Base):
    __tablename__ = "attach_audit"
    __table_args__ = (
        CheckConstraint(
            "source_kind IN ('email', 'text', 'document')",
            name="ck_attach_audit_source_kind",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tripit_trip_id: Mapped[str] = mapped_column(String(64), index=True)
    # JSONB list of strings — TripIt segment ids that were pushed in this attach
    pushed_segment_ids: Mapped[list] = mapped_column(JSONB)
    pushed_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), index=True
    )
    source_kind: Mapped[str] = mapped_column(String(16))
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    undone_at: Mapped[datetime | None] = mapped_column(nullable=True)
```

- [ ] **Step 4: Re-export + run tests**

```python
# in src/trip_tracker/models/__init__.py
from trip_tracker.models.attach_audit import AttachAudit
```

```bash
uv run pytest tests/test_models_attach_audit.py -v
```

- [ ] **Step 5: mypy + commit**

```bash
uv run mypy src/trip_tracker/models/
git add src/trip_tracker/models/attach_audit.py \
        src/trip_tracker/models/__init__.py \
        tests/test_models_attach_audit.py
git commit -m "feat(phase12): add AttachAudit model for 10-min undo + forensic record"
```

---

### Task 5: Single Alembic migration for all Phase 12 schema additions

**Files:**
- Create: `migrations/versions/2026_05_06_NNNN_<id>_phase12_tripit_schema.py`

- [ ] **Step 1: Generate migration scaffold**

```bash
cd /Users/owine/Git/trip-tracker
uv run alembic revision -m "phase12_tripit_schema"
```

Note the generated filename + revision id. The `down_revision` should auto-populate to `f175b03585e7` (Phase 11). Verify.

- [ ] **Step 2: Edit the generated file**

Replace the auto-generated body with explicit ops. Template:

```python
"""phase12_tripit_schema

Revision ID: <auto>
Revises: f175b03585e7
Create Date: 2026-05-06 ...

Adds TripIt cache columns to trips + segments and creates six new tables:
raw_text, raw_documents, tripit_oauth_credentials, tripit_sync_state,
tripit_notification_log, attach_audit. Pure additive — no existing data
is touched.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "<auto>"
down_revision: str | None = "f175b03585e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # 1. Append TripIt cache columns to trips                             #
    # ------------------------------------------------------------------ #
    with op.batch_alter_table("trips") as batch:
        batch.add_column(sa.Column("tripit_trip_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("tripit_synced_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("tripit_etag", sa.String(255), nullable=True))
        batch.add_column(sa.Column("upstream_deleted_at", sa.DateTime(timezone=True), nullable=True))
        # Unique constraint is what `unique=True` on the ORM column emits;
        # PostgreSQL backs it with a unique index automatically.
        batch.create_unique_constraint("uq_trips_tripit_trip_id", ["tripit_trip_id"])

    # ------------------------------------------------------------------ #
    # 2. Append TripIt cache columns to segments                          #
    # ------------------------------------------------------------------ #
    with op.batch_alter_table("segments") as batch:
        batch.add_column(sa.Column("tripit_segment_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("tripit_segment_type", sa.String(32), nullable=True))
        batch.add_column(sa.Column("tripit_synced_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_unique_constraint("uq_segments_tripit_segment_id", ["tripit_segment_id"])

    # ------------------------------------------------------------------ #
    # 3. raw_text                                                         #
    # ------------------------------------------------------------------ #
    op.create_table(
        "raw_text",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("hint", sa.String(500), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("parser_audit", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("candidates", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
    )

    # ------------------------------------------------------------------ #
    # 4. raw_documents                                                    #
    # ------------------------------------------------------------------ #
    op.create_table(
        "raw_documents",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("filename", sa.String(500), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_path", sa.String(500), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("parser_audit", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("candidates", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("attach_only", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_unique_constraint("uq_raw_documents_sha256", "raw_documents", ["sha256"])

    # ------------------------------------------------------------------ #
    # 5. tripit_oauth_credentials (singleton via CHECK constraint)        #
    # ------------------------------------------------------------------ #
    op.create_table(
        "tripit_oauth_credentials",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("consumer_key", sa.Text(), nullable=False),
        sa.Column("consumer_secret", sa.Text(), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("access_token_secret", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "id = '00000000-0000-0000-0000-000000000002'",
            name="ck_tripit_oauth_credentials_singleton",
        ),
    )

    # ------------------------------------------------------------------ #
    # 6. tripit_sync_state (singleton via CHECK constraint)               #
    # ------------------------------------------------------------------ #
    op.create_table(
        "tripit_sync_state",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("last_modified_since", sa.BigInteger(), nullable=True),
        sa.Column("last_pull_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_full_reconcile_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "id = '00000000-0000-0000-0000-000000000003'",
            name="ck_tripit_sync_state_singleton",
        ),
    )

    # ------------------------------------------------------------------ #
    # 7. tripit_notification_log                                          #
    # ------------------------------------------------------------------ #
    op.create_table(
        "tripit_notification_log",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("raw_payload", JSONB, nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_tripit_notification_log_received_at",
        "tripit_notification_log",
        ["received_at"],
    )

    # ------------------------------------------------------------------ #
    # 8. attach_audit                                                     #
    # ------------------------------------------------------------------ #
    op.create_table(
        "attach_audit",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tripit_trip_id", sa.String(64), nullable=False),
        sa.Column("pushed_segment_ids", JSONB, nullable=False),
        sa.Column("pushed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("source_id", UUID(as_uuid=True), nullable=False),
        sa.Column("undone_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "source_kind IN ('email', 'text', 'document')",
            name="ck_attach_audit_source_kind",
        ),
    )
    op.create_index("ix_attach_audit_pushed_at", "attach_audit", ["pushed_at"])
    op.create_index("ix_attach_audit_tripit_trip_id", "attach_audit", ["tripit_trip_id"])


def downgrade() -> None:
    # Pure-additive migration — downgrade is supported (drop everything we created)
    op.drop_index("ix_attach_audit_tripit_trip_id", table_name="attach_audit")
    op.drop_index("ix_attach_audit_pushed_at", table_name="attach_audit")
    op.drop_table("attach_audit")

    op.drop_index("ix_tripit_notification_log_received_at", table_name="tripit_notification_log")
    op.drop_table("tripit_notification_log")

    op.drop_table("tripit_sync_state")
    op.drop_table("tripit_oauth_credentials")

    op.drop_constraint("uq_raw_documents_sha256", "raw_documents", type_="unique")
    op.drop_table("raw_documents")

    op.drop_table("raw_text")

    with op.batch_alter_table("segments") as batch:
        batch.drop_constraint("uq_segments_tripit_segment_id", type_="unique")
        batch.drop_column("tripit_synced_at")
        batch.drop_column("tripit_segment_type")
        batch.drop_column("tripit_segment_id")

    with op.batch_alter_table("trips") as batch:
        batch.drop_constraint("uq_trips_tripit_trip_id", type_="unique")
        batch.drop_column("upstream_deleted_at")
        batch.drop_column("tripit_etag")
        batch.drop_column("tripit_synced_at")
        batch.drop_column("tripit_trip_id")
```

- [ ] **Step 3: Apply migration to dev DB + verify**

```bash
# Backup first
docker compose exec postgres pg_dump -U trip_tracker trip_tracker > /tmp/pre-phase12.sql

# Apply
uv run alembic upgrade head

# Verify trips columns
docker compose exec postgres psql -U trip_tracker -d trip_tracker -c "\d trips" | grep -E "tripit_|upstream_"
# Verify segments columns
docker compose exec postgres psql -U trip_tracker -d trip_tracker -c "\d segments" | grep tripit_
# Verify each new table
for t in raw_text raw_documents tripit_oauth_credentials tripit_sync_state tripit_notification_log attach_audit; do
  docker compose exec postgres psql -U trip_tracker -d trip_tracker -c "\d $t"
done
```

Expected: all four trip cache columns present; all three segment cache columns present; all six tables exist with correct constraints.

- [ ] **Step 4: Verify singleton CHECK constraints actually enforce**

```bash
docker compose exec postgres psql -U trip_tracker -d trip_tracker << 'EOF'
-- Should fail with check constraint violation
INSERT INTO tripit_oauth_credentials (id, consumer_key, consumer_secret, access_token, access_token_secret)
VALUES (gen_random_uuid(), 'k', 's', 't', 'ts');

-- Should succeed (the well-known id)
INSERT INTO tripit_oauth_credentials (id, consumer_key, consumer_secret, access_token, access_token_secret)
VALUES ('00000000-0000-0000-0000-000000000002', 'k', 's', 't', 'ts');

-- Cleanup so the test doesn't leave state
DELETE FROM tripit_oauth_credentials WHERE id = '00000000-0000-0000-0000-000000000002';
EOF
```

Expected: first INSERT fails with `new row for relation "tripit_oauth_credentials" violates check constraint "ck_tripit_oauth_credentials_singleton"`. Second INSERT succeeds.

- [ ] **Step 5: Test downgrade then re-upgrade (proves reversibility)**

```bash
uv run alembic downgrade -1
docker compose exec postgres psql -U trip_tracker -d trip_tracker -c "\d trips" | grep tripit_
# Expected: no output — columns gone
uv run alembic upgrade head
docker compose exec postgres psql -U trip_tracker -d trip_tracker -c "\d trips" | grep tripit_
# Expected: 4 columns back
```

- [ ] **Step 6: From-clean re-test (matches Phase 11 pattern)**

```bash
docker compose exec postgres psql -U postgres -c "DROP DATABASE trip_tracker"
docker compose exec postgres psql -U postgres -c "CREATE DATABASE trip_tracker OWNER trip_tracker"
docker compose exec -T postgres psql -U trip_tracker -d trip_tracker < /tmp/pre-phase12.sql
uv run alembic upgrade head
```

If `DROP DATABASE` fails because of active connections:
```bash
docker compose exec postgres psql -U postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='trip_tracker' AND pid <> pg_backend_pid()"
```

Should succeed cleanly.

- [ ] **Step 7: Run full test suite**

```bash
uv run pytest 2>&1 | tail -10
```

Expect all-green (Phase 11 baseline 532 + 17 new model tests from Tasks 1-4 = ~549 tests).

- [ ] **Step 8: Commit**

```bash
git add migrations/versions/
git commit -m "feat(phase12): alembic migration — add TripIt cache columns + 6 new tables"
```

---

### Task 6: Smoke + push v2

**Files:** none modified; verifies end-to-end.

- [ ] **Step 1: docker-compose smoke**

```bash
docker compose down
docker compose up -d --build
sleep 5
docker compose logs app | tail -30
```

App should boot cleanly. Alembic auto-upgrade should apply Phase 12 if it didn't run earlier.

- [ ] **Step 2: Verify auth still works post-Phase-12**

```bash
TOKEN=$(grep '^OWNER_SESSION_TOKEN=' .env | cut -d= -f2)
curl -i "http://localhost:8000/auth/bootstrap?token=$TOKEN" | head -10
```

Expected: 302 + Set-Cookie.

- [ ] **Step 3: Push v2**

```bash
git push origin v2
```

The draft PR #30 will auto-update with the new commits.

- [ ] **Step 4: No new commit for this task** (it's verification only)

---

## Phase 12 success criteria

- [ ] All tests green (`uv run pytest`)
- [ ] mypy clean (`uv run mypy src/`)
- [ ] All 4 new Trip columns present + unique index on `tripit_trip_id`
- [ ] All 3 new Segment columns present + unique index on `tripit_segment_id`
- [ ] All 6 new tables exist with correct constraints
- [ ] CHECK constraints on `tripit_oauth_credentials` + `tripit_sync_state` actually reject non-singleton inserts
- [ ] CHECK constraint on `attach_audit.source_kind` rejects unknown values
- [ ] Migration is reversible (downgrade then upgrade succeeds)
- [ ] App boots cleanly via docker compose
- [ ] v2 pushed; draft PR #30 reflects the Phase 12 commits

---

## What's next: Phase 10 + Phase 13+

Once Phase 12 lands and TripIt support has issued credentials, Phase 10 (TripIt API client + spike + OAuth bootstrap CLI) becomes unblocked. Phase 13 (sync + viewer rewire) follows; Phase 14a/14b/15 build on that.

Phase 10 plan to be written when TripIt creds arrive — writing it now would be against assumed API behavior the spike is supposed to validate.
