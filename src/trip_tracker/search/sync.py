"""Doc rendering + enqueue helper for the Meili sync subsystem.

Pure functions live here. The saq task that calls them lives in worker.py.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Literal

from saq import Queue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.config import WorkerSettings
from trip_tracker.models.document import Document
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler

_EPOCH = date(1970, 1, 1)


async def _trip_traveler_ids(db: AsyncSession, trip_id: uuid.UUID) -> list[str]:
    rows = (
        (await db.execute(select(TripTraveler.user_id).where(TripTraveler.trip_id == trip_id)))
        .scalars()
        .all()
    )
    return [str(uid) for uid in rows]


async def trip_to_doc(trip: Trip, *, db: AsyncSession) -> dict[str, Any]:
    """Render a Trip ORM row to its Meili index doc."""
    return {
        "id": str(trip.id),
        "title": trip.title,
        "primary_destination": trip.primary_destination,
        "start_date": (trip.start_date - _EPOCH).days,
        "end_date": (trip.end_date - _EPOCH).days,
        "traveler_ids": await _trip_traveler_ids(db, trip.id),
    }


def _vehicle_number(seg: Segment) -> str | None:
    """Flatten flight_number or train_number from JSONB details, or None."""
    details = seg.details or {}
    if seg.type == "flight":
        return details.get("flight_number")
    if seg.type == "train":
        return details.get("train_number")
    return None


def _city_from_location(loc: dict[str, Any] | None) -> str | None:
    if not loc:
        return None
    return loc.get("city")


async def segment_to_doc(seg: Segment, *, db: AsyncSession) -> dict[str, Any]:
    """Render a Segment ORM row to its Meili index doc."""
    details = seg.details or {}
    return {
        "id": str(seg.id),
        "trip_id": str(seg.trip_id) if seg.trip_id else None,
        "traveler_ids": (await _trip_traveler_ids(db, seg.trip_id) if seg.trip_id else []),
        "type": seg.type,
        "provider": seg.provider,
        "confirmation_number": seg.confirmation_number,
        "start_at_unix": int(seg.start_at.timestamp()),
        "start_city": _city_from_location(seg.start_location),
        "end_city": _city_from_location(seg.end_location),
        "vehicle_number": _vehicle_number(seg),
        "notes": details.get("notes"),
    }


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
        # Orphan: surface only to the owner.
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


def _build_queue(settings: WorkerSettings) -> Queue:
    """Factory for the saq Queue. Indirected so tests can monkeypatch it."""
    return Queue.from_url(settings.redis_url)


async def enqueue_meili_sync(
    settings: WorkerSettings,
    *,
    entity: Literal["trip", "segment", "document"],
    entity_id: uuid.UUID,
) -> None:
    """Enqueue a sync_meili saq job.

    NOTE: do NOT pass `unique=True` here — saq 0.26's `Queue.enqueue` treats
    only `Job.__dataclass_fields__` keys as job metadata; `unique` is not
    one of them and would fall through to the worker function as a kwarg,
    causing `TypeError: sync_meili() got an unexpected keyword argument
    'unique'`. `key=...` is set so log lookups can find the job; if perfect
    in-flight dedup matters, route through saq's `apply()` API instead.
    """
    q = _build_queue(settings)
    try:
        await q.enqueue(
            "sync_meili",
            entity=entity,
            entity_id=str(entity_id),
            key=f"meili_sync:{entity}:{entity_id}",
            retries=5,
        )
    finally:
        await q.disconnect()
