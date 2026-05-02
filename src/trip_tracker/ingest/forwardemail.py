"""POST /api/ingest/forwardemail — ForwardEmail.net webhook adapter.

ForwardEmail posts JSON like { raw, headers, attachments[], session, ... }.
We extract `raw` (the original RFC-822 MIME) and feed it through the same
persistence path as /api/ingest/email. Auth is a shared ?token= query param;
no HMAC because FE only signs payloads on paid plans, and the inner MIME is
not signed regardless.
"""

from __future__ import annotations

import hmac
import json
import uuid

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from structlog import get_logger

from trip_tracker.config import Settings
from trip_tracker.db import get_session
from trip_tracker.ingest.mime import parse_mime
from trip_tracker.ingest.webhook import _persist_raw_email, _settings_dep, enqueue_parse

router = APIRouter(prefix="/api/ingest", tags=["ingest"])
log = get_logger()


@router.post("/forwardemail", status_code=status.HTTP_202_ACCEPTED)
async def ingest_forwardemail(
    request: Request,
    db: AsyncSession = Depends(get_session),  # noqa: B008
    settings: Settings = Depends(_settings_dep),  # noqa: B008
) -> Response:
    # Step 1: Token gate. Constant-time compare so timing leaks don't help an attacker.
    expected = settings.forwardemail_relay_token.get_secret_value()
    provided = request.query_params.get("token") or ""
    if not hmac.compare_digest(expected, provided):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # Step 2: Parse JSON. FE's max body is bounded by your reverse-proxy config;
    # we don't enforce a separate cap here.
    try:
        body_bytes = await request.body()
        payload = json.loads(body_bytes)
    except json.JSONDecodeError:
        return JSONResponse({"error": "bad_request", "detail": "invalid JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse(
            {"error": "bad_request", "detail": "expected JSON object"}, status_code=400
        )

    raw_str = payload.get("raw")
    if not isinstance(raw_str, str) or not raw_str:
        return JSONResponse(
            {"error": "bad_request", "detail": "missing or empty 'raw' field"},
            status_code=400,
        )
    mime_body = raw_str.encode()  # FE delivers raw as a string with \r\n line endings

    # Step 3: Parse MIME headers, persist row, enqueue worker.
    parsed = parse_mime(mime_body)

    new_id: uuid.UUID | None
    async with db.begin():
        new_id = await _persist_raw_email(db, mime_body, parsed)

    log.info(
        "ingest_forwardemail",
        status=202,
        to_address=parsed.to_address,
        from_address=parsed.from_address,
        message_id=parsed.message_id[:64],
        body_bytes=len(mime_body),
        duplicate_message_id=new_id is None,
        fe_recipient=(payload.get("session") or {}).get("recipient"),
    )

    if new_id is not None:
        await enqueue_parse(settings, new_id)

    return Response(status_code=202)
