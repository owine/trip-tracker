"""Document extraction saq task. Spec §8."""

from __future__ import annotations

import asyncio
import io
import logging
import uuid
from typing import Any

import pdfplumber
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

    doc_uuid = uuid.UUID(document_id)
    async with SM() as db:
        doc = await db.get(Document, doc_uuid)
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
        except Exception as exc:  # deliberate broad catch for resilience
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
        final_status = doc.extract_status

    # Enqueue Meili sync (only when there's text to index).
    if final_status in ("extracted", "empty"):
        await enqueue_meili_sync(settings, entity="document", entity_id=doc_uuid)
