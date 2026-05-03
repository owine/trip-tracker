"""saq worker: parses RawEmail rows in the background.

Runs in a separate container from the FastAPI app, sharing the same image:
    command: ["saq", "trip_tracker.worker.settings"]

Single task `parse_raw_email(raw_email_id)` is enqueued by the webhook
handler after RawEmail is committed.
"""

from __future__ import annotations

import logging
import uuid
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as email_policy_default
from pathlib import Path
from typing import Any

from redis.asyncio import Redis as AsyncRedis
from saq import Queue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import trip_tracker.parsers.vendors  # noqa: F401  # register all packs
from trip_tracker.config import WorkerSettings
from trip_tracker.documents.extract import extract_document
from trip_tracker.documents.persist import persist_pdf_attachments
from trip_tracker.documents.storage import LocalFsStorage
from trip_tracker.models.forwarding_alias import ForwardingAlias
from trip_tracker.models.raw_email import RawEmail
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.parsers.base import SegmentDraft
from trip_tracker.parsers.budget import cost_cents_for_usage, record_usage
from trip_tracker.parsers.cluster import cluster_for_user, derive_destination
from trip_tracker.parsers.dedup import find_existing_segment
from trip_tracker.parsers.dispatch import dispatch_parse
from trip_tracker.parsers.llm import LLMClient
from trip_tracker.search.client import MeiliClientProtocol, build_client
from trip_tracker.search.sync import enqueue_meili_sync, segment_to_doc, trip_to_doc
from trip_tracker.weather.cache import set_cached
from trip_tracker.weather.client import fetch_forecast

logger = logging.getLogger(__name__)


def _build_doc_queue(settings: WorkerSettings) -> Queue:
    """Factory for the saq Queue used by doc-extract enqueuing. Indirected for tests."""
    return Queue.from_url(str(settings.redis_url))


async def _enqueue_doc_extracts(settings: WorkerSettings, new_doc_ids: list[uuid.UUID]) -> None:
    """Enqueue extract_document for each newly-persisted document id.

    Called AFTER db.commit() so the worker picking up the task can see the row.
    Failure is logged but not propagated — the document row stays in 'pending'
    and a future re-run will retry.
    """
    if not new_doc_ids:
        return
    q = _build_doc_queue(settings)
    try:
        for doc_id in new_doc_ids:
            await q.enqueue("extract_document", document_id=str(doc_id))
    except Exception as exc:  # Redis blip shouldn't fail the parse task
        logger.warning("_enqueue_doc_extracts failed: %s", exc)
    finally:
        await q.disconnect()


async def parse_raw_email(ctx: dict[str, Any], *, raw_email_id: str) -> None:
    """Parse one RawEmail and persist the result.

    Idempotent: re-running on an already-parsed RawEmail is a no-op.
    saq passes kwargs through `ctx` for the function's keyword args (note
    the kw-only signature). Engine and settings live in the worker context.

    TODO (Phase 3.5): the Inbox `reask` route stores a hint in
    raw.headers['X-Tt-Hint']. Pass it through to dispatch_parse here so the
    LLM picks up the user's correction. v0.3.0 ships without this propagation.
    """
    settings: WorkerSettings = ctx["settings"]
    # Use the engine populated by startup() in production. Tests may inject
    # their own engine via ctx["engine"]. Either way, the engine is owned by
    # the caller — don't dispose it here.
    engine = ctx["engine"]
    SessionMaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    rid = uuid.UUID(raw_email_id)
    async with SessionMaker() as db:
        raw = await db.get(RawEmail, rid)
        if raw is None:
            logger.warning("RawEmail %s not found", raw_email_id)
            return
        if raw.parse_status != "pending":
            logger.info(
                "RawEmail %s already parsed (%s); skipping",
                raw_email_id,
                raw.parse_status,
            )
            return

        local_part = raw.to_address.split("@", 1)[0].lower()
        owner = (
            await db.execute(
                select(ForwardingAlias).where(ForwardingAlias.local_part == local_part)
            )
        ).scalar_one_or_none()
        if owner is None:
            logger.info("no alias for %s — marking no_segments", raw.to_address)
            raw.parse_status = "no_segments"
            await db.commit()
            return

        # Phase 5: persist PDF attachments now that we have owner.user_id.
        # Auto-link is deferred to Task 7 (runs after segment dispatch); this
        # call leaves segment_id / trip_id NULL on each new Document row.
        new_doc_ids = await persist_pdf_attachments(
            db,
            settings,
            raw_email_id=raw.id,
            owner_user_id=owner.user_id,
            body=raw.mime_blob,
        )

        _msg = BytesParser(policy=email_policy_default).parsebytes(raw.mime_blob)
        assert isinstance(_msg, EmailMessage), (
            "BytesParser with default policy returns EmailMessage"
        )  # nosec B101
        msg = _msg
        client = LLMClient(settings)
        outcome = await dispatch_parse(
            msg,
            llm_client=client,
            db=db,
            cap_cents=settings.llm_daily_budget_cents,
        )

        if outcome.result.source == "llm:haiku-4-5":
            await record_usage(
                db,
                cost_cents=cost_cents_for_usage(
                    input_tokens=outcome.llm_input_tokens,
                    output_tokens=outcome.llm_output_tokens,
                ),
            )

        if not outcome.result.segments:
            raw.parse_status = "no_segments"
            await db.commit()
            # Phase 5: enqueue PDF extraction even when there are no segments —
            # an email can carry a boarding-pass PDF the parser doesn't recognise.
            await _enqueue_doc_extracts(settings, new_doc_ids)
            return

        # Phase 9 Track A: dedup gate. Partition drafts into matched (existing
        # segment found via strong/medium rules) and fresh. All-matched →
        # 'duplicate' + early return; partial → 'review' + persist fresh only.
        matched: list[tuple[SegmentDraft, Segment]] = []
        fresh: list[SegmentDraft] = []
        for d in outcome.result.segments:
            existing = await find_existing_segment(db, owner.user_id, d)
            if existing is not None:
                matched.append((d, existing))
            else:
                fresh.append(d)

        if not fresh:
            # All drafts were duplicates — no segments to persist, no auto-Expense.
            raw.parse_status = "duplicate"
            # JSONB columns: rebind, don't mutate in place (SQLAlchemy needs a new
            # dict reference to mark the column dirty).
            raw.headers = {
                **(raw.headers or {}),
                "X-Tt-Dedup-Against": [str(s.id) for _, s in matched],
            }
            await db.commit()
            # Re-forwarded boarding-pass PDFs should still extract their text.
            await _enqueue_doc_extracts(settings, new_doc_ids)
            return

        if matched:
            raw.headers = {
                **(raw.headers or {}),
                "X-Tt-Dedup-Partial": [
                    {
                        "existing_id": str(s.id),
                        "draft_type": d.type,
                        "draft_start_at": d.start_at.isoformat(),
                    }
                    for d, s in matched
                ],
            }

        created_segments: list[Segment] = []
        trips_to_sync: set[uuid.UUID] = set()

        for draft in fresh:
            decision = await cluster_for_user(db, owner.user_id, draft)
            trip_id: uuid.UUID | None
            if decision.kind == "create_new":
                trip = Trip(
                    title=decision.auto_title or "Trip",
                    start_date=draft.start_at.date(),
                    end_date=(draft.end_at or draft.start_at).date(),
                    primary_destination=derive_destination(draft),
                    created_by=owner.user_id,
                )
                db.add(trip)
                await db.flush()
                db.add(TripTraveler(trip_id=trip.id, user_id=owner.user_id, role="owner"))
                trip_id = trip.id
                trips_to_sync.add(trip.id)
            else:
                # decision.kind in {"attach", "ambiguous"}: both carry a trip_id
                # for the best-scoring candidate. Ambiguous cases land on that
                # candidate but with parse_status='review' (set later from the
                # confidence floor or partial-dedup path) so the user can
                # reassign in /inbox if the auto-pick was wrong.
                trip_id = decision.trip_id

            seg = Segment(
                trip_id=trip_id,
                owner_user_id=owner.user_id,
                type=draft.type,
                status=draft.status,
                confirmation_number=draft.confirmation_number,
                provider=draft.provider,
                start_at=draft.start_at,
                start_tz=draft.start_tz,
                end_at=draft.end_at,
                end_tz=draft.end_tz,
                start_location=draft.start_location,
                end_location=draft.end_location,
                details=draft.details,
                parse_source=outcome.result.source,
                parse_confidence=outcome.result.confidence,
                raw_email_id=raw.id,
            )
            db.add(seg)
            created_segments.append(seg)

        if matched:
            # Partial dedup → always review so the user sees the partial.
            raw.parse_status = "review"
        elif outcome.result.confidence < settings.llm_confidence_floor:
            raw.parse_status = "review"
        else:
            raw.parse_status = "parsed"
        await db.commit()

        # Phase 5 Task 7: link previously-persisted attachment Documents to the
        # segments we just committed. The heuristic operates on filename
        # substrings (confirmation #, flight/train #, unique date) and skips
        # already-manually-linked rows.
        from trip_tracker.documents.autolink import autolink_pending_for_email

        await autolink_pending_for_email(db, raw_email_id=raw.id)

        # Phase 5: enqueue PDF extraction for newly-attached documents AFTER the
        # commit so the worker that picks up the task can see the row.
        await _enqueue_doc_extracts(settings, new_doc_ids)

        for s in created_segments:
            await enqueue_meili_sync(settings, entity="segment", entity_id=s.id)
        for tid in trips_to_sync:
            await enqueue_meili_sync(settings, entity="trip", entity_id=tid)


async def sync_meili(ctx: dict[str, Any], *, entity: str, entity_id: str) -> None:
    """Upsert one Trip or Segment to Meili. On delete from Postgres, the
    entity is gone — issue a Meili delete instead."""
    engine = ctx["engine"]
    meili: MeiliClientProtocol = ctx["meili"]
    SessionMaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    rid = uuid.UUID(entity_id)
    async with SessionMaker() as db:
        if entity == "trip":
            trip_row: Trip | None = await db.get(Trip, rid)
            if trip_row is None:
                await meili.index("trips").delete_document(str(rid))
            else:
                doc = await trip_to_doc(trip_row, db=db)
                await meili.index("trips").update_documents([doc])
        elif entity == "segment":
            seg_row: Segment | None = await db.get(Segment, rid)
            if seg_row is None:
                await meili.index("segments").delete_document(str(rid))
            else:
                doc = await segment_to_doc(seg_row, db=db)
                await meili.index("segments").update_documents([doc])
        elif entity == "document":
            from trip_tracker.models.document import Document
            from trip_tracker.search.sync import document_to_doc

            doc_row: Document | None = await db.get(Document, uuid.UUID(entity_id))
            if doc_row is None:
                await meili.index("documents").delete_document(entity_id)
            else:
                payload = await document_to_doc(doc_row, db=db)
                await meili.index("documents").update_documents([payload])
        else:
            raise ValueError(f"unknown entity: {entity}")


async def refresh_weather(ctx: dict[str, Any], *, lat: float, lon: float) -> None:
    """saq task: pull a fresh Open-Meteo forecast and cache it. Idempotent.

    Cache the result under the REQUESTED (lat, lon), not the response's
    nearest-station coords — Open-Meteo snaps to the nearest station, which
    can shift coordinates by ~0.02°. If we cached under response coords,
    the next page-render lookup with original coords would miss every time.
    Spec §6.4.
    """
    redis = ctx["redis"]
    forecast = await fetch_forecast(lat, lon)
    await set_cached(forecast, redis, request_lat=lat, request_lon=lon)


async def startup(ctx: dict[str, Any]) -> None:
    """Build worker-process singletons. saq calls this once when the worker boots."""
    s = WorkerSettings()
    ctx["settings"] = s
    # Build one engine per worker process (not per task) so the connection
    # pool is reused across thousands of jobs. Disposed in shutdown().
    ctx["engine"] = create_async_engine(str(s.database_url))
    ctx["meili"] = build_client(s)
    ctx["storage"] = LocalFsStorage(Path(s.documents_dir))
    ctx["redis"] = AsyncRedis.from_url(s.redis_url)  # NEW for Phase 7


async def shutdown(ctx: dict[str, Any]) -> None:
    """Dispose the engine on graceful shutdown."""
    engine = ctx.get("engine")
    if engine is not None:
        await engine.dispose()
    redis = ctx.get("redis")
    if redis is not None:
        await redis.aclose()  # NEW for Phase 7


# Read settings once at module import. Worker fails fast if env is incomplete.
_SETTINGS = WorkerSettings()
queue = Queue.from_url(_SETTINGS.redis_url)

# saq picks up `settings` (a dict) when invoked via `saq trip_tracker.worker.settings`.
settings = {
    "queue": queue,
    "functions": [parse_raw_email, sync_meili, extract_document, refresh_weather],
    "startup": startup,
    "shutdown": shutdown,
    "concurrency": 1,  # one task at a time per worker; matches arq's effective default
    # Retries: per-task `retries=5` set at enqueue time — see webhook.py.
}
