"""Persist PDF email attachments. Spec §6.2.

Called from `parse_raw_email` (worker.py) after alias resolution succeeds.
The autolink step (Task 7) runs separately, after segments are committed,
so this function leaves segment_id and trip_id NULL.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import column, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.config import WorkerSettings
from trip_tracker.documents.helpers import is_pdf, sha256_hex
from trip_tracker.documents.storage import LocalFsStorage
from trip_tracker.ingest.attachments import extract_attachments
from trip_tracker.models.document import Document

_logger = logging.getLogger(__name__)


async def persist_pdf_attachments(
    db: AsyncSession,
    settings: WorkerSettings,
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
    enqueuing extract_document for each returned id AFTER the commit.
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
            await storage.put(sha, att.payload)
            new_ids.append(row.id)
        # else: existing row — UPSERT just updated raw_email_id; no new file.
    return new_ids
