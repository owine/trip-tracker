"""POST /api/ingest/email — verify HMAC, dedupe, persist raw_email. Spec §5."""

from __future__ import annotations

import logging
import time
import uuid

import structlog
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse, Response
from saq import Queue
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.config import Settings
from trip_tracker.db import get_session
from trip_tracker.ingest.hmac_verify import (
    PruneGate,
    prune_replay_cache,
    record_nonce,
    verify_signature,
)
from trip_tracker.ingest.mime import ParsedEmail, parse_mime
from trip_tracker.models.raw_email import RawEmail

router = APIRouter(prefix="/api/ingest", tags=["ingest"])
log = structlog.get_logger(__name__)
_logger = logging.getLogger(__name__)
_PRUNE_GATE = PruneGate(interval_seconds=60.0)


async def enqueue_parse(settings: Settings, raw_email_id: uuid.UUID) -> None:
    """Enqueue parse_raw_email task. Failure is logged but not propagated —
    the parse_pending admin command is the recovery path."""
    try:
        q = Queue.from_url(settings.redis_url)
        await q.enqueue(
            "parse_raw_email",
            raw_email_id=str(raw_email_id),
            retries=5,  # max attempts including initial — saq does exponential backoff
        )
        await q.disconnect()
    except Exception as exc:  # Redis blip shouldn't fail the webhook
        _logger.warning("enqueue_parse failed for %s: %s", raw_email_id, exc)


async def _persist_raw_email(
    db: AsyncSession,
    body: bytes,
    parsed: ParsedEmail,
) -> uuid.UUID | None:
    """Insert a raw_emails row if Message-ID is new. Returns inserted ID, or None on duplicate.

    Caller is responsible for the surrounding transaction. The single-statement
    INSERT ... ON CONFLICT DO NOTHING RETURNING id pattern is concurrency-safe
    under READ COMMITTED.
    """
    stmt = (
        pg_insert(RawEmail)
        .values(
            to_address=parsed.to_address,
            from_address=parsed.from_address,
            subject=parsed.subject,
            message_id=parsed.message_id,
            mime_blob=body,
            headers=parsed.headers,
            parse_status="pending",
        )
        .on_conflict_do_nothing(index_elements=["message_id"])
        .returning(RawEmail.id)
    )
    result: CursorResult[tuple[()]] = await db.execute(stmt)  # type: ignore[assignment]
    return result.scalar_one_or_none()


def _settings_dep() -> Settings:
    return Settings()


@router.post("/email", status_code=status.HTTP_202_ACCEPTED)
async def ingest_email(
    request: Request,
    db: AsyncSession = Depends(get_session),  # noqa: B008
    settings: Settings = Depends(_settings_dep),  # noqa: B008
) -> Response:
    # Step 1: Streaming size cap.
    max_bytes = settings.webhook_max_body_bytes
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            return JSONResponse(
                {"error": "payload_too_large", "max_bytes": max_bytes},
                status_code=413,
            )
        chunks.append(chunk)
    body = b"".join(chunks)

    # Step 2: Verify HMAC.
    sig = request.headers.get(settings.webhook_signature_header) or ""
    secret_bytes = settings.webhook_secret.get_secret_value().encode()
    if not verify_signature(body, sig, secret_bytes):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # Step 3: Verify timestamp + nonce.
    ts_raw = request.headers.get("X-Webhook-Timestamp") or ""
    nonce = (request.headers.get("X-Webhook-Nonce") or "").strip()
    try:
        ts = int(ts_raw)
    except ValueError:
        return JSONResponse({"error": "bad_request", "detail": "bad timestamp"}, 400)
    if not (1 <= len(nonce) <= 64):
        return JSONResponse({"error": "bad_request", "detail": "bad nonce"}, 400)
    skew = abs(int(time.time()) - ts)
    if skew > settings.webhook_timestamp_tolerance_seconds:
        return JSONResponse({"error": "bad_request", "detail": "timestamp skew"}, 400)

    # Step 4: Periodic prune (best-effort). Run BEFORE opening the main txn so
    # this is a separate, committed unit of work — pruning is hygiene, not
    # correctness, and we don't want it tangled with the nonce-insert txn.
    # Note: bind the context manager to a name first because Python's `async
    # with X if cond else Y:` is a SyntaxError (the `with` statement does not
    # accept inline conditional expressions for the cm).
    if _PRUNE_GATE.should_prune():
        prune_cm = db.begin_nested() if db.in_transaction() else db.begin()
        async with prune_cm:
            await prune_replay_cache(db)

    # Parse MIME *before* opening the main txn so the structured logging line
    # at the bottom can reference `parsed` even if the txn body raises before
    # the INSERT — the parse itself is pure CPU and always safe.
    parsed = parse_mime(body)

    # Step 5-6: Single transaction for nonce-insert + raw_emails-insert.
    async with db.begin():
        recorded = await record_nonce(db, ts_seconds=ts, nonce=nonce)
        replay = not recorded
        new_raw_email_id = await _persist_raw_email(db, body, parsed)
        duplicate = new_raw_email_id is None and not replay

    log.info(
        "ingest_webhook",
        status=202,
        to_address=parsed.to_address,
        from_address=parsed.from_address,
        message_id=parsed.message_id[:64],
        body_bytes=len(body),
        replay=replay,
        duplicate_message_id=duplicate,
    )

    # Step 8: Enqueue background parse task (best-effort; Redis blip → logged only).
    if new_raw_email_id is not None:
        await enqueue_parse(settings, new_raw_email_id)

    # Step 7: Always 202 once HMAC + timestamp pass.
    return Response(status_code=202)
