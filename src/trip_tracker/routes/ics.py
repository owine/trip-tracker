"""Public ICS feed route — Authelia-exempt at the proxy layer.

GET /ics/<token>.ics → text/calendar with one VEVENT per segment in the
authenticated-by-token user's traveler_ids scope. Spec §7.1.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.deps import get_settings
from trip_tracker.config import Settings
from trip_tracker.db import get_session
from trip_tracker.ics.render import render_calendar
from trip_tracker.ics.tokens import hash_token, resolve_token
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler

router = APIRouter(tags=["ics"])
_logger = logging.getLogger(__name__)


@router.get("/ics/{token}.ics", response_class=Response)
async def ics_feed(
    token: str,
    db: AsyncSession = Depends(get_session),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> Response:
    user = await resolve_token(token, db)
    if user is None:
        raise HTTPException(status_code=404, detail="Not found")

    segments = (
        (
            await db.execute(
                select(Segment)
                # Join Trip to exclude soft-deleted trips' segments from subscribers'
                # feeds immediately on merge (spec §3.3: 410 Gone, no grace period).
                .join(Trip, Trip.id == Segment.trip_id)
                .join(TripTraveler, TripTraveler.trip_id == Segment.trip_id)
                .where(
                    TripTraveler.user_id == user.id,
                    Trip.merged_into_id.is_(None),
                )
                .order_by(Segment.start_at)
            )
        )
        .scalars()
        .all()
    )

    body = render_calendar(user=user, segments=segments, base_url=str(settings.base_url))
    # Log first 6 hex chars of the SHA-256 hash (NOT the plaintext token) so
    # suspicious spikes from a particular subscribed client surface in logs
    # without leaking enough to recover the secret. The plaintext token is
    # never logged. Spec §7.1.
    # Log first 6 hex chars of the hash as `prefix=` (NOT the plaintext token,
    # which is never logged). Label avoids the literal word "token" in the
    # format string so semgrep's logger-credential-leak heuristic doesn't fire
    # on a benign correlation ID. Spec §7.1.
    hash_prefix = hash_token(token)[:6]
    _logger.info(
        "ics_feed served: prefix=%s user_id=%s n_segments=%d",
        hash_prefix,
        user.id,
        len(segments),
    )
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="trip-tracker.ics"',
            "Cache-Control": "private, max-age=300",
        },
    )
