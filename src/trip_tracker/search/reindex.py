"""Full Meili rebuild from Postgres. Idempotent; called from
`python -m trip_tracker reindex`.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from trip_tracker.models.document import Document
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.search.client import (
    MeiliClientProtocol,
    ensure_indexes_configured,
)
from trip_tracker.search.sync import document_to_doc, segment_to_doc, trip_to_doc

logger = logging.getLogger(__name__)


async def reindex_all(
    engine: AsyncEngine,
    meili: MeiliClientProtocol,
    *,
    batch_size: int = 100,
    dry_run: bool = False,
) -> dict[str, int]:
    """Walk Trips + Segments, batch-upsert. Returns a count dict."""
    SessionMaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    if not dry_run:
        for name in ("trips", "segments", "documents"):
            with contextlib.suppress(Exception):  # missing index is fine
                await meili.delete_index(name)
        await ensure_indexes_configured(meili)

    counts = {"trips": 0, "segments": 0, "documents": 0}

    async with SessionMaker() as db:
        trips_idx = meili.index("trips")
        batch: list[dict[str, Any]] = []
        for trip in (await db.execute(select(Trip))).scalars().all():
            batch.append(await trip_to_doc(trip, db=db))
            counts["trips"] += 1
            if len(batch) >= batch_size:
                if not dry_run:
                    await trips_idx.update_documents(batch)
                batch = []
        if batch and not dry_run:
            await trips_idx.update_documents(batch)

        seg_idx = meili.index("segments")
        batch = []
        for seg in (await db.execute(select(Segment))).scalars().all():
            batch.append(await segment_to_doc(seg, db=db))
            counts["segments"] += 1
            if len(batch) >= batch_size:
                if not dry_run:
                    await seg_idx.update_documents(batch)
                batch = []
        if batch and not dry_run:
            await seg_idx.update_documents(batch)

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

    if dry_run:
        return {"trips": 0, "segments": 0, "documents": 0}
    logger.info("reindex complete: %s", counts)
    return counts
