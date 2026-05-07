"""Document upload / link / unlink / delete routes. Spec §6.1."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from saq import Queue
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.deps import get_settings, require_user
from trip_tracker.config import Settings
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
from trip_tracker.models.user import User
from trip_tracker.templating import register_globals

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
register_globals(templates)


def _localize_dt(dt: datetime, tz: str = "UTC", fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Render a UTC-stored datetime in the segment's local tz.

    Mirrors the same filter from routes/trips.py — each route module needs its
    own filter registration since Jinja2Templates instantiations don't share
    filter state.
    """
    from zoneinfo import ZoneInfo

    if dt.tzinfo is None:
        from datetime import UTC

        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ZoneInfo(tz)).strftime(fmt)


templates.env.filters["localize_dt"] = _localize_dt
_logger = logging.getLogger(__name__)


def _build_queue(settings: Settings) -> Queue:
    """Factory for the saq Queue. Indirected so tests can monkeypatch it."""
    return Queue.from_url(str(settings.redis_url))


def _storage_dep(settings: Settings = Depends(get_settings)) -> StorageBackend:  # noqa: B008
    return LocalFsStorage(settings.documents_dir)


async def _user_can_access_trip(db: AsyncSession, user: User, trip_id: uuid.UUID) -> bool:  # noqa: ARG001
    return (
        await db.execute(select(Trip.id).where(Trip.id == trip_id))
    ).scalar_one_or_none() is not None


@router.get("/trips/{trip_id}/documents", response_class=HTMLResponse)
async def list_for_trip(
    trip_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    if not await _user_can_access_trip(db, user, trip_id):
        raise HTTPException(404)
    trip = (
        await db.execute(select(Trip).where(Trip.id == trip_id, Trip.merged_into_id.is_(None)))
    ).scalar_one_or_none()
    if trip is None:
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
        {
            "trip_id": trip_id,
            "trip": trip,
            "documents": docs,
            "segments": segs,
            "user": user,
        },
    )


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
        # Enqueue extraction (saq dispatches by string name; function lives in worker — Task 8)
        q = _build_queue(settings)
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
    if "session" in request.scope:
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
    target = f"/trips/{seg.trip_id}#segment-{segment_id}"
    if is_new:
        return RedirectResponse(target, status_code=303)
    if "session" in request.scope:
        request.session["flash"] = f"Already uploaded as {doc.filename}"
    return RedirectResponse(target, status_code=303)


@router.post("/documents/{document_id}/link")
async def link_to_segment(
    document_id: uuid.UUID,
    segment_id: uuid.UUID = Form(...),  # noqa: B008
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
    # The "document" arm of sync_meili is added in Task 9 — for now this enqueues
    # a job that will warn-log "unknown entity" until Task 9 lands.
    from trip_tracker.search.sync import enqueue_meili_sync

    await enqueue_meili_sync(settings, entity="document", entity_id=doc.id)
    await db.delete(doc)
    await db.commit()
    return Response(status_code=204)


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
    if path is None:
        raise HTTPException(503, detail="storage backend does not support direct streaming")
    return FileResponse(path, media_type=doc.mime_type, filename=doc.filename)


async def _can_access_doc(db: AsyncSession, user: User, doc: Document) -> bool:
    """Owner OR trip-traveler can read."""
    if doc.owner_user_id == user.id:
        return True
    if doc.trip_id is None:
        return False
    return await _user_can_access_trip(db, user, doc.trip_id)


async def _get_owned(db: AsyncSession, doc_id: uuid.UUID, user: User) -> Document:
    doc = (await db.execute(select(Document).where(Document.id == doc_id))).scalar_one_or_none()
    if doc is None:
        raise HTTPException(404)
    if doc.owner_user_id != user.id:
        raise HTTPException(403)
    return doc
