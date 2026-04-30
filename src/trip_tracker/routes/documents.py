"""Document upload / link / unlink / delete routes. Spec §6.1."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
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
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User

router = APIRouter()
_logger = logging.getLogger(__name__)


def _build_queue(settings: Settings) -> Queue:
    """Factory for the saq Queue. Indirected so tests can monkeypatch it."""
    return Queue.from_url(str(settings.redis_url))


def _storage_dep(settings: Settings = Depends(get_settings)) -> StorageBackend:  # noqa: B008
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


async def _get_owned(db: AsyncSession, doc_id: uuid.UUID, user: User) -> Document:
    doc = (await db.execute(select(Document).where(Document.id == doc_id))).scalar_one_or_none()
    if doc is None:
        raise HTTPException(404)
    if doc.owner_user_id != user.id:
        raise HTTPException(403)
    return doc
