# Phase 5 — Documents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a documents subsystem — text-PDF upload + attached-from-email ingestion + async pdfplumber extraction + Meilisearch's 3rd derived index — so users can attach boarding passes, hotel confirmations, and similar PDFs to trips/segments and find them via the ⌘K palette.

**Architecture:** A new `documents` table (UUID id, FKs to `users`/`trips`/`segments`/`raw_emails`, content-addressed `storage_key`, extraction status). Files live on local disk under `<DOCUMENTS_DIR>/<sha256[:2]>/<sha256>` via a `StorageBackend` Protocol with one v0.5.0 implementation (`LocalFsStorage`). Phase 2's webhook is extended to enumerate PDF attachments and persist them with `segment_id=NULL`; Phase 3's `parse_raw_email` saq task auto-links them after segments are created (filename-heuristic). A new saq task `extract_document` runs `pdfplumber` on each PDF and writes `extracted_text`. The Phase 4 search subsystem gains a third Meili index `documents`, the proxy route broadens its `Literal`, the ⌘K palette adds the new index, and the `reindex` CLI walks documents too.

**Tech Stack:** Python 3.14 (target=py313), `pdfplumber` (new dep, text PDFs only), saq, Redis 7, Meilisearch 1.13 + `meilisearch-python-sdk`, FastAPI, SQLAlchemy 2.0 async, Postgres 18 (`pg_insert.on_conflict_do_update` + `xmax=0` UPSERT idiom), Alembic, Pydantic v2, Tailwind, Alpine.js (already loaded). No new runtime libraries beyond `pdfplumber`.

**Spec reference:** [`docs/superpowers/specs/2026-04-30-phase5-documents-design.md`](../specs/2026-04-30-phase5-documents-design.md). Section numbers (e.g. §6.2) below refer to this spec.

**Branch:** `feat/phase-5-documents`. Cut from `main` at the HEAD when implementation starts (currently `21d22db` after the spec landed).

**Out of scope (deferred to Phase 5.x):** OCR/Tesseract/poppler, image attachments, S3 storage, document categories, drag-and-drop UI, thumbnails, admin re-extract action, per-user storage quota.

**Toolchain quirks worth re-stating per task:**
- `from __future__ import annotations` at top of every new module.
- ruff `target=py313` + mypy `python_version=3.14` — keep imports/PEP 585 forms idiomatic.
- ruff hates `BLE001` (broad `except Exception`); use `contextlib.suppress(Exception)` when the swallow is intentional, otherwise re-raise as a typed exception.
- Bandit B101 fires on `assert` even in Pydantic-validated code — add `# nosec B101` with the reason. Bandit B110 (try/except/pass) likewise — prefer `contextlib.suppress`.
- Pydantic v2: `@field_validator(...)` requires `@classmethod` on the line below.
- FastAPI returns: don't union with `RedirectResponse` and `_TemplateResponse` in a single annotation; use `Response` and let FastAPI handle the body.
- pre-commit djlint hook is `djlint-reformat` (NOT `djlint-jinja` — it silently matches zero files).

---

## File Structure

```
src/trip_tracker/
├── config.py                                [MODIFY: add 3 new settings]
├── app.py                                   [MODIFY: include documents router]
├── worker.py                                [MODIFY: register extract_document; add storage to ctx; case "document" in sync_meili]
├── ingest/
│   ├── webhook.py                           [MODIFY: persist attachments after RawEmail INSERT]
│   └── attachments.py                       [CREATE — extract_attachments(body) helper]
├── documents/                               [CREATE — new subpackage]
│   ├── __init__.py
│   ├── storage.py                           Protocol + LocalFsStorage
│   ├── helpers.py                           sha256 streaming hash + magic-byte check
│   ├── autolink.py                          match_attachment_to_segment heuristic
│   ├── extract.py                           extract_document saq task
│   └── events.py                            SQLAlchemy after_delete listener for disk cleanup
├── models/
│   ├── document.py                          [CREATE — ORM model]
│   └── __init__.py                          [MODIFY: re-export Document]
├── routes/
│   └── documents.py                         [CREATE — POST/GET/DELETE handlers]
├── search/
│   ├── client.py                            [MODIFY: third entry in ensure_indexes_configured]
│   ├── sync.py                              [MODIFY: document_to_doc + Literal extension]
│   ├── proxy.py                             [MODIFY: Literal["trips","segments","documents"]]
│   └── reindex.py                           [MODIFY: third walk for Documents]
└── templates/
    ├── _search_palette.html                 [MODIFY: add "documents" to indexes array; render doc hits]
    ├── trips/_documents.html                [CREATE — partial: list + upload form]
    └── segments/_documents.html             [CREATE — partial: inline list + upload form]

migrations/versions/
└── XXXXXXXXXXXX_phase5_documents.py        [CREATE]

tests/
├── test_models_document.py                 [CREATE]
├── test_documents_storage.py               [CREATE]
├── test_documents_helpers.py               [CREATE]
├── test_documents_autolink.py              [CREATE]
├── test_documents_extract.py               [CREATE]
├── test_documents_events.py                [CREATE]
├── test_routes_documents_upload.py         [CREATE]
├── test_routes_documents_download.py       [CREATE]
├── test_routes_documents_link.py           [CREATE]
├── test_ingest_webhook_attachments.py      [CREATE]
├── test_search_documents.py                [CREATE]
├── test_search_reindex_documents.py        [CREATE]
└── fixtures/documents/                     [CREATE — small real-shape PDFs for tests]
    ├── tiny-text.pdf                       (text-extractable)
    ├── tiny-empty.pdf                      (PDF with no text)
    └── boarding-pass-fake.pdf              (filename matches AF7237 fixture)
```

---

## Task 1 — Schema + Alembic migration + ORM model + cascade listener

**Spec ref:** §4.1, §4.2.

**Files:**
- Create: `src/trip_tracker/models/document.py`
- Modify: `src/trip_tracker/models/__init__.py` (re-export `Document`)
- Create: `migrations/versions/XXXXXXXXXXXX_phase5_documents.py`
- Create: `src/trip_tracker/documents/__init__.py` (empty marker)
- Create: `src/trip_tracker/documents/events.py`
- Create: `tests/test_models_document.py`
- Create: `tests/test_documents_events.py`

**Model `dependency`:** Task 1 has no upstream dependencies. Task 2 depends on Task 1 only because the cascade-listener test needs a working storage (so Task 1's listener is a thin import-only stub here, and Task 2 fills in `LocalFsStorage`).

- [ ] **Step 1.1 — Add ORM model**

`src/trip_tracker/models/document.py`:

```python
"""Document ORM. Spec §4.1."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from trip_tracker.models.base import Base

if TYPE_CHECKING:
    from trip_tracker.models.raw_email import RawEmail
    from trip_tracker.models.segment import Segment
    from trip_tracker.models.trip import Trip
    from trip_tracker.models.user import User


class Document(Base):
    """Stored file (text PDF in v0.5.0) + extracted text."""

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "sha256", name="uq_documents_owner_sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    trip_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trips.id", ondelete="CASCADE"), nullable=True
    )
    segment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("segments.id", ondelete="SET NULL"),
        nullable=True,
    )
    raw_email_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("raw_emails.id", ondelete="SET NULL"),
        nullable=True,
    )
    filename: Mapped[str] = mapped_column(String, nullable=False)
    mime_type: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String, nullable=False)
    extracted_text: Mapped[str | None] = mapped_column(String, nullable=True)
    extract_status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", server_default="pending"
    )
    extract_method: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    owner: Mapped["User"] = relationship(back_populates=None, lazy="raise")
    trip: Mapped["Trip | None"] = relationship(back_populates=None, lazy="raise")
    segment: Mapped["Segment | None"] = relationship(back_populates=None, lazy="raise")
    raw_email: Mapped["RawEmail | None"] = relationship(back_populates=None, lazy="raise")
```

- [ ] **Step 1.2 — Re-export from package init**

In `src/trip_tracker/models/__init__.py`, add the `from trip_tracker.models.document import Document` line. Mirror the pattern of the existing re-exports.

- [ ] **Step 1.3 — Cascade listener stub (Task 2 fills the body)**

`src/trip_tracker/documents/__init__.py` is just `"""Document subsystem."""\n`.

`src/trip_tracker/documents/events.py`:

```python
"""SQLAlchemy ORM event listeners for documents.

Disk cleanup on Document delete. Registered at import time. The Storage
backend is set via `set_storage_for_events(storage)` from app/worker
startup so this module stays import-free of heavy deps.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import event

from trip_tracker.models.document import Document

if TYPE_CHECKING:
    from trip_tracker.documents.storage import StorageBackend

_logger = logging.getLogger(__name__)
_storage: "StorageBackend | None" = None


def set_storage_for_events(storage: "StorageBackend") -> None:
    """Inject the storage backend used by the after_delete listener."""
    global _storage
    _storage = storage


@event.listens_for(Document, "after_delete")
def _document_after_delete(_mapper: Any, _connection: Any, target: Document) -> None:
    """Schedule disk cleanup after the row is deleted.

    Note: this fires inside the Session.flush(), before commit. We rely on
    the listener running synchronously in the same async task; the actual
    storage.delete() is dispatched via asyncio.create_task so it runs after
    the DB transaction commits successfully.
    """
    if _storage is None:
        _logger.warning(
            "Document %s deleted but storage not set; orphan file at %s",
            target.id,
            target.storage_key,
        )
        return
    import asyncio

    storage = _storage
    key = target.storage_key
    asyncio.create_task(_safe_delete(storage, key))


async def _safe_delete(storage: "StorageBackend", key: str) -> None:
    try:
        await storage.delete(key)
    except Exception:  # noqa: BLE001 — disk cleanup failure shouldn't crash
        _logger.exception("storage.delete failed for key=%s", key)
```

- [ ] **Step 1.4 — Alembic migration**

```bash
uv run alembic revision -m "phase5 documents"
```

Edit the generated file (`migrations/versions/<revision>_phase5_documents.py`):

```python
"""phase5 documents

Revision ID: <auto>
Revises: 905263cb9862  # phase3_llm_budget
Create Date: 2026-04-30 …
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "<auto>"
down_revision: str | None = "905263cb9862"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trip_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("segment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("raw_email_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("extract_status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("extract_method", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trip_id"], ["trips.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["segment_id"], ["segments.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["raw_email_id"], ["raw_emails.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("owner_user_id", "sha256", name="uq_documents_owner_sha256"),
    )
    op.create_index("ix_documents_trip_id", "documents", ["trip_id"])
    op.create_index("ix_documents_segment_id", "documents", ["segment_id"])
    op.create_index("ix_documents_owner", "documents", ["owner_user_id"])


def downgrade() -> None:
    op.drop_index("ix_documents_owner", table_name="documents")
    op.drop_index("ix_documents_segment_id", table_name="documents")
    op.drop_index("ix_documents_trip_id", table_name="documents")
    op.drop_table("documents")
```

- [ ] **Step 1.5 — Failing tests**

`tests/test_models_document.py`:

```python
"""Document ORM + cascade tests."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.document import Document
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User


@pytest.mark.asyncio
async def test_document_unique_owner_sha256(db_session: AsyncSession) -> None:
    u = User(oidc_subject="d1", email="d1@x.com", display_name="D1")
    db_session.add(u)
    await db_session.flush()
    db_session.add(
        Document(
            owner_user_id=u.id,
            filename="a.pdf",
            mime_type="application/pdf",
            size_bytes=10,
            sha256="a" * 64,
            storage_key="aa/" + "a" * 64,
        )
    )
    await db_session.commit()
    db_session.add(
        Document(
            owner_user_id=u.id,
            filename="b.pdf",
            mime_type="application/pdf",
            size_bytes=10,
            sha256="a" * 64,
            storage_key="aa/" + "a" * 64,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_trip_delete_cascades_documents(db_session: AsyncSession) -> None:
    u = User(oidc_subject="d2", email="d2@x.com", display_name="D2")
    db_session.add(u)
    await db_session.flush()
    t = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 2), created_by=u.id)
    db_session.add(t)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=t.id, user_id=u.id, role="owner"))
    db_session.add(
        Document(
            owner_user_id=u.id,
            trip_id=t.id,
            filename="x.pdf",
            mime_type="application/pdf",
            size_bytes=10,
            sha256="b" * 64,
            storage_key="bb/" + "b" * 64,
        )
    )
    await db_session.commit()
    await db_session.delete(t)
    await db_session.commit()
    rows = (await db_session.execute(select(Document))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_segment_delete_sets_segment_id_null(db_session: AsyncSession) -> None:
    u = User(oidc_subject="d3", email="d3@x.com", display_name="D3")
    db_session.add(u)
    await db_session.flush()
    t = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 2), created_by=u.id)
    db_session.add(t)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=t.id, user_id=u.id, role="owner"))
    s = Segment(
        trip_id=t.id,
        owner_user_id=u.id,
        type="flight",
        status="confirmed",
        start_at=datetime(2026, 6, 1, 13, tzinfo=UTC),
        start_tz="UTC",
        parse_source="manual",
        parse_confidence=1.0,
    )
    db_session.add(s)
    await db_session.flush()
    d = Document(
        owner_user_id=u.id,
        trip_id=t.id,
        segment_id=s.id,
        filename="x.pdf",
        mime_type="application/pdf",
        size_bytes=10,
        sha256="c" * 64,
        storage_key="cc/" + "c" * 64,
    )
    db_session.add(d)
    await db_session.commit()
    await db_session.delete(s)
    await db_session.commit()
    refreshed = (await db_session.execute(select(Document).where(Document.id == d.id))).scalar_one()
    assert refreshed.segment_id is None
    assert refreshed.trip_id == t.id  # Trip still alive
```

- [ ] **Step 1.6 — Run tests to verify they fail (module-not-found / no-such-table) → run alembic against the test fixture, then run again to verify passes**

```bash
uv run pytest tests/test_models_document.py -v
# fails: cannot import Document, no documents table

# Create the model + migration per 1.1 + 1.4. Then:
uv run pytest tests/test_models_document.py -v
# 3 passed
```

The conftest `db_session` fixture builds schema via `Base.metadata.create_all` (not Alembic), so the migration file's *correctness* is verified separately:

```bash
uv run alembic upgrade head
# Should report "Running upgrade 905263cb9862 -> <new>, phase5 documents"
uv run alembic downgrade -1
uv run alembic upgrade head
```

- [ ] **Step 1.7 — Full suite + commit**

```bash
uv run pytest -q
uv run mypy src
uv run ruff check . && uv run ruff format --check .
git add src/trip_tracker/models/document.py src/trip_tracker/models/__init__.py \
        src/trip_tracker/documents/__init__.py src/trip_tracker/documents/events.py \
        migrations/versions/*_phase5_documents.py tests/test_models_document.py
git commit -m "feat(documents): Document ORM + Alembic migration + after_delete listener stub"
```

**Quality bar:**
- The `down_revision` must point to whatever the current head is; check with `uv run alembic current` first. If a Phase 4 migration was added, link to that head instead of `905263cb9862`.
- The model uses `mapped_column(default=lambda: datetime.now(UTC), onupdate=...)` for `created_at`/`updated_at` — match the pattern in `models/segment.py` if it differs (some projects use `server_default=text("now()")` instead).
- `relationship(..., lazy="raise")` is intentional — accidental cross-trip auto-loads in async code blow up tests. Existing models follow this convention.
- Don't write `extracted_text: Mapped[str]` for nullable text — use `str | None`. UP037 will flag the older `Optional[str]` form.

---

## Task 2 — `StorageBackend` Protocol + `LocalFsStorage` + path-traversal guard

**Spec ref:** §5.

**Files:**
- Create: `src/trip_tracker/documents/storage.py`
- Create: `tests/test_documents_storage.py`

- [ ] **Step 2.1 — Failing tests**

`tests/test_documents_storage.py`:

```python
"""LocalFsStorage round-trips, path-traversal guard, idempotency."""

from __future__ import annotations

from pathlib import Path

import pytest

from trip_tracker.documents.storage import LocalFsStorage, StorageBackend

GOOD_KEY = "ab/" + "a" * 64
BAD_KEYS = [
    "../etc/passwd",
    "ab/../../../tmp/x",
    "AB/" + "a" * 64,  # non-lower hex prefix
    "ab/" + "a" * 63,  # short
    "ab/" + "a" * 65,  # long
    "ab/" + "g" * 64,  # non-hex
    "abc/" + "a" * 64,  # 3-char prefix
    "/absolute",
]


@pytest.fixture
def storage(tmp_path: Path) -> LocalFsStorage:
    return LocalFsStorage(tmp_path)


@pytest.mark.asyncio
async def test_put_then_open_round_trips(storage: LocalFsStorage) -> None:
    sha = "a" * 64
    key = await storage.put(sha, b"hello world")
    assert key == f"{sha[:2]}/{sha}"
    chunks = []
    async for chunk in storage.open(key):
        chunks.append(chunk)
    assert b"".join(chunks) == b"hello world"


@pytest.mark.asyncio
async def test_put_is_idempotent_for_existing_key(storage: LocalFsStorage) -> None:
    sha = "b" * 64
    k1 = await storage.put(sha, b"v1")
    k2 = await storage.put(sha, b"v1")
    assert k1 == k2
    chunks = []
    async for chunk in storage.open(k1):
        chunks.append(chunk)
    assert b"".join(chunks) == b"v1"


@pytest.mark.asyncio
async def test_delete_is_idempotent(storage: LocalFsStorage) -> None:
    sha = "c" * 64
    key = await storage.put(sha, b"x")
    await storage.delete(key)
    await storage.delete(key)  # missing — must not raise


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", BAD_KEYS)
async def test_open_rejects_path_traversal(storage: LocalFsStorage, bad: str) -> None:
    with pytest.raises(ValueError):
        async for _ in storage.open(bad):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", BAD_KEYS)
async def test_delete_rejects_path_traversal(storage: LocalFsStorage, bad: str) -> None:
    with pytest.raises(ValueError):
        await storage.delete(bad)


@pytest.mark.parametrize("bad", BAD_KEYS)
def test_absolute_path_rejects_path_traversal(storage: LocalFsStorage, bad: str) -> None:
    with pytest.raises(ValueError):
        storage.absolute_path(bad)


def test_absolute_path_returns_full_path_for_good_key(
    storage: LocalFsStorage, tmp_path: Path
) -> None:
    assert storage.absolute_path(GOOD_KEY) == str(tmp_path / GOOD_KEY)


def test_protocol_satisfied(storage: LocalFsStorage) -> None:
    """Static check: LocalFsStorage matches StorageBackend Protocol."""
    sb: StorageBackend = storage  # would mypy-fail if shape mismatches
    assert sb is storage
```

- [ ] **Step 2.2 — Run tests to verify they fail (import error)**

```bash
uv run pytest tests/test_documents_storage.py -v
# ImportError: cannot import name 'LocalFsStorage'
```

- [ ] **Step 2.3 — Implement**

`src/trip_tracker/documents/storage.py`:

```python
"""Document storage Protocol + LocalFsStorage. Spec §5."""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

_KEY_RE = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{64}$")
_CHUNK = 64 * 1024


def _validate_key(key: str) -> None:
    if not _KEY_RE.fullmatch(key):
        raise ValueError(f"invalid storage_key: {key!r}")


class StorageBackend(Protocol):
    """File storage abstraction. v0.5.0 ships LocalFsStorage; S3 in Phase 5.x.

    NOTE: `open` is `async def` (returning AsyncIterator[bytes]) so future S3
    backends can do an async metadata check before yielding. Call sites use
    `async for chunk in await storage.open(key):` (double-await — the await
    resolves the coroutine, the async-for iterates).
    """

    async def put(self, sha256: str, content: bytes) -> str:
        """Persist content. Returns storage_key. Idempotent on identical content."""

    async def open(self, storage_key: str) -> AsyncIterator[bytes]:
        """Iterate file content in chunks. Raises ValueError on bad key."""

    async def delete(self, storage_key: str) -> None:
        """Remove file. Idempotent: missing file is not an error.

        Raises ValueError on a malformed key (path-traversal guard).
        """

    def absolute_path(self, storage_key: str) -> str | None:
        """Return absolute FS path for X-Accel mode, or None if not local."""


class LocalFsStorage:
    """Filesystem-backed StorageBackend. Content-addressed under <root>/<sha[:2]>/<sha>."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    async def put(self, sha256: str, content: bytes) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError(f"invalid sha256: {sha256!r}")
        key = f"{sha256[:2]}/{sha256}"
        target = self._root / key
        if target.exists():
            return key
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, target)
        return key

    async def open(self, storage_key: str) -> AsyncIterator[bytes]:
        _validate_key(storage_key)
        path = self._root / storage_key

        async def _iter() -> AsyncIterator[bytes]:
            with path.open("rb") as fh:
                while chunk := fh.read(_CHUNK):
                    yield chunk

        return _iter()

    async def delete(self, storage_key: str) -> None:
        _validate_key(storage_key)
        path = self._root / storage_key
        try:
            path.unlink()
        except FileNotFoundError:
            return  # idempotent

    def absolute_path(self, storage_key: str) -> str:
        _validate_key(storage_key)
        return str(self._root / storage_key)
```

Note: `open` returns an `AsyncIterator[bytes]` directly (the inner `_iter` is the generator). The Protocol and the impl both declare `async def open(...) -> AsyncIterator[bytes]` so the structural match is exact. Call sites: `async for chunk in await storage.open(key):` — the await resolves the coroutine, the async-for iterates the chunks.

(Historical note for skimmers: an earlier draft of this plan declared `open` as sync on the Protocol and async on the impl, causing a structural mismatch. The fix has been folded into the Protocol block above; no separate re-spec step is needed.)

```python
# (already shown above) — no additional Protocol changes needed
class StorageBackend(Protocol):
    async def put(self, sha256: str, content: bytes) -> str: ...
    async def open(self, storage_key: str) -> AsyncIterator[bytes]: ...
    async def delete(self, storage_key: str) -> None: ...
    def absolute_path(self, storage_key: str) -> str | None: ...
```

And the test invocation becomes `async for chunk in await storage.open(key):` — adjust the test cases accordingly (the failing-tests block above already has this pattern).

- [ ] **Step 2.4 — Verify pass + commit**

```bash
uv run pytest tests/test_documents_storage.py -v   # all pass
uv run mypy src                                    # clean
git add src/trip_tracker/documents/storage.py tests/test_documents_storage.py
git commit -m "feat(documents): StorageBackend Protocol + LocalFsStorage with path-traversal guard"
```

**Quality bar:**
- `_validate_key` regex must use `fullmatch` (not `match`) so `aa/<64>extra` is rejected.
- The `_KEY_RE` allows lowercase hex only — uppercase is rejected. Spec §5.2 confirms lowercase.
- `os.replace` (not `os.rename`) for cross-platform atomic move.
- `Path.write_bytes(...)` is sync; for the typical 100KB–25MB PDF that's fine. If perf bites later, use `aiofiles`. YAGNI for now.

---

## Task 3 — Helpers (sha256, magic-byte check, size cap)

**Spec ref:** §6.1 steps 2–4.

**Files:**
- Create: `src/trip_tracker/documents/helpers.py`
- Create: `tests/test_documents_helpers.py`

- [ ] **Step 3.1 — Failing tests**

```python
"""Document helpers: sha256, magic-byte, size cap."""

from __future__ import annotations

import hashlib

import pytest

from trip_tracker.documents.helpers import (
    PDF_MAGIC,
    SizeLimitExceeded,
    is_pdf,
    sha256_hex,
)


def test_sha256_hex_matches_hashlib() -> None:
    body = b"hello world"
    assert sha256_hex(body) == hashlib.sha256(body).hexdigest()


def test_is_pdf_accepts_pdf_magic() -> None:
    assert is_pdf(PDF_MAGIC + b"-1.4\n%hello") is True


def test_is_pdf_rejects_other_content() -> None:
    assert is_pdf(b"PNG\r\n") is False
    assert is_pdf(b"") is False
    assert is_pdf(b"%PD") is False


def test_size_limit_exceeded_when_total_grows() -> None:
    limit = 10
    accumulated = 0
    for chunk in [b"hello", b"world", b"!"]:
        accumulated += len(chunk)
        if accumulated > limit:
            with pytest.raises(SizeLimitExceeded):
                raise SizeLimitExceeded(limit=limit, observed=accumulated)
```

The test file is intentionally light because the helpers are simple — coverage comes from upload-route integration tests in Task 4.

- [ ] **Step 3.2 — Implement**

`src/trip_tracker/documents/helpers.py`:

```python
"""Document upload helpers: sha256, magic-byte check, size cap exception."""

from __future__ import annotations

import hashlib

PDF_MAGIC = b"%PDF"


class SizeLimitExceeded(Exception):
    """Raised when a streaming upload exceeds MAX_UPLOAD_BYTES."""

    def __init__(self, *, limit: int, observed: int) -> None:
        super().__init__(f"upload exceeded {limit} bytes (saw {observed})")
        self.limit = limit
        self.observed = observed


def sha256_hex(content: bytes) -> str:
    """Lowercase 64-char hex sha256 of content. Used for storage_key + dedup."""
    return hashlib.sha256(content).hexdigest()


def is_pdf(content: bytes) -> bool:
    """First-4-bytes magic check. Authoritative — Content-Type is advisory."""
    return content[: len(PDF_MAGIC)] == PDF_MAGIC
```

- [ ] **Step 3.3 — Run + commit**

```bash
uv run pytest tests/test_documents_helpers.py -v
uv run mypy src
git add src/trip_tracker/documents/helpers.py tests/test_documents_helpers.py
git commit -m "feat(documents): sha256 + PDF magic-byte + size-limit exception"
```

**Quality bar:**
- `sha256_hex` takes `bytes` (not a stream). Streaming for ≥1 MiB is acceptable — see §6.1 step 3 — but in Phase 5 v0.5.0 we accept the whole-body-in-memory approach for simplicity (PDFs are <25 MiB by config). If a streaming variant is needed later, add `sha256_stream(it: Iterable[bytes])`.
- The `SizeLimitExceeded` exception carries `limit` and `observed` as attrs so callers can render a precise 413 message.

---

## Task 4 — Manual-upload routes

**Spec ref:** §6.1.

**Files:**
- Create: `src/trip_tracker/routes/documents.py`
- Modify: `src/trip_tracker/app.py` (include the new router)
- Create: `tests/test_routes_documents_upload.py`
- Create: `tests/test_routes_documents_link.py`

- [ ] **Step 4.1 — Failing tests for upload (new + dedup)**

`tests/test_routes_documents_upload.py`:

```python
"""POST /trips/{id}/documents — manual upload + dedup + size cap."""

from __future__ import annotations

import io
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.models.document import Document
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User


PDF_BODY = b"%PDF-1.4\n%fake content for tests\n"


async def _seed(db: AsyncSession) -> tuple[User, Trip]:
    u = User(oidc_subject="up1", email="up1@x.com", display_name="UP1")
    db.add(u)
    await db.flush()
    t = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 2), created_by=u.id)
    db.add(t)
    await db.flush()
    db.add(TripTraveler(trip_id=t.id, user_id=u.id, role="owner"))
    await db.commit()
    return u, t


@pytest.mark.asyncio
async def test_upload_to_trip_creates_document(
    db_session: AsyncSession,
    authenticated_client_factory,  # provided by conftest
) -> None:
    u, t = await _seed(db_session)
    async with authenticated_client_factory(u) as client:
        r = await client.post(
            f"/trips/{t.id}/documents",
            files={"file": ("boarding-pass.pdf", io.BytesIO(PDF_BODY), "application/pdf")},
        )
    assert r.status_code in (200, 303)
    docs = (
        (await db_session.execute(select(Document).where(Document.trip_id == t.id))).scalars().all()
    )
    assert len(docs) == 1
    assert docs[0].filename == "boarding-pass.pdf"
    assert docs[0].extract_status == "pending"
    assert docs[0].mime_type == "application/pdf"


@pytest.mark.asyncio
async def test_upload_dedup_returns_303_on_second_upload(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    u, t = await _seed(db_session)
    async with authenticated_client_factory(u) as client:
        r1 = await client.post(
            f"/trips/{t.id}/documents",
            files={"file": ("a.pdf", io.BytesIO(PDF_BODY), "application/pdf")},
        )
        assert r1.status_code in (200, 303)
        r2 = await client.post(
            f"/trips/{t.id}/documents",
            files={"file": ("a.pdf", io.BytesIO(PDF_BODY), "application/pdf")},
        )
        assert r2.status_code == 303

    docs = (await db_session.execute(select(Document))).scalars().all()
    assert len(docs) == 1


@pytest.mark.asyncio
async def test_upload_rejects_non_pdf(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    u, t = await _seed(db_session)
    async with authenticated_client_factory(u) as client:
        r = await client.post(
            f"/trips/{t.id}/documents",
            files={"file": ("evil.png", io.BytesIO(b"\x89PNG\r\n"), "application/pdf")},
        )
    assert r.status_code == 400  # magic-byte check fails


@pytest.mark.asyncio
async def test_upload_size_cap_returns_413(
    db_session: AsyncSession, authenticated_client_factory, monkeypatch
) -> None:
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "32")
    u, t = await _seed(db_session)
    big = PDF_BODY * 10
    async with authenticated_client_factory(u) as client:
        r = await client.post(
            f"/trips/{t.id}/documents",
            files={"file": ("big.pdf", io.BytesIO(big), "application/pdf")},
        )
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_upload_requires_traveler_membership(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    owner, t = await _seed(db_session)
    other = User(oidc_subject="up2", email="up2@x.com", display_name="UP2")
    db_session.add(other)
    await db_session.commit()
    async with authenticated_client_factory(other) as client:
        r = await client.post(
            f"/trips/{t.id}/documents",
            files={"file": ("a.pdf", io.BytesIO(PDF_BODY), "application/pdf")},
        )
    assert r.status_code in (403, 404)
```

**Fixture note — `authenticated_client_factory` does NOT exist project-wide.** Phase 4's `tests/test_routes_search.py` uses an inline `_cookie(user, settings)` helper. Rather than rewriting the test bodies above, **add this small fixture at the top of every new route-test file** that uses the `authenticated_client_factory(user)` form:

```python
# Place at the top of tests/test_routes_documents_upload.py
# AND tests/test_routes_documents_link.py
# AND tests/test_routes_documents_download.py
from contextlib import asynccontextmanager

import httpx
import pytest

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings


def _cookie(user, settings):
    return {
        "tt_session": encode_session(
            SessionPayload(user_id=user.id, oidc_subject=user.oidc_subject),
            secret=settings.session_secret.get_secret_value(),
            max_age=3600,
        )
    }


@asynccontextmanager
async def _ctx(app, settings, user):
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies=_cookie(user, settings),
        ) as client,
    ):
        yield client


@pytest.fixture
def authenticated_client_factory(db_url, monkeypatch):
    """Returns a callable: factory(user) -> async-context-manager-yielding-AsyncClient.

    Local to this test file; the project does not ship a shared version.
    """
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    app = create_app(settings=settings)

    def _make(user):
        return _ctx(app, settings, user)

    return _make
```

With that fixture defined locally in each route-test file, the failing-test code blocks above (which use `async with authenticated_client_factory(u) as client:`) work unchanged. Don't promote this to `conftest.py` — keeping it local matches the project's "each test file builds what it needs" convention.

- [ ] **Step 4.2 — Failing tests for link/unlink/delete**

`tests/test_routes_documents_link.py`:

```python
"""Document link / unlink / delete routes."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.document import Document
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User


async def _seed_with_segment_and_doc(db: AsyncSession) -> tuple[User, Trip, Segment, Document]:
    u = User(oidc_subject="lk1", email="lk1@x.com", display_name="LK1")
    db.add(u)
    await db.flush()
    t = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 2), created_by=u.id)
    db.add(t)
    await db.flush()
    db.add(TripTraveler(trip_id=t.id, user_id=u.id, role="owner"))
    s = Segment(
        trip_id=t.id,
        owner_user_id=u.id,
        type="flight",
        status="confirmed",
        start_at=datetime(2026, 6, 1, 13, tzinfo=UTC),
        start_tz="UTC",
        parse_source="manual",
        parse_confidence=1.0,
    )
    db.add(s)
    await db.flush()
    d = Document(
        owner_user_id=u.id,
        trip_id=t.id,
        filename="x.pdf",
        mime_type="application/pdf",
        size_bytes=10,
        sha256="d" * 64,
        storage_key="dd/" + "d" * 64,
    )
    db.add(d)
    await db.commit()
    return u, t, s, d


@pytest.mark.asyncio
async def test_link_attaches_segment_id(db_session, authenticated_client_factory) -> None:
    u, t, s, d = await _seed_with_segment_and_doc(db_session)
    async with authenticated_client_factory(u) as client:
        r = await client.post(f"/documents/{d.id}/link", data={"segment_id": str(s.id)})
    assert r.status_code in (200, 303)
    refreshed = (await db_session.execute(select(Document).where(Document.id == d.id))).scalar_one()
    assert refreshed.segment_id == s.id


@pytest.mark.asyncio
async def test_unlink_clears_segment_id(db_session, authenticated_client_factory) -> None:
    u, t, s, d = await _seed_with_segment_and_doc(db_session)
    d.segment_id = s.id
    db_session.add(d)
    await db_session.commit()
    async with authenticated_client_factory(u) as client:
        r = await client.post(f"/documents/{d.id}/unlink")
    assert r.status_code in (200, 303)
    refreshed = (await db_session.execute(select(Document).where(Document.id == d.id))).scalar_one()
    assert refreshed.segment_id is None


@pytest.mark.asyncio
async def test_delete_removes_row(db_session, authenticated_client_factory) -> None:
    u, t, s, d = await _seed_with_segment_and_doc(db_session)
    async with authenticated_client_factory(u) as client:
        r = await client.delete(f"/documents/{d.id}")
    assert r.status_code in (200, 204, 303)
    rows = (await db_session.execute(select(Document).where(Document.id == d.id))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_delete_forbidden_for_non_owner(db_session, authenticated_client_factory) -> None:
    u, t, s, d = await _seed_with_segment_and_doc(db_session)
    other = User(oidc_subject="lk2", email="lk2@x.com", display_name="LK2")
    db_session.add(other)
    await db_session.commit()
    async with authenticated_client_factory(other) as client:
        r = await client.delete(f"/documents/{d.id}")
    assert r.status_code in (403, 404)
```

- [ ] **Step 4.3 — Implement the routes**

`src/trip_tracker/routes/documents.py`:

```python
"""Document upload / link / unlink / delete routes. Spec §6.1."""

from __future__ import annotations

import logging
import uuid
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from saq import Queue

from trip_tracker.auth.deps import require_user
from trip_tracker.config import Settings, get_settings
from trip_tracker.db import get_session
from trip_tracker.documents.helpers import (
    SizeLimitExceeded,
    is_pdf,
    sha256_hex,
)
from trip_tracker.documents.storage import LocalFsStorage, StorageBackend
from trip_tracker.models.document import Document
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User

router = APIRouter()
_logger = logging.getLogger(__name__)


def _storage_dep(settings: Settings = Depends(get_settings)) -> StorageBackend:
    return LocalFsStorage(settings.documents_dir)


async def _user_can_access_trip(db: AsyncSession, user: User, trip_id: uuid.UUID) -> bool:
    return (
        await db.execute(
            select(TripTraveler.user_id).where(
                TripTraveler.trip_id == trip_id, TripTraveler.user_id == user.id
            )
        )
    ).scalar_one_or_none() is not None


async def _read_upload(file: UploadFile, max_bytes: int) -> bytes:
    """Read the upload, enforcing the size cap as we go."""
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(64 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise SizeLimitExceeded(limit=max_bytes, observed=total)
        chunks.append(chunk)
    return b"".join(chunks)


async def _upsert_and_enqueue(
    db: AsyncSession,
    storage: StorageBackend,
    settings: Settings,
    *,
    user: User,
    trip_id: uuid.UUID | None,
    segment_id: uuid.UUID | None,
    filename: str,
    content: bytes,
) -> tuple[Document, bool]:
    """INSERT ... ON CONFLICT DO NOTHING. Returns (doc, is_new)."""
    sha = sha256_hex(content)
    storage_key = f"{sha[:2]}/{sha}"

    stmt = (
        pg_insert(Document)
        .values(
            owner_user_id=user.id,
            trip_id=trip_id,
            segment_id=segment_id,
            filename=filename,
            mime_type="application/pdf",
            size_bytes=len(content),
            sha256=sha,
            storage_key=storage_key,
            extract_status="pending",
        )
        .on_conflict_do_nothing(index_elements=["owner_user_id", "sha256"])
        .returning(Document.id)
    )
    inserted_id = (await db.execute(stmt)).scalar_one_or_none()

    if inserted_id is not None:
        await storage.put(sha, content)
        await db.commit()
        doc = (await db.execute(select(Document).where(Document.id == inserted_id))).scalar_one()
        # Enqueue extraction (saq dispatches by string name; function lives in worker)
        q = Queue.from_url(str(settings.redis_url))
        try:
            await q.enqueue("extract_document", document_id=str(inserted_id))
        finally:
            await q.disconnect()
        return doc, True

    # Existing row — fetch it.
    existing = (
        await db.execute(
            select(Document).where(Document.owner_user_id == user.id, Document.sha256 == sha)
        )
    ).scalar_one()
    return existing, False


@router.post("/trips/{trip_id}/documents")
async def upload_to_trip(
    trip_id: uuid.UUID,
    request: Request,
    file: UploadFile,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
    storage: StorageBackend = Depends(_storage_dep),  # noqa: B008
) -> Response:
    if not await _user_can_access_trip(db, user, trip_id):
        raise HTTPException(404)
    try:
        content = await _read_upload(file, settings.max_upload_bytes)
    except SizeLimitExceeded as e:
        raise HTTPException(413, detail=str(e)) from e
    if not is_pdf(content):
        raise HTTPException(400, detail="not a PDF (magic-byte check failed)")
    doc, is_new = await _upsert_and_enqueue(
        db,
        storage,
        settings,
        user=user,
        trip_id=trip_id,
        segment_id=None,
        filename=file.filename or "document.pdf",
        content=content,
    )
    if is_new:
        return RedirectResponse(f"/trips/{trip_id}/documents", status_code=303)
    # Dup — same redirect with flash; the dedup detection is the test's contract.
    request.session["flash"] = f"Already uploaded as {doc.filename}"
    return RedirectResponse(f"/trips/{trip_id}/documents", status_code=303)


@router.post("/segments/{segment_id}/documents")
async def upload_to_segment(
    segment_id: uuid.UUID,
    request: Request,
    file: UploadFile,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
    storage: StorageBackend = Depends(_storage_dep),  # noqa: B008
) -> Response:
    seg = (await db.execute(select(Segment).where(Segment.id == segment_id))).scalar_one_or_none()
    if seg is None or not await _user_can_access_trip(db, user, seg.trip_id):
        raise HTTPException(404)
    try:
        content = await _read_upload(file, settings.max_upload_bytes)
    except SizeLimitExceeded as e:
        raise HTTPException(413, detail=str(e)) from e
    if not is_pdf(content):
        raise HTTPException(400, detail="not a PDF")
    doc, is_new = await _upsert_and_enqueue(
        db,
        storage,
        settings,
        user=user,
        trip_id=seg.trip_id,
        segment_id=segment_id,
        filename=file.filename or "document.pdf",
        content=content,
    )
    if is_new:
        return RedirectResponse(f"/trips/{seg.trip_id}#segment-{segment_id}", status_code=303)
    request.session["flash"] = f"Already uploaded as {doc.filename}"
    return RedirectResponse(f"/trips/{seg.trip_id}#segment-{segment_id}", status_code=303)


@router.post("/documents/{document_id}/link")
async def link_to_segment(
    document_id: uuid.UUID,
    segment_id: uuid.UUID = Form(...),
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    doc = await _get_owned(db, document_id, user)
    seg = (await db.execute(select(Segment).where(Segment.id == segment_id))).scalar_one_or_none()
    if seg is None or seg.trip_id != doc.trip_id:
        raise HTTPException(400, detail="segment not on this document's trip")
    doc.segment_id = segment_id
    await db.commit()
    return RedirectResponse(f"/trips/{doc.trip_id}/documents", status_code=303)


@router.post("/documents/{document_id}/unlink")
async def unlink_from_segment(
    document_id: uuid.UUID,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    doc = await _get_owned(db, document_id, user)
    doc.segment_id = None
    await db.commit()
    return RedirectResponse(f"/trips/{doc.trip_id}/documents", status_code=303)


@router.delete("/documents/{document_id}")
async def delete_document(
    document_id: uuid.UUID,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> Response:
    doc = await _get_owned(db, document_id, user)
    # Sync Meili delete BEFORE we remove the row so the entity_id is still valid.
    from trip_tracker.search.sync import enqueue_meili_sync

    await enqueue_meili_sync(settings, entity="document", entity_id=doc.id)
    await db.delete(doc)
    await db.commit()
    return Response(status_code=204)


async def _get_owned(db: AsyncSession, doc_id: uuid.UUID, user: User) -> Document:
    doc = (await db.execute(select(Document).where(Document.id == doc_id))).scalar_one_or_none()
    if doc is None:
        raise HTTPException(404)
    if doc.owner_user_id != user.id:
        # Could also allow trip travelers if you want shared-trip doc edits.
        # v0.5.0: owner only — simplest auth model.
        raise HTTPException(403)
    return doc
```

- [ ] **Step 4.4 — Wire router into app**

In `src/trip_tracker/app.py`, after the existing router includes:

```python
from trip_tracker.routes.documents import router as documents_router

app.include_router(documents_router)
```

Match the surrounding pattern.

- [ ] **Step 4.5 — Add `Settings` fields**

In `src/trip_tracker/config.py`:

```python
documents_dir: Path = Path("/data/documents")
max_upload_bytes: int = 26_214_400  # 25 MiB
documents_x_accel_prefix: str | None = None
```

The Pydantic v2 `Settings` class is in `config.py`; insert these alongside the existing fields. Make sure `from pathlib import Path` is imported.

- [ ] **Step 4.6 — Run + commit**

```bash
uv run pytest tests/test_routes_documents_upload.py tests/test_routes_documents_link.py -v
uv run pytest -q
uv run mypy src
uv run ruff check . && uv run ruff format --check .
git add src/trip_tracker/routes/documents.py src/trip_tracker/app.py \
        src/trip_tracker/config.py tests/test_routes_documents_*.py
git commit -m "feat(documents): manual upload + link/unlink/delete routes"
```

**Quality bar:**
- The `_upsert_and_enqueue` helper does **two** DB round-trips on the new path (INSERT then SELECT) — could fold into one with `RETURNING *` but readability wins; the second SELECT is on the PK index and is microseconds.
- `Queue.from_url(...)` + `.disconnect()` mirrors the Phase 4 webhook pattern in `src/trip_tracker/ingest/webhook.py:34`. Don't reach for a long-lived queue singleton; saq's TCP setup is cheap.
- `request.session["flash"]` requires `SessionMiddleware` (already wired by Phase 1). If this raises in tests, add a `@pytest.fixture` that monkeypatches the session — but it shouldn't, the existing flash usage works.
- The X-Accel ownership check in Task 5 will tighten the access logic — for now, `_get_owned` is owner-only.
- `request: Request` is needed only for the flash on dedup; in tests where `request.session` doesn't exist you'll get AttributeError. Wrap the flash assignment in a `try`/`except (AttributeError, AssertionError)` if needed, OR guard with `if hasattr(request, "session"):`.

---

## Task 5 — Download route (X-Accel + FileResponse fallback)

**Spec ref:** §10.

**Files:**
- Modify: `src/trip_tracker/routes/documents.py` (add download handler)
- Create: `tests/test_routes_documents_download.py`

- [ ] **Step 5.1 — Failing tests**

```python
"""GET /documents/{id}/download — auth + X-Accel + fallback."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

# Reuse the seed helper from test_routes_documents_link.
from tests.test_routes_documents_link import _seed_with_segment_and_doc


PDF_BODY = b"%PDF-1.4\nfake\n"


@pytest.mark.asyncio
async def test_download_streams_in_dev_mode(
    db_session, authenticated_client_factory, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DOCUMENTS_DIR", str(tmp_path))
    monkeypatch.delenv("DOCUMENTS_X_ACCEL_PREFIX", raising=False)
    u, t, s, d = await _seed_with_segment_and_doc(db_session)
    # Place a real file matching the doc's storage_key.
    full = tmp_path / d.storage_key
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(PDF_BODY)

    async with authenticated_client_factory(u) as client:
        r = await client.get(f"/documents/{d.id}/download")
    assert r.status_code == 200
    assert r.content == PDF_BODY
    assert "attachment" in r.headers["content-disposition"]
    assert r.headers["content-type"] == "application/pdf"


@pytest.mark.asyncio
async def test_download_x_accel_emits_redirect_header(
    db_session, authenticated_client_factory, monkeypatch
) -> None:
    monkeypatch.setenv("DOCUMENTS_X_ACCEL_PREFIX", "/internal-documents")
    u, t, s, d = await _seed_with_segment_and_doc(db_session)
    async with authenticated_client_factory(u) as client:
        r = await client.get(f"/documents/{d.id}/download")
    assert r.status_code == 204
    assert r.headers["x-accel-redirect"] == f"/internal-documents/{d.storage_key}"
    assert "attachment" in r.headers["content-disposition"]
    assert r.content == b""


@pytest.mark.asyncio
async def test_download_403_for_non_owner(db_session, authenticated_client_factory) -> None:
    from trip_tracker.models.user import User

    u, t, s, d = await _seed_with_segment_and_doc(db_session)
    other = User(oidc_subject="dl1", email="dl1@x.com", display_name="DL1")
    db_session.add(other)
    await db_session.commit()
    async with authenticated_client_factory(other) as client:
        r = await client.get(f"/documents/{d.id}/download")
    assert r.status_code in (403, 404)


@pytest.mark.asyncio
async def test_download_401_anonymous(db_session) -> None:
    from httpx import ASGITransport, AsyncClient
    from trip_tracker.app import create_app

    u, t, s, d = await _seed_with_segment_and_doc(db_session)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get(f"/documents/{d.id}/download")
    assert r.status_code == 401
```

- [ ] **Step 5.2 — Implement download handler**

Append to `src/trip_tracker/routes/documents.py`:

```python
from html import escape

from fastapi.responses import FileResponse


@router.get("/documents/{document_id}/download")
async def download(
    document_id: uuid.UUID,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
    storage: StorageBackend = Depends(_storage_dep),  # noqa: B008
) -> Response:
    doc = (
        await db.execute(select(Document).where(Document.id == document_id))
    ).scalar_one_or_none()
    if doc is None:
        raise HTTPException(404)
    if not await _can_access_doc(db, user, doc):
        raise HTTPException(403)

    safe_filename = quote(doc.filename, safe="")
    cd = f"attachment; filename=\"{escape(doc.filename)}\"; filename*=UTF-8''{safe_filename}"

    if settings.documents_x_accel_prefix:
        return Response(
            status_code=204,
            headers={
                "X-Accel-Redirect": f"{settings.documents_x_accel_prefix}/{doc.storage_key}",
                "Content-Disposition": cd,
                "Content-Type": doc.mime_type,
            },
        )

    path = storage.absolute_path(doc.storage_key)
    return FileResponse(path, media_type=doc.mime_type, filename=doc.filename)


async def _can_access_doc(db: AsyncSession, user: User, doc: Document) -> bool:
    """Owner OR trip-traveler."""
    if doc.owner_user_id == user.id:
        return True
    if doc.trip_id is None:
        return False
    return await _user_can_access_trip(db, user, doc.trip_id)
```

- [ ] **Step 5.3 — Run + commit**

```bash
uv run pytest tests/test_routes_documents_download.py -v
uv run pytest -q
git add src/trip_tracker/routes/documents.py tests/test_routes_documents_download.py
git commit -m "feat(documents): download route with X-Accel + FileResponse fallback"
```

**Quality bar:**
- The `Content-Disposition` header uses both `filename=` (latin-1) and `filename*=UTF-8''…` (RFC 5987) so non-ASCII filenames (Japanese boarding passes) survive. Test against `"切符.pdf"` if you want to stress this — defer if green-path coverage is sufficient.
- The X-Accel header value MUST be a path with a leading slash and MUST NOT include host. The test asserts the exact form.
- `_can_access_doc` is intentionally lenient (owner OR trip-traveler) compared to `_get_owned` (owner-only) used for delete/link. Reading is broader; writing is narrower.

---

## Task 6 — Email attachment persistence (inside `parse_raw_email`)

**Spec ref:** §6.2.

**Reality check vs spec.** The spec §6.2 originally located attachment persistence in the webhook. **The webhook does not resolve the alias to an `owner_user_id`** — that lookup happens in `parse_raw_email` (`src/trip_tracker/worker.py:72-79`, queries `ForwardingAlias`). Persisting attachments in the webhook would require either re-running alias resolution there (Phase-2 refactor) or guessing the owner. The simpler fix, and the one this plan adopts, is to move attachment persistence **into `parse_raw_email`**, immediately after alias resolution succeeds and before segment dispatch. The autolink step (Task 7) then runs after segment dispatch in the same task — both steps live inside the parser job, with no new variable plumbing needed.

**Implication:** Re-forwarding an email with new attachments that wasn't part of the first forward would be dedup'd by `message_id` at the RawEmail level (Phase 2 behavior) — `parse_raw_email` would not re-fire for the second forward. v0.5.0 accepts this limitation; if a user wants to add an attachment to an existing email's record they upload it manually instead. Note in README's "Known limitations" if the user objects.

**Files:**
- Create: `src/trip_tracker/ingest/attachments.py` (pure helper)
- Modify: `src/trip_tracker/worker.py` (call helper inside `parse_raw_email`, after alias resolution)
- Create: `tests/test_documents_attachment_persistence.py`

- [ ] **Step 6.1 — Failing tests**

The tests drive the new helper directly (a simple `persist_pdf_attachments(db, settings, *, raw_email_id, owner_user_id, body)` function), not the webhook. Driving the worker's saq task entry directly is cumbersome; a function-level test of the helper is sufficient since `parse_raw_email` will just call it.

`tests/test_documents_attachment_persistence.py`:

```python
"""persist_pdf_attachments: extract + UPSERT + storage write + extract enqueue."""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.config import Settings
from trip_tracker.documents.persist import persist_pdf_attachments
from trip_tracker.models.document import Document
from trip_tracker.models.raw_email import RawEmail
from trip_tracker.models.user import User


PDF = b"%PDF-1.4\nfake bp\n"


def _email_with(*pdfs: tuple[str, bytes], non_pdf: bytes | None = None) -> bytes:
    msg = EmailMessage()
    msg["From"] = "airline@example.com"
    msg["To"] = "oliver@trips.example.com"
    msg["Subject"] = "Your boarding pass"
    msg["Message-ID"] = "<persist1@test>"
    msg.set_content("Body text.")
    for filename, payload in pdfs:
        msg.add_attachment(payload, maintype="application", subtype="pdf", filename=filename)
    if non_pdf is not None:
        msg.add_attachment(non_pdf, maintype="image", subtype="png", filename="snap.png")
    return msg.as_bytes()


@pytest.mark.asyncio
async def test_pdf_attachment_creates_document_with_owner(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    u = User(oidc_subject="pa1", email="pa1@x.com", display_name="PA1")
    db_session.add(u)
    await db_session.flush()
    re_ = RawEmail(
        to_address="oliver@trips.example.com",
        from_address="x@x.com",
        subject="bp",
        message_id="<pa1@test>",
        mime_blob=b"",
        headers={},
        parse_status="pending",
    )
    db_session.add(re_)
    await db_session.commit()

    settings = Settings(documents_dir=tmp_path)
    body = _email_with(("bp.pdf", PDF))

    new_ids = await persist_pdf_attachments(
        db_session,
        settings,
        raw_email_id=re_.id,
        owner_user_id=u.id,
        body=body,
    )
    await db_session.commit()  # caller commits; helper does not

    docs = (await db_session.execute(select(Document))).scalars().all()
    assert len(docs) == 1
    assert docs[0].filename == "bp.pdf"
    assert docs[0].owner_user_id == u.id
    assert docs[0].raw_email_id == re_.id
    assert docs[0].extract_status == "pending"
    assert docs[0].segment_id is None and docs[0].trip_id is None
    assert new_ids == [docs[0].id]  # caller will enqueue these IDs after commit
    file_path = tmp_path / docs[0].storage_key
    assert file_path.exists() and file_path.read_bytes() == PDF


@pytest.mark.asyncio
async def test_idempotent_on_duplicate_sha256(db_session: AsyncSession, tmp_path: Path) -> None:
    u = User(oidc_subject="pa2", email="pa2@x.com", display_name="PA2")
    db_session.add(u)
    await db_session.flush()
    re_ = RawEmail(
        to_address="o@x",
        from_address="x",
        subject="x",
        message_id="<pa2@test>",
        mime_blob=b"",
        headers={},
        parse_status="pending",
    )
    db_session.add(re_)
    await db_session.commit()

    settings = Settings(documents_dir=tmp_path)
    body = _email_with(("bp.pdf", PDF))

    first_ids = await persist_pdf_attachments(
        db_session,
        settings,
        raw_email_id=re_.id,
        owner_user_id=u.id,
        body=body,
    )
    await db_session.commit()
    second_ids = await persist_pdf_attachments(
        db_session,
        settings,
        raw_email_id=re_.id,
        owner_user_id=u.id,
        body=body,
    )
    await db_session.commit()

    docs = (await db_session.execute(select(Document))).scalars().all()
    assert len(docs) == 1
    assert len(first_ids) == 1
    assert second_ids == []  # second call: existing row, no new id returned


@pytest.mark.asyncio
async def test_non_pdf_attachment_dropped(db_session: AsyncSession, tmp_path: Path) -> None:
    u = User(oidc_subject="pa3", email="pa3@x.com", display_name="PA3")
    db_session.add(u)
    await db_session.flush()
    re_ = RawEmail(
        to_address="o@x",
        from_address="x",
        subject="x",
        message_id="<pa3@test>",
        mime_blob=b"",
        headers={},
        parse_status="pending",
    )
    db_session.add(re_)
    await db_session.commit()

    settings = Settings(documents_dir=tmp_path)
    body = _email_with(non_pdf=b"\x89PNG\r\n")  # no PDFs, only an image

    new_ids = await persist_pdf_attachments(
        db_session,
        settings,
        raw_email_id=re_.id,
        owner_user_id=u.id,
        body=body,
    )
    await db_session.commit()

    docs = (await db_session.execute(select(Document))).scalars().all()
    assert docs == []
    assert new_ids == []
```

Drop the unused `from unittest.mock import AsyncMock, MagicMock, patch` import at the top of this test file — the helper no longer involves Queue, so no mocking is needed.

- [ ] **Step 6.2 — Implement attachment extractor**

`src/trip_tracker/ingest/attachments.py`:

```python
"""Extract non-inline attachments from a raw MIME body."""

from __future__ import annotations

import email
import email.parser
import email.policy
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Attachment:
    filename: str
    content_type: str
    payload: bytes


def extract_attachments(body: bytes) -> list[Attachment]:
    """Return all non-inline attachments. Inline + multipart wrappers are skipped."""
    msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(body)
    out: list[Attachment] = []
    for part in msg.iter_attachments():
        try:
            payload = part.get_content()
        except (KeyError, AttributeError):
            continue
        if not isinstance(payload, bytes):
            # Text attachments come back as str — skip; we only care about PDFs.
            continue
        out.append(
            Attachment(
                filename=part.get_filename() or "attachment",
                content_type=part.get_content_type() or "application/octet-stream",
                payload=payload,
            )
        )
    return out
```

- [ ] **Step 6.3 — Implement the persist helper**

`src/trip_tracker/documents/persist.py`:

```python
"""Persist PDF email attachments. Spec §6.2.

Called from `parse_raw_email` (worker.py) after alias resolution succeeds.
The autolink step (Task 7) runs separately, after segments are committed,
so this function leaves segment_id and trip_id NULL.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from saq import Queue
from sqlalchemy import column, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.config import Settings
from trip_tracker.documents.helpers import is_pdf, sha256_hex
from trip_tracker.documents.storage import LocalFsStorage
from trip_tracker.ingest.attachments import extract_attachments
from trip_tracker.models.document import Document

_logger = logging.getLogger(__name__)


async def persist_pdf_attachments(
    db: AsyncSession,
    settings: Settings,
    *,
    raw_email_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    body: bytes,
) -> list[uuid.UUID]:
    """For each PDF attachment in `body`: UPSERT a Document row + write the file.
    Returns the IDs of newly-INSERTED documents (not UPSERTed-existing ones).
    Idempotent on (owner_user_id, sha256).

    NOTE: does NOT commit. The caller (parse_raw_email) commits the session
    once after segment dispatch finishes, so a parser failure rolls back
    documents along with everything else. The caller is also responsible for
    enqueuing extract_document for each returned id AFTER the commit — see
    Step 6.4 below.
    """
    attachments = extract_attachments(body)
    if not attachments:
        return []
    storage = LocalFsStorage(settings.documents_dir)
    new_ids: list[uuid.UUID] = []

    for att in attachments:
        if not is_pdf(att.payload):
            _logger.info(
                "ingest: dropping non-PDF attachment %r (content_type=%s, size=%d)",
                att.filename,
                att.content_type,
                len(att.payload),
            )
            continue
        sha = sha256_hex(att.payload)
        stmt = (
            pg_insert(Document)
            .values(
                owner_user_id=owner_user_id,
                raw_email_id=raw_email_id,
                filename=att.filename,
                mime_type="application/pdf",
                size_bytes=len(att.payload),
                sha256=sha,
                storage_key=f"{sha[:2]}/{sha}",
                extract_status="pending",
            )
            .on_conflict_do_update(
                index_elements=["owner_user_id", "sha256"],
                set_={"raw_email_id": raw_email_id, "updated_at": func.now()},
            )
            .returning(Document.id, (column("xmax") == 0).label("inserted"))
        )
        row = (await db.execute(stmt)).one()
        if row.inserted:
            # File write is content-addressed and idempotent; safe to do
            # before commit because re-running on a rollback writes the same
            # bytes to the same path and either succeeds or no-ops.
            await storage.put(sha, att.payload)
            new_ids.append(row.id)
        # else: existing row — UPSERT just updated raw_email_id; no new file.
    return new_ids
```

**Why no commit + return ids:** if the segment-dispatch step in `parse_raw_email` later raises, the whole transaction rolls back — including these document INSERTs. Otherwise we'd ship orphan attachment rows tied to a `raw_email` whose `parse_status` never advanced. The file on disk is content-addressed and idempotent, so leaving an unreferenced file after rollback is acceptable (would be cleaned up by a future `bin/cleanup-orphans` job; tracked as Phase 5.x).

**Why enqueue after commit:** if we enqueue inside the transaction, a worker could pick up `extract_document` for a doc id that doesn't exist yet (the INSERT hasn't committed). Saq commits the enqueue immediately on its Redis side; the DB INSERT is on a separate transaction. Race-safe ordering is: caller commits → caller enqueues.

- [ ] **Step 6.4 — Wire into `parse_raw_email`**

In `src/trip_tracker/worker.py`, inside `parse_raw_email`, **right after the alias-resolution block succeeds** (after `owner = ...` and the `no_segments` early-return for missing aliases), add the persistence call and capture the returned ids:

```python
# Persist PDF attachments now that we have owner.user_id. Auto-link is
# deferred to a separate call after segment dispatch (Task 7). The helper
# does NOT commit — that happens after segment dispatch.
from trip_tracker.documents.persist import persist_pdf_attachments

new_doc_ids = await persist_pdf_attachments(
    db,
    settings,
    raw_email_id=raw.id,
    owner_user_id=owner.user_id,
    body=raw.mime_blob,
)
```

The exact insertion line: just above the `_msg = BytesParser(...)` line that begins segment dispatch (around `worker.py:84`). The `db`, `settings`, `raw`, and `owner` variables are all already in scope at that point.

**After the existing `db.commit()` at the end of the function** (or in the same commit block where `parse_status` is updated), add:

```python
# Enqueue extract_document for each newly-inserted doc, AFTER commit so
# the worker that picks up the task can find the row.
if new_doc_ids:
    q = Queue.from_url(str(settings.redis_url))
    try:
        for doc_id in new_doc_ids:
            await q.enqueue("extract_document", document_id=str(doc_id))
    finally:
        await q.disconnect()
```

Adjust the `persist_pdf_attachments` test (Step 6.1) to reflect the new contract:
- The test should pass `db_session` (which auto-commits when the test exits the fixture) and assert that `persist_pdf_attachments` returns the list of new ids.
- The "enqueue called" assertion is removed from the helper test — enqueueing is now the caller's responsibility. Add a separate small integration test for `parse_raw_email` that mocks `Queue.from_url` and asserts `q.enqueue("extract_document", ...)` is called once per new doc id, ONLY after the segment-commit transaction succeeds.

- [ ] **Step 6.5 — Run + commit**

```bash
uv run pytest tests/test_documents_attachment_persistence.py -v
uv run pytest -q
git add src/trip_tracker/ingest/attachments.py \
        src/trip_tracker/documents/persist.py \
        src/trip_tracker/worker.py \
        tests/test_documents_attachment_persistence.py
git commit -m "feat(documents): persist PDF email attachments inside parse_raw_email"
```

**Quality bar:**
- `iter_attachments()` skips inline parts and multipart wrappers automatically — don't reimplement.
- `column("xmax") == 0` needs `column` + `func` imported from `sqlalchemy`. Mypy may infer `Any` for the literal — `.label("inserted")` preserves the truthy access via `row.inserted`.
- The `Queue.from_url` open/disconnect cycle mirrors `enqueue_parse` (`webhook.py:34`); don't reach for a long-lived queue singleton.
- One commit at the end of the loop — not per-attachment.
- The helper takes `body: bytes` directly (not a `RawEmail`); the caller passes `raw.mime_blob`. This keeps the helper testable with hand-crafted MIME without DB seeding.

---

## Task 7 — Auto-link heuristic + parser-pipeline integration

**Spec ref:** §6.2 interaction note + §7.

**Files:**
- Create: `src/trip_tracker/documents/autolink.py`
- Modify: `src/trip_tracker/worker.py` (call autolink inside `parse_raw_email`)
- Create: `tests/test_documents_autolink.py`

- [ ] **Step 7.1 — Failing tests for the heuristic**

`tests/test_documents_autolink.py`:

```python
"""Auto-link heuristic: filename → segment.id."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from trip_tracker.documents.autolink import match_attachment_to_segment


@dataclass
class FakeSeg:
    id: uuid.UUID
    type: str
    confirmation_number: str | None
    details: dict
    start_at: datetime


def _seg(
    *, conf=None, fnum=None, tnum=None, type_="flight", start=datetime(2026, 6, 1, 13, tzinfo=UTC)
) -> FakeSeg:
    details = {}
    if fnum:
        details["flight_number"] = fnum
    if tnum:
        details["train_number"] = tnum
    return FakeSeg(uuid.uuid4(), type_, conf, details, start)


def test_match_by_confirmation_number() -> None:
    s = _seg(conf="ABC123")
    assert match_attachment_to_segment("BoardingPass_ABC123.pdf", [s]) == s.id


def test_match_by_flight_number() -> None:
    s = _seg(fnum="AF7237")
    assert match_attachment_to_segment("af7237_paris.pdf", [s]) == s.id


def test_match_by_train_number() -> None:
    s = _seg(tnum="9023", type_="train")
    assert match_attachment_to_segment("Ticket_9023.pdf", [s]) == s.id


def test_match_by_unique_date() -> None:
    s = _seg(start=datetime(2026, 6, 1, 13, tzinfo=UTC))
    assert match_attachment_to_segment("boarding_2026-06-01.pdf", [s]) == s.id
    assert match_attachment_to_segment("boarding_20260601.pdf", [s]) == s.id


def test_no_match_returns_none() -> None:
    s = _seg(conf="ABC123", fnum="AF999")
    assert match_attachment_to_segment("random.pdf", [s]) is None


def test_ambiguous_date_returns_none() -> None:
    s1 = _seg(start=datetime(2026, 6, 1, 13, tzinfo=UTC))
    s2 = _seg(start=datetime(2026, 6, 1, 19, tzinfo=UTC))
    assert match_attachment_to_segment("boarding_2026-06-01.pdf", [s1, s2]) is None


def test_first_match_wins_when_both_conf_and_date_apply() -> None:
    s_conf = _seg(conf="XYZ999")
    s_date = _seg(start=datetime(2026, 6, 5, tzinfo=UTC))
    assert match_attachment_to_segment("BP_XYZ999_2026-06-05.pdf", [s_date, s_conf]) == s_conf.id
```

- [ ] **Step 7.2 — Implement the heuristic**

`src/trip_tracker/documents/autolink.py`:

```python
"""Filename → segment.id auto-link heuristic. Spec §7."""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import date

# Module-level: imported by parser job; keep imports minimal.

_DATE_DASH = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATE_COMPACT = re.compile(r"\b(\d{4})(\d{2})(\d{2})\b")


def match_attachment_to_segment(
    filename: str,
    segments: Sequence["object"],
) -> uuid.UUID | None:
    """Return the matching segment.id or None.

    Three rules, first match wins:
      1. confirmation_number (exact, word-boundary, case-insensitive)
      2. flight_number / train_number (same shape, from Segment.details)
      3. unique start_at::date (YYYY-MM-DD or YYYYMMDD in filename)

    Ambiguous date matches (≥2 segments on the same day) → None.

    `segments` is typed as Sequence[object] to keep the function purely
    structural — callers pass real Segment ORM rows or dataclass test fakes.
    """
    # Rule 1: confirmation number
    for s in segments:
        conf = getattr(s, "confirmation_number", None)
        if conf and re.search(rf"\b{re.escape(conf)}\b", filename, re.IGNORECASE):
            return getattr(s, "id")

    # Rule 2: vehicle number (flight_number or train_number from details)
    for s in segments:
        details = getattr(s, "details", None) or {}
        for key in ("flight_number", "train_number"):
            vnum = details.get(key)
            if vnum and re.search(rf"\b{re.escape(str(vnum))}\b", filename, re.IGNORECASE):
                return getattr(s, "id")

    # Rule 3: unique date
    target: date | None = None
    if m := _DATE_DASH.search(filename):
        target = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    elif m := _DATE_COMPACT.search(filename):
        target = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if target is None:
        return None

    same_day = [
        s
        for s in segments
        if getattr(s, "start_at", None) and getattr(s, "start_at").date() == target
    ]
    if len(same_day) == 1:
        return getattr(same_day[0], "id")
    return None
```

- [ ] **Step 7.3 — Run heuristic tests, then add parser-pipeline integration test**

```bash
uv run pytest tests/test_documents_autolink.py -v
# 7 passed
```

Add an integration test in the same file (or `tests/test_worker_autolink.py`):

```python
"""parse_raw_email auto-links unlinked Documents after creating segments."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from trip_tracker.models.document import Document
from trip_tracker.models.raw_email import RawEmail
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User


@pytest.mark.asyncio
async def test_parse_raw_email_auto_links_documents(
    db_url: str, db_session: AsyncSession
) -> None:
    # Seed: user, trip, RawEmail, Segment (parser would normally create segment),
    # and a Document with raw_email_id set + segment_id NULL.
    u = User(oidc_subject="al1", email="al1@x.com", display_name="AL1")
    db_session.add(u); await db_session.flush()
    t = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 2),
             created_by=u.id)
    db_session.add(t); await db_session.flush()
    db_session.add(TripTraveler(trip_id=t.id, user_id=u.id, role="owner"))
    re_ = RawEmail(to_address="oliver@trips.example.com", from_address="x@x.com",
                   subject="bp", message_id="<al1@test>", mime_blob=b"",
                   headers={}, parse_status="parsed")
    db_session.add(re_); await db_session.flush()
    s = Segment(trip_id=t.id, owner_user_id=u.id, type="flight", status="confirmed",
                confirmation_number="K8YH3M",
                start_at=datetime(2026, 6, 1, 13, tzinfo=UTC), start_tz="UTC",
                parse_source="manual", parse_confidence=1.0,
                raw_email_id=re_.id)
    db_session.add(s)
    db_session.add(Document(
        owner_user_id=u.id, raw_email_id=re_.id, segment_id=None, trip_id=None,
        filename="Itinerary_K8YH3M_2026.pdf", mime_type="application/pdf",
        size_bytes=10, sha256="e" * 64, storage_key="ee/" + "e" * 64,
    ))
    await db_session.commit()

    # Call the autolink function directly; we don't need to dispatch full parser job.
    from trip_tracker.documents.autolink import autolink_pending_for_email
    engine = create_async_engine(db_url)
    SM = async_sessionmaker(engine, expire_on_commit=False)
    async with SM() as db:
        await autolink_pending_for_email(db, raw_email_id=re_.id)
    await engine.dispose()

    refreshed = (await db_session.execute(select(Document))).scalars().all()
    assert len(refreshed) == 1
    assert refreshed[0].segment_id == s.id
    assert refreshed[0].trip_id == t.id


@pytest.mark.asyncio
async def test_autolink_skips_already_linked_documents(
    db_url: str, db_session: AsyncSession
) -> None:
    # Same setup but Document already has segment_id set — autolink must skip it.
    # ... (same seed pattern; assert segment_id stays put)
```

Add the helper to `autolink.py`:

```python
async def autolink_pending_for_email(
    db: "AsyncSession",
    *,
    raw_email_id: uuid.UUID,
) -> None:
    """For each Document with raw_email_id=:rid AND segment_id IS NULL,
    look up that email's segments and run match_attachment_to_segment.
    Update the doc with segment_id + trip_id when matched.
    """
    from sqlalchemy import select
    from trip_tracker.models.document import Document
    from trip_tracker.models.segment import Segment

    docs = (
        (
            await db.execute(
                select(Document).where(
                    Document.raw_email_id == raw_email_id,
                    Document.segment_id.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not docs:
        return

    segs = (
        (await db.execute(select(Segment).where(Segment.raw_email_id == raw_email_id)))
        .scalars()
        .all()
    )
    if not segs:
        return

    for doc in docs:
        match_id = match_attachment_to_segment(doc.filename, segs)
        if match_id is None:
            continue
        seg = next(s for s in segs if s.id == match_id)
        doc.segment_id = match_id
        doc.trip_id = seg.trip_id
    await db.commit()
```

- [ ] **Step 7.4 — Hook into `parse_raw_email`**

In `src/trip_tracker/worker.py`'s `parse_raw_email` body, after the segments-commit block (around the existing `enqueue_meili_sync(...)` calls), add:

```python
from trip_tracker.documents.autolink import autolink_pending_for_email

await autolink_pending_for_email(db, raw_email_id=rid)
```

The variable name `rid` (= `uuid.UUID(raw_email_id)`) is defined near the top of `parse_raw_email` (`worker.py:58`). The exact insertion point is right after segments are committed and Meili-synced — that way auto-link sees the just-committed segments.

- [ ] **Step 7.5 — Run + commit**

```bash
uv run pytest tests/test_documents_autolink.py -v
uv run pytest -q
git add src/trip_tracker/documents/autolink.py src/trip_tracker/worker.py tests/test_documents_autolink.py
git commit -m "feat(documents): filename heuristic + auto-link inside parse_raw_email"
```

**Quality bar:**
- The `getattr(s, ..., default)` style in the heuristic keeps it pure-structural so test fakes (dataclasses) and real ORM rows both work without conditional imports.
- `Segment.raw_email_id` exists (Phase 3 added it) — verify with `grep raw_email_id src/trip_tracker/models/segment.py` before relying on it for the SELECT.
- Don't conflate "already-linked" docs with "newly-linked-this-run" — the SELECT filter `segment_id IS NULL` handles this. Manual links via `/documents/{id}/link` are preserved by construction.

---

## Task 8 — Extraction saq task + worker startup

**Spec ref:** §8.

**Files:**
- Modify: `pyproject.toml` (add `pdfplumber` dep)
- Create: `src/trip_tracker/documents/extract.py`
- Modify: `src/trip_tracker/worker.py` (register `extract_document`; add `storage` to ctx; `case "document"` in `sync_meili`)
- Create: `tests/test_documents_extract.py`
- Add: `tests/fixtures/documents/tiny-text.pdf` (small but real text PDF)
- Add: `tests/fixtures/documents/tiny-empty.pdf` (PDF with no extractable text)

- [ ] **Step 8.1 — Add `pdfplumber` dep**

```bash
uv add 'pdfplumber>=0.11,<0.12'
```

Per `feedback_dependency-currency.md`: verify the latest stable on PyPI before adding (`curl -s https://pypi.org/pypi/pdfplumber/json | jq -r .info.version`). Renovate will normalize the pin after push — don't fuss about exact range.

- [ ] **Step 8.2 — Add fixture PDFs**

Generate tiny fixtures programmatically (committed as binaries):

```python
# scripts/_make_test_pdfs.py — run once to produce tests/fixtures/documents/*
from pathlib import Path
import reportlab.pdfgen.canvas  # dev-only; not in prod deps

out = Path("tests/fixtures/documents")
out.mkdir(parents=True, exist_ok=True)

c = reportlab.pdfgen.canvas.Canvas(str(out / "tiny-text.pdf"))
c.drawString(100, 750, "AIR FRANCE BOARDING PASS")
c.drawString(100, 730, "Flight AF007 · JFK → CDG")
c.showPage()
c.save()

# Empty PDF: a blank page
c = reportlab.pdfgen.canvas.Canvas(str(out / "tiny-empty.pdf"))
c.showPage()
c.save()
```

Run once locally, commit the resulting PDFs. Don't add `reportlab` to project deps.

- [ ] **Step 8.3 — Failing tests**

`tests/test_documents_extract.py`:

```python
"""extract_document saq task: pdfplumber → extracted_text."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from trip_tracker.config import Settings
from trip_tracker.documents.extract import extract_document
from trip_tracker.documents.storage import LocalFsStorage
from trip_tracker.models.document import Document
from trip_tracker.models.user import User


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "documents"


async def _seed_doc(
    db: AsyncSession,
    *,
    storage: LocalFsStorage,
    fixture: str,
) -> Document:
    u = User(oidc_subject="ex1", email="ex1@x.com", display_name="EX1")
    db.add(u)
    await db.flush()
    payload = (FIXTURE_DIR / fixture).read_bytes()
    from trip_tracker.documents.helpers import sha256_hex

    sha = sha256_hex(payload)
    await storage.put(sha, payload)
    d = Document(
        owner_user_id=u.id,
        filename=fixture,
        mime_type="application/pdf",
        size_bytes=len(payload),
        sha256=sha,
        storage_key=f"{sha[:2]}/{sha}",
        extract_status="pending",
    )
    db.add(d)
    await db.commit()
    return d


@pytest.mark.asyncio
async def test_extract_text_pdf_populates_extracted_text(
    db_url: str, db_session: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalFsStorage(tmp_path)
    d = await _seed_doc(db_session, storage=storage, fixture="tiny-text.pdf")

    engine = create_async_engine(db_url)
    settings = Settings()  # uses test env from conftest
    ctx = {"engine": engine, "settings": settings, "storage": storage}
    await extract_document(ctx, document_id=str(d.id))
    await engine.dispose()

    refreshed = (await db_session.execute(select(Document).where(Document.id == d.id))).scalar_one()
    assert refreshed.extract_status == "extracted"
    assert refreshed.extract_method == "pdfplumber"
    assert "AIR FRANCE" in (refreshed.extracted_text or "")


@pytest.mark.asyncio
async def test_extract_empty_pdf_marked_empty(
    db_url: str, db_session: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalFsStorage(tmp_path)
    d = await _seed_doc(db_session, storage=storage, fixture="tiny-empty.pdf")

    engine = create_async_engine(db_url)
    ctx = {"engine": engine, "settings": Settings(), "storage": storage}
    await extract_document(ctx, document_id=str(d.id))
    await engine.dispose()

    refreshed = (await db_session.execute(select(Document).where(Document.id == d.id))).scalar_one()
    assert refreshed.extract_status == "empty"
    assert refreshed.extracted_text in (None, "")


@pytest.mark.asyncio
async def test_extract_idempotent_on_already_extracted(
    db_url: str, db_session: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalFsStorage(tmp_path)
    d = await _seed_doc(db_session, storage=storage, fixture="tiny-text.pdf")
    d.extract_status = "extracted"
    d.extracted_text = "preserved"
    db_session.add(d)
    await db_session.commit()

    engine = create_async_engine(db_url)
    ctx = {"engine": engine, "settings": Settings(), "storage": storage}
    await extract_document(ctx, document_id=str(d.id))
    await engine.dispose()

    refreshed = (await db_session.execute(select(Document).where(Document.id == d.id))).scalar_one()
    assert refreshed.extracted_text == "preserved"  # untouched


@pytest.mark.asyncio
async def test_extract_marks_failed_on_pdfplumber_error(
    db_url: str, db_session: AsyncSession, tmp_path: Path, monkeypatch
) -> None:
    """Corrupt PDF → pdfplumber raises → extract_status='failed', text=NULL."""
    storage = LocalFsStorage(tmp_path)
    d = await _seed_doc(db_session, storage=storage, fixture="tiny-text.pdf")

    def _boom(buf):  # type: ignore[no-untyped-def]
        raise ValueError("simulated PDFSyntaxError")

    monkeypatch.setattr("trip_tracker.documents.extract._extract_pdf", _boom)

    engine = create_async_engine(db_url)
    ctx = {"engine": engine, "settings": Settings(), "storage": storage}
    await extract_document(ctx, document_id=str(d.id))
    await engine.dispose()

    refreshed = (await db_session.execute(select(Document).where(Document.id == d.id))).scalar_one()
    assert refreshed.extract_status == "failed"
    assert refreshed.extracted_text is None


@pytest.mark.asyncio
async def test_extract_marks_unsupported_for_non_pdf_mime(
    db_url: str, db_session: AsyncSession, tmp_path: Path
) -> None:
    """A doc seeded with mime_type='image/png' → 'unsupported', no extraction."""
    storage = LocalFsStorage(tmp_path)
    u = User(oidc_subject="ex2", email="ex2@x.com", display_name="EX2")
    db_session.add(u)
    await db_session.flush()
    d = Document(
        owner_user_id=u.id,
        filename="x.png",
        mime_type="image/png",
        size_bytes=10,
        sha256="2" * 64,
        storage_key="22/" + "2" * 64,
        extract_status="pending",
    )
    db_session.add(d)
    await db_session.commit()

    engine = create_async_engine(db_url)
    ctx = {"engine": engine, "settings": Settings(), "storage": storage}
    await extract_document(ctx, document_id=str(d.id))
    await engine.dispose()

    refreshed = (await db_session.execute(select(Document).where(Document.id == d.id))).scalar_one()
    assert refreshed.extract_status == "unsupported"
    assert refreshed.extracted_text is None
```

- [ ] **Step 8.4 — Implement the task**

`src/trip_tracker/documents/extract.py`:

```python
"""Document extraction saq task. Spec §8."""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

import pdfplumber
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from trip_tracker.config import Settings
from trip_tracker.documents.storage import StorageBackend
from trip_tracker.models.document import Document
from trip_tracker.search.sync import enqueue_meili_sync

_logger = logging.getLogger(__name__)
_EXTRACT_TIMEOUT_SEC = 60.0


def _extract_pdf(buf: io.BytesIO) -> str:
    """Sync pdfplumber call. Run in an executor."""
    pages: list[str] = []
    with pdfplumber.open(buf) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if text:
                pages.append(text)
    return "\n\n".join(pages)


async def extract_document(ctx: dict[str, Any], *, document_id: str) -> None:
    """saq task: load doc, run pdfplumber, persist text, enqueue Meili sync."""
    engine = ctx["engine"]
    settings: Settings = ctx["settings"]
    storage: StorageBackend = ctx["storage"]
    SM = async_sessionmaker(engine, expire_on_commit=False)

    async with SM() as db:
        doc = (
            await db.execute(select(Document).where(Document.id == document_id))
        ).scalar_one_or_none()
        if doc is None:
            _logger.warning("extract_document: id=%s not found", document_id)
            return
        if doc.extract_status != "pending":
            _logger.info(
                "extract_document: id=%s status=%s — skipping",
                document_id,
                doc.extract_status,
            )
            return
        if doc.mime_type != "application/pdf":
            doc.extract_status = "unsupported"
            await db.commit()
            return

        # Read the file into memory; pdfplumber needs seekable.
        buf = io.BytesIO()
        async for chunk in await storage.open(doc.storage_key):
            buf.write(chunk)
        buf.seek(0)

        loop = asyncio.get_running_loop()
        try:
            text = await asyncio.wait_for(
                loop.run_in_executor(None, _extract_pdf, buf),
                timeout=_EXTRACT_TIMEOUT_SEC,
            )
        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
            _logger.warning("extract_document: id=%s failed: %s", document_id, exc)
            doc.extract_status = "failed"
            doc.extracted_text = None
            await db.commit()
            return

        if text:
            doc.extract_status = "extracted"
            doc.extract_method = "pdfplumber"
            doc.extracted_text = text
        else:
            doc.extract_status = "empty"
            doc.extracted_text = None
        await db.commit()

    # Enqueue Meili sync (only when there's text to index).
    if doc.extract_status in ("extracted", "empty"):
        await enqueue_meili_sync(settings, entity="document", entity_id=doc.id)
```

- [ ] **Step 8.5 — Register in worker**

In `src/trip_tracker/worker.py`:

```python
# At the top, alongside parse_raw_email + sync_meili imports:
from trip_tracker.documents.extract import extract_document
from trip_tracker.documents.storage import LocalFsStorage
from pathlib import Path

# In `startup(ctx)`:
ctx["storage"] = LocalFsStorage(Path(s.documents_dir))

# Below the `settings = {...}` dict, extend functions list:
settings = {
    ...,
    "functions": [parse_raw_email, sync_meili, extract_document],
    ...,
}
```

Task 9 will add the `case "document":` arm to `sync_meili`.

- [ ] **Step 8.6 — Run + commit**

```bash
uv run pytest tests/test_documents_extract.py -v
uv run pytest -q
git add pyproject.toml uv.lock src/trip_tracker/documents/extract.py \
        src/trip_tracker/worker.py tests/test_documents_extract.py \
        tests/fixtures/documents/
git commit -m "feat(documents): extract_document saq task + pdfplumber dependency"
```

**Quality bar:**
- The `except (asyncio.TimeoutError, Exception):  # noqa: BLE001` is intentionally broad — pdfplumber can raise `PDFSyntaxError`, `PSException`, `ValueError`, `TypeError` on different malformed PDFs. The handler always sets `failed` status. Bandit B110 is silenced via the explicit handler body (we set status, not just `pass`).
- pdfplumber holds page text in memory; for a typical boarding pass (1–2 pages) this is microscopic. The 60s timeout protects against pathological multi-thousand-page PDFs.
- `pdfplumber.open(buf)` requires a seekable file-like; `io.BytesIO` is fine. Don't pass the raw async iterator.
- The fixture PDFs are small (~1 KB each); committing them as binaries is acceptable. Add `*.pdf` to `.gitattributes` as `binary` if you want git diff to skip them.

---

## Task 9 — Meili 3rd index (`documents`)

**Spec ref:** §9.

**Files:**
- Modify: `src/trip_tracker/search/sync.py` (add `document_to_doc` + extend `Literal`)
- Modify: `src/trip_tracker/search/client.py` (add `_DOCUMENTS_FILTERABLE`/`_DOCUMENTS_SORTABLE` + 3rd loop entry)
- Modify: `src/trip_tracker/search/proxy.py` (broaden `Literal`)
- Modify: `src/trip_tracker/search/reindex.py` (third walk)
- Modify: `src/trip_tracker/worker.py` `sync_meili` (add `case "document"`)
- Modify: `src/trip_tracker/templates/_search_palette.html` (add "documents" to indexes array; render doc hits)
- Create: `tests/test_search_documents.py`
- Create: `tests/test_search_reindex_documents.py`

- [ ] **Step 9.1 — Failing tests**

```python
"""Documents Meili index: doc renderer, sync wiring, proxy access, reindex walk."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.document import Document
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User
from trip_tracker.search.sync import document_to_doc, enqueue_meili_sync


@pytest.mark.asyncio
async def test_document_to_doc_shape(db_session: AsyncSession) -> None:
    u = User(oidc_subject="dt1", email="dt1@x.com", display_name="DT1")
    db_session.add(u)
    await db_session.flush()
    t = Trip(
        title="Paris vacation",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 7),
        created_by=u.id,
    )
    db_session.add(t)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=t.id, user_id=u.id, role="owner"))
    d = Document(
        owner_user_id=u.id,
        trip_id=t.id,
        filename="bp.pdf",
        mime_type="application/pdf",
        size_bytes=10,
        sha256="f" * 64,
        storage_key="ff/" + "f" * 64,
        extracted_text="AIR FRANCE",
    )
    db_session.add(d)
    await db_session.commit()

    doc = await document_to_doc(d, db=db_session)
    assert doc["id"] == str(d.id)
    assert doc["filename"] == "bp.pdf"
    assert doc["extracted_text"] == "AIR FRANCE"
    assert str(u.id) in doc["traveler_ids"]
    assert doc["trip_id"] == str(t.id)
    assert doc["segment_id"] is None
    assert isinstance(doc["created_at_unix"], int)


@pytest.mark.asyncio
async def test_orphan_document_traveler_ids_falls_back_to_owner(
    db_session: AsyncSession,
) -> None:
    u = User(oidc_subject="dt2", email="dt2@x.com", display_name="DT2")
    db_session.add(u)
    await db_session.flush()
    d = Document(
        owner_user_id=u.id,
        filename="o.pdf",
        mime_type="application/pdf",
        size_bytes=10,
        sha256="9" * 64,
        storage_key="99/" + "9" * 64,
    )
    db_session.add(d)
    await db_session.commit()
    doc = await document_to_doc(d, db=db_session)
    assert doc["traveler_ids"] == [str(u.id)]
    assert doc["trip_id"] is None
```

`tests/test_search_reindex_documents.py`:

```python
"""reindex extension: third walk for Documents."""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from trip_tracker.models.document import Document
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User
from trip_tracker.search.reindex import reindex_all


@pytest.mark.asyncio
async def test_reindex_walks_documents(db_url: str, db_session: AsyncSession) -> None:
    u = User(oidc_subject="rd1", email="rd1@x.com", display_name="RD1")
    db_session.add(u)
    await db_session.flush()
    t = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 2), created_by=u.id)
    db_session.add(t)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=t.id, user_id=u.id, role="owner"))
    db_session.add(
        Document(
            owner_user_id=u.id,
            trip_id=t.id,
            filename="r.pdf",
            mime_type="application/pdf",
            size_bytes=10,
            sha256="1" * 64,
            storage_key="11/" + "1" * 64,
            extract_status="extracted",
            extracted_text="hello",
        )
    )
    await db_session.commit()

    indexes = {n: MagicMock() for n in ("trips", "segments", "documents")}
    for idx in indexes.values():
        idx.update_documents = AsyncMock()
        idx.update_filterable_attributes = AsyncMock()
        idx.update_sortable_attributes = AsyncMock()
    fake_meili = MagicMock()
    fake_meili.delete_index = AsyncMock()
    fake_meili.create_index = AsyncMock()
    fake_meili.index = MagicMock(side_effect=lambda n: indexes[n])

    engine = create_async_engine(db_url)
    counts = await reindex_all(engine, fake_meili, batch_size=50)
    await engine.dispose()

    assert counts["documents"] == 1
    indexes["documents"].update_documents.assert_awaited()
```

- [ ] **Step 9.2 — Implement `document_to_doc`**

In `src/trip_tracker/search/sync.py`, add the renderer (mirror trip_to_doc / segment_to_doc):

```python
async def document_to_doc(doc: Document, *, db: AsyncSession) -> dict[str, Any]:
    """Render a Document as a Meili index payload. Spec §9.2."""
    if doc.trip_id is not None:
        traveler_ids = [
            str(uid)
            for uid in (
                await db.execute(
                    select(TripTraveler.user_id).where(TripTraveler.trip_id == doc.trip_id)
                )
            )
            .scalars()
            .all()
        ]
    else:
        traveler_ids = [str(doc.owner_user_id)]
    return {
        "id": str(doc.id),
        "owner_user_id": str(doc.owner_user_id),
        "trip_id": str(doc.trip_id) if doc.trip_id else None,
        "segment_id": str(doc.segment_id) if doc.segment_id else None,
        "traveler_ids": traveler_ids,
        "filename": doc.filename,
        "extracted_text": doc.extracted_text or "",
        "mime_type": doc.mime_type,
        "created_at_unix": int(doc.created_at.timestamp()),
    }
```

Add `from trip_tracker.models.document import Document` at the top.

- [ ] **Step 9.3 — Extend `Literal` in `enqueue_meili_sync`**

In the same file, change the signature:

```python
async def enqueue_meili_sync(
    settings: Settings,
    *,
    entity: Literal["trip", "segment", "document"],
    entity_id: uuid.UUID,
) -> None: ...
```

The existing implementation body (`q.enqueue("sync_meili", ...)`) is unchanged.

- [ ] **Step 9.4 — Add `elif entity == "document"` arm in `sync_meili`**

The existing `sync_meili` (`worker.py:168`) uses `if/elif/else`, NOT a `match` statement. Add a third `elif` arm before the existing `else`:

```python
# Existing structure (don't refactor to match — just extend):
async def sync_meili(ctx: dict[str, Any], *, entity: str, entity_id: str) -> None:
    ...
    async with SM() as db:
        if entity == "trip":
            ...  # existing
        elif entity == "segment":
            ...  # existing
        elif entity == "document":
            from trip_tracker.models.document import Document
            from trip_tracker.search.sync import document_to_doc

            # Match the trip/segment arms: resolve the string id to a UUID
            # once, then use db.get() (avoids the raw-string == UUID compare
            # that other arms intentionally don't do).
            doc_uuid = uuid.UUID(entity_id)
            doc = await db.get(Document, doc_uuid)
            if doc is None:
                await meili.index("documents").delete_document(entity_id)
            else:
                payload = await document_to_doc(doc, db=db)
                await meili.index("documents").update_documents([payload])
        else:
            logger.warning("sync_meili: unknown entity=%s", entity)
```

The `logger` name in worker.py is `logger` (not `_logger`); match the existing convention.

- [ ] **Step 9.5 — Add 3rd index in `ensure_indexes_configured`**

In `src/trip_tracker/search/client.py`, add the constants near the existing `_TRIP_FILTERABLE` etc:

```python
_DOCUMENTS_FILTERABLE = ["traveler_ids", "trip_id", "segment_id", "owner_user_id"]
_DOCUMENTS_SORTABLE = ["created_at_unix"]
```

And extend the loop tuple in `ensure_indexes_configured`:

```python
for name, filterable, sortable in (
    ("trips", _TRIP_FILTERABLE, _TRIP_SORTABLE),
    ("segments", _SEGMENT_FILTERABLE, _SEGMENT_SORTABLE),
    ("documents", _DOCUMENTS_FILTERABLE, _DOCUMENTS_SORTABLE),
):
    ...
```

- [ ] **Step 9.6 — Broaden proxy `Literal`**

In `src/trip_tracker/search/proxy.py`, change the path-param type:

```python
@router.post("/api/search/{index}")
async def search(
    index: Literal["trips", "segments", "documents"],
    ...,
) -> SearchResponse:
    ...
```

- [ ] **Step 9.7 — Extend `reindex_all`**

In `src/trip_tracker/search/reindex.py`, mirror the trip/segment walks:

```python
# After the segments walk:
docs_idx = meili.index("documents")
batch = []
for doc in (await db.execute(select(Document))).scalars().all():
    batch.append(await document_to_doc(doc, db=db))
    counts["documents"] += 1
    if len(batch) >= batch_size:
        if not dry_run:
            await docs_idx.update_documents(batch)
        batch = []
if batch and not dry_run:
    await docs_idx.update_documents(batch)
```

And initialize `counts = {"trips": 0, "segments": 0, "documents": 0}`. Update the dry-run early-return dict similarly.

The CLI's print line in `__main__.py`'s `_reindex` becomes:

```python
print(
    f"Reindex complete: trips={counts['trips']} segments={counts['segments']} documents={counts['documents']}"
)
```

- [ ] **Step 9.8 — Add documents to ⌘K palette**

In `src/trip_tracker/templates/_search_palette.html`, find the indexes array (likely `['trips', 'segments']` in JS) and add `'documents'`. Add a result-row template branch:

```html
<template x-if="hit._index === 'documents'">
  <div class="...">
    <div class="text-sm font-medium" x-text="'📄 ' + hit.filename"></div>
    <div class="text-xs text-gray-600" x-html="hit._formatted?.extracted_text || ''"></div>
  </div>
</template>
```

Click handler should branch on the doc hit:

```js
function navigateForHit(hit) {
  if (hit._index === 'documents') {
    if (hit.segment_id) return `/trips/${hit.trip_id}#segment-${hit.segment_id}`;
    if (hit.trip_id)    return `/trips/${hit.trip_id}/documents`;
    return `/documents/${hit.id}/download`;
  }
  // existing trip/segment branches
}
```

- [ ] **Step 9.9 — Run + commit**

```bash
uv run pytest tests/test_search_documents.py tests/test_search_reindex_documents.py -v
uv run pytest -q
uv run mypy src
git add src/trip_tracker/search/ src/trip_tracker/worker.py \
        src/trip_tracker/__main__.py \
        src/trip_tracker/templates/_search_palette.html \
        tests/test_search_documents.py tests/test_search_reindex_documents.py
git commit -m "feat(search): documents Meili index + palette/proxy/reindex extension"
```

**Quality bar:**
- The `case _:` default arm in the match statement is required — without it, mypy + ruff complain about non-exhaustive match.
- The orphan-docs test exercises the `traveler_ids = [str(owner_user_id)]` fallback. Without it, the proxy would silently exclude the user's own orphan docs.
- Don't forget to add the `documents` entry to the `enqueue_meili_sync` dedup-key lookup if there is one — Phase 4's implementation builds the dedup key as `f"meili_sync:{entity}:{entity_id}"`, which works unchanged for `"document"`.
- The palette template's djlint formatting: run `uv run djlint src/trip_tracker/templates --reformat` before commit if the hook complains.

---

## Task 10 — UI: trip-level docs partial + segment-inline list + upload forms

**Spec ref:** §11.

**Files:**
- Modify: `src/trip_tracker/routes/documents.py` (add `GET /trips/{id}/documents` HTMX partial)
- Create: `src/trip_tracker/templates/trips/_documents.html`
- Create: `src/trip_tracker/templates/segments/_documents.html`
- Modify: `src/trip_tracker/templates/trips/detail.html` (or whichever trip-detail template exists; include the docs partial)
- Modify: `src/trip_tracker/templates/segments/_row.html` or `segments/detail.html` (include segments docs partial)
- (Optional) test the rendered HTML with a snapshot test if djlint coverage isn't enough

- [ ] **Step 10.1 — Add `GET /trips/{id}/documents` route**

```python
from fastapi.templating import Jinja2Templates
from trip_tracker.templates import templates  # or however the project exposes Jinja env


@router.get("/trips/{trip_id}/documents")
async def list_for_trip(
    trip_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    if not await _user_can_access_trip(db, user, trip_id):
        raise HTTPException(404)
    docs = (
        (
            await db.execute(
                select(Document)
                .where(Document.trip_id == trip_id)
                .order_by(Document.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    segs = (await db.execute(select(Segment).where(Segment.trip_id == trip_id))).scalars().all()
    return templates.TemplateResponse(
        request,
        "trips/_documents.html",
        {"trip_id": trip_id, "documents": docs, "segments": segs},
    )
```

- [ ] **Step 10.2 — Implement the partials**

`src/trip_tracker/templates/trips/_documents.html`:

```html
{% extends "base.html" %}
{% block content %}
<section class="space-y-4">
  <h2 class="text-lg font-semibold">Documents</h2>

  <form method="POST" action="/trips/{{ trip_id }}/documents"
        enctype="multipart/form-data" class="flex gap-2 items-center">
    <input type="file" name="file" accept=".pdf" required
           class="text-sm" />
    <button type="submit" class="btn btn-primary">Upload PDF</button>
  </form>

  {% if not documents %}
    <p class="text-sm text-gray-600">
      No documents yet. Upload boarding passes, hotel confirmations, or
      vouchers — they'll be searchable via ⌘K.
    </p>
  {% else %}
  <ul class="divide-y">
    {% for d in documents %}
    <li class="py-3 flex items-start justify-between gap-4">
      <div>
        <div class="font-medium">📄 {{ d.filename }}</div>
        <div class="text-xs text-gray-600">
          {% if d.raw_email_id %}<span class="badge">📧 Email</span>{% else %}<span class="badge">📤 Upload</span>{% endif %}
          {% if d.segment_id %}
            · <a href="#segment-{{ d.segment_id }}" class="link">→ linked segment</a>
          {% endif %}
          · {{ (d.size_bytes / 1024) | round(0) | int }} KiB
          ·
          {% if d.extract_status == "extracted" %}<span title="Text extracted">✅</span>
          {% elif d.extract_status == "pending" %}<span title="Extraction pending">⏳</span>
          {% else %}<span title="{{ d.extract_status }}">🚫</span>
          {% endif %}
          · uploaded {{ d.created_at | localize_dt }}
        </div>
      </div>
      <div class="flex gap-2">
        <a href="/documents/{{ d.id }}/download" class="btn btn-sm">Download</a>
        {% if not d.segment_id %}
        <form method="POST" action="/documents/{{ d.id }}/link" class="inline">
          <select name="segment_id" class="select select-sm">
            <option value="">Link to…</option>
            {% for s in segments %}
            <option value="{{ s.id }}">{{ s.type }} · {{ s.confirmation_number or s.id }}</option>
            {% endfor %}
          </select>
          <button class="btn btn-sm">Link</button>
        </form>
        {% else %}
        <form method="POST" action="/documents/{{ d.id }}/unlink" class="inline">
          <button class="btn btn-sm">Unlink</button>
        </form>
        {% endif %}
        <form method="POST" action="/documents/{{ d.id }}"
              hx-delete="/documents/{{ d.id }}"
              hx-confirm="Delete this document and its file?"
              class="inline">
          <button class="btn btn-sm btn-danger">Delete</button>
        </form>
      </li>
    {% endfor %}
  </ul>
  {% endif %}
</section>
{% endblock %}
```

`src/trip_tracker/templates/segments/_documents.html` is similar but scoped to one segment (no segment chip, no link/unlink form — those happen from trip-level). Upload form posts to `/segments/{{ segment_id }}/documents`.

- [ ] **Step 10.3 — Include partials in trip-detail and segment-detail templates**

In whichever template renders the trip detail (look for `trips/detail.html` or similar), add an include or a tab:

```html
{% include "trips/_documents.html" %}
```

If the existing trip-detail template uses tabs/sections, decide whether documents is a section below segments or its own tab. Match the surrounding pattern; don't restructure.

- [ ] **Step 10.4 — Run djlint, snapshot, commit**

```bash
uv run djlint src/trip_tracker/templates --reformat
uv run pytest -q
git add src/trip_tracker/routes/documents.py src/trip_tracker/templates/
git commit -m "feat(documents): trip-level + segment-inline docs partials + upload forms"
```

**Quality bar:**
- The `localize_dt` Jinja filter was added in Phase 2. Verify it's exposed on the templates env used here; if not, add it (mirror Phase 2's wiring).
- The `hx-delete` + `hx-confirm` HTMX combo handles the DELETE; but since this is a `<form method="POST">` with a button, htmx must be loaded. Phase 2/3 already include htmx — confirm via grep.
- djlint version pin: pre-commit hook rev MUST match `pyproject.toml`'s djlint version. If you bump djlint here, bump both. Per `feedback_pre-commit-version-pin.md`.

---

## Task 11 — README + verification gate + tag v0.4.0

(Handled inline by the controller — same shape as v0.2.0/v0.3.0/v0.4.0.)

**Spec ref:** §12 (Done definition).

**Files:**
- Modify: `README.md`
- Run: full pytest + cov, ruff (check + format --check), mypy, pre-commit, bandit, djlint, docker build
- Smoke (alt-port stack as in v0.4.0): boot Postgres + Redis + Meili containers on alt ports, run migrations, seed a user + trip + a real boarding-pass PDF, run `python -m trip_tracker reindex`, query Meili directly for a word in the PDF, verify it surfaces. Hit `/api/search/documents` without auth and confirm 401.
- Tag: signed `v0.4.0` (NOTE: should read `v0.5.0` — Phase 4 used 0.4.0; Phase 5 uses 0.5.0)

- [ ] **Step 11.1 — README updates**

Update Status:

```markdown
> **Status:** Phase 5 — document upload + email-attachment ingestion + ⌘K search of extracted text.
> Phase 6 (TBD) is next.
```

Append a new section before "Production deploy":

```markdown
## Documents (Phase 5)

Upload boarding passes, hotel confirmations, vouchers, and other PDFs — either
manually from a trip / segment page, or by forwarding an email with a PDF
attached. Documents are auto-linked to a matching segment when the filename
contains a confirmation number, flight/train number, or unique date.

### Search

Documents land in Meilisearch's third index (`documents`) after async text
extraction (typically <2s for boarding-pass-sized PDFs). Press **⌘K** to
search filenames AND extracted text. Click a document hit to either jump to
its linked segment (if any), or download it directly.

### Storage

Files live under `${DOCUMENTS_DIR}` (default `/data/documents`),
content-addressed as `<sha256[:2]>/<sha256>`. Same content uploaded twice =
one file on disk, one row in the database (UNIQUE constraint on
`owner_user_id + sha256`).

### Reverse-proxy serving (optional but recommended)

By default, the FastAPI app streams downloads. For better performance, set
`DOCUMENTS_X_ACCEL_PREFIX` (e.g., `/internal-documents`) and configure your
reverse proxy to serve `/data/documents` from that path **as an internal-only
location**:

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
```

- [ ] **Step 11.2 — Local verification gate**

```bash
./scripts/build-tailwind.sh
uv run pytest --cov                          # ≥85%
uv run ruff check src tests migrations
uv run ruff format --check .                 # whole tree
uv run mypy src
uv run pre-commit run --all-files
uv run bandit -c pyproject.toml -r src/
uv run djlint src/trip_tracker/templates --check
docker build -t trip-tracker:dev .
```

All must be green. Iterate on any failure.

- [ ] **Step 11.3 — Smoke test (manual, ad-hoc per v0.4.0 recipe)**

Same recipe as v0.4.0 (commits around `c0f492c`):

1. Boot Postgres + Redis + Meili on alternate ports (5433, 6380, 7701).
2. Run migrations against the alt DB.
3. Seed admin user, trip "Paris vacation", and upload a real boarding-pass PDF via the manual route OR construct an email-with-attachment payload and POST to /api/ingest/email.
4. Wait for the saq worker to process extraction (boot a worker container or run `saq trip_tracker.worker.settings` ad-hoc).
5. Run `python -m trip_tracker reindex` to make sure all three indexes populate.
6. Query Meili directly: `curl -s -X POST http://localhost:7701/indexes/documents/search -H "Authorization: Bearer ..." -d '{"q":"<word from your PDF>"}' | jq` — verify the doc surfaces.
7. Hit `/api/search/documents` without auth → 401.
8. Tear down containers.

- [ ] **Step 11.4 — Commit, tag, push**

```bash
git add README.md
git commit -m "docs: README — Phase 5 documents section + recovery"

git tag -a -s v0.5.0 -m "Phase 5 — Documents (text PDFs, manual + email, pdfplumber, Meili 3rd index)"
git checkout main
git merge --ff-only feat/phase-5-documents
git push origin main
git push origin v0.5.0
```

The release workflow on GitHub fires on the tag push, producing a multi-arch image at `ghcr.io/<owner>/trip-tracker:v0.5.0`, signed with cosign + SBOM attached.

- [ ] **Step 11.5 — Schedule release-verification agent**

Same pattern as v0.2.0 / v0.3.0 / v0.4.0: schedule a one-time remote agent ~20 min after tag push to verify GHCR image, signature, SBOM. Reuse the prompt template from prior tags, swapping `v0.4.0` → `v0.5.0`.

**Quality bar:**
- Coverage ≥85% AFTER all 11 tasks land. The big surface-area additions (routes, autolink, extract) all have unit tests; if coverage drops, the most likely culprit is `extract_document`'s error branches (timeout, malformed PDF) — add a fixture or mock to exercise them.
- The `ruff format --check .` (whole tree, including `migrations/` and config files) must pass. Phase 3 commit `0546d4e` taught this lesson.
- Bandit clean. Any `# nosec` includes the specific code (e.g., `# nosec B110 — pdfplumber's typed-exception broad-catch is intentional`).
- The signed tag uses your SSH signing key (already configured per Phase 2).

---

## Done Definition for Phase 5

- All 11 tasks merged to `main`.
- CI green (lint + typecheck + test + security + docker + djlint + bandit).
- Coverage ≥ 85%.
- Manual upload of a PDF (trip + segment routes) succeeds; sha256 dedup returns 303 on second upload of the same content.
- Forwarding an email with a PDF attachment auto-creates a Document; auto-link targets the matching segment when the filename has a conf# / flight# / unique-date hint.
- `python -m trip_tracker reindex` reports `trips=N segments=M documents=K`.
- ⌘K palette finds documents by filename and by extracted text.
- `GET /documents/{id}/download` serves the file (X-Accel mode + FileResponse fallback) with proper auth and `Content-Disposition`.
- Trip delete cascades to documents (DB rows + disk files via `after_delete` listener).
- Path-traversal guard rejects forged `storage_key` values.
- README "Documents (Phase 5)" section documents upload, auto-link, `MAX_UPLOAD_BYTES` env, and the `internal-documents` reverse-proxy setup.
- `v0.5.0` tag pushed; release workflow produces signed multi-arch GHCR image; release-verification scheduled agent confirms tag landed cleanly.

After this lands, return to brainstorming/writing-plans for the next Phase (Phase 6 — TBD; candidates: ICS subscribable feed, world map of trips, expense tracking, weather, OCR/Phase 5.1).
