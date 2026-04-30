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
from typing import Any

from saq import Queue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import trip_tracker.parsers.vendors  # noqa: F401  # register all packs
from trip_tracker.config import Settings
from trip_tracker.models.forwarding_alias import ForwardingAlias
from trip_tracker.models.raw_email import RawEmail
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.parsers.budget import cost_cents_for_usage, record_usage
from trip_tracker.parsers.cluster import cluster_for_user, derive_destination
from trip_tracker.parsers.dispatch import dispatch_parse
from trip_tracker.parsers.llm import LLMClient
from trip_tracker.search.client import MeiliClientProtocol, build_client
from trip_tracker.search.sync import segment_to_doc, trip_to_doc

logger = logging.getLogger(__name__)


async def parse_raw_email(ctx: dict[str, Any], *, raw_email_id: str) -> None:
    """Parse one RawEmail and persist the result.

    Idempotent: re-running on an already-parsed RawEmail is a no-op.
    saq passes kwargs through `ctx` for the function's keyword args (note
    the kw-only signature). Engine and settings live in the worker context.

    TODO (Phase 3.5): the Inbox `reask` route stores a hint in
    raw.headers['X-Tt-Hint']. Pass it through to dispatch_parse here so the
    LLM picks up the user's correction. v0.3.0 ships without this propagation.
    """
    settings: Settings = ctx["settings"]
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
            return

        for draft in outcome.result.segments:
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
            elif decision.kind == "attach":
                trip_id = decision.trip_id
            else:
                trip_id = None

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

        if outcome.result.confidence < settings.llm_confidence_floor:
            raw.parse_status = "review"
        else:
            raw.parse_status = "parsed"
        await db.commit()


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
        else:
            raise ValueError(f"unknown entity: {entity}")


async def startup(ctx: dict[str, Any]) -> None:
    """Build worker-process singletons. saq calls this once when the worker boots."""
    s = Settings()
    ctx["settings"] = s
    # Build one engine per worker process (not per task) so the connection
    # pool is reused across thousands of jobs. Disposed in shutdown().
    ctx["engine"] = create_async_engine(str(s.database_url))
    ctx["meili"] = build_client(s)


async def shutdown(ctx: dict[str, Any]) -> None:
    """Dispose the engine on graceful shutdown."""
    engine = ctx.get("engine")
    if engine is not None:
        await engine.dispose()


# Read settings once at module import. Worker fails fast if env is incomplete.
_SETTINGS = Settings()
queue = Queue.from_url(_SETTINGS.redis_url)

# saq picks up `settings` (a dict) when invoked via `saq trip_tracker.worker.settings`.
settings = {
    "queue": queue,
    "functions": [parse_raw_email, sync_meili],
    "startup": startup,
    "shutdown": shutdown,
    "concurrency": 1,  # one task at a time per worker; matches arq's effective default
    # Retries: per-task `retries=5` set at enqueue time — see webhook.py.
}
