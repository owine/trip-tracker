"""Inbox routes: 3 buckets + 5 actions.

Auth scoping: same case-insensitive lowering pattern as admin raw-emails
(extract local-part of to_address, lower, join to forwarding_aliases).
Admins (is_admin=True) see all RawEmails.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from redis.asyncio import Redis as AsyncRedis
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.deps import require_user
from trip_tracker.config import Settings
from trip_tracker.db import get_session
from trip_tracker.expenses.categories import Category
from trip_tracker.expenses.currencies import minor_digits
from trip_tracker.expenses.freeze import freeze_fx
from trip_tracker.expenses.fx import FxError
from trip_tracker.ingest.webhook import enqueue_parse
from trip_tracker.models.expense import Expense
from trip_tracker.models.forwarding_alias import ForwardingAlias
from trip_tracker.models.raw_email import RawEmail
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User
from trip_tracker.search.sync import enqueue_meili_sync
from trip_tracker.templating import register_globals
from trip_tracker.trips.consolidation import (
    ConsolidationCandidate,
    ConsolidationTarget,
    consolidation_candidates,
)

logger = logging.getLogger(__name__)

# Map Segment.type → Expense Category
_SEGMENT_TYPE_TO_CATEGORY: dict[str, Category] = {
    "flight": Category.TRANSIT,
    "train": Category.TRANSIT,
    "transfer": Category.TRANSIT,
    "car": Category.TRANSIT,
    "lodging": Category.LODGING,
    "activity": Category.ACTIVITIES,
}

router = APIRouter(prefix="/inbox", tags=["inbox"])
_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
register_globals(templates)


def _user_owned_filter(user: User) -> sa.ColumnElement[bool]:
    """Return a SQLA expression for 'this user owns this RawEmail'."""
    if user.is_admin:
        return sa.true()
    local = sa.func.lower(sa.func.split_part(RawEmail.to_address, "@", 1))
    return RawEmail.id.in_(
        select(RawEmail.id)
        .join(ForwardingAlias, ForwardingAlias.local_part == local)
        .where(ForwardingAlias.user_id == user.id)
    )


async def _load_owned(db: AsyncSession, user: User, raw_id: uuid.UUID) -> RawEmail:
    raw = (
        await db.execute(select(RawEmail).where(RawEmail.id == raw_id, _user_owned_filter(user)))
    ).scalar_one_or_none()
    if raw is None:
        raise HTTPException(404)
    return raw


@router.get("", response_class=HTMLResponse)
async def inbox_list(
    request: Request,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    own = _user_owned_filter(user)
    review_rows = (
        (
            await db.execute(
                select(RawEmail)
                .where(RawEmail.parse_status == "review", own)
                .order_by(RawEmail.received_at.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    no_seg_rows = (
        (
            await db.execute(
                select(RawEmail)
                .where(RawEmail.parse_status == "no_segments", own)
                .order_by(RawEmail.received_at.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    duplicate_rows = (
        (
            await db.execute(
                select(RawEmail)
                .where(RawEmail.parse_status == "duplicate", own)
                .order_by(RawEmail.received_at.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    # Build per-raw-email consolidation candidates for the review bucket.
    # TODO(v0.9.1-perf): N+1 across review_rows (≤ 50/page, ~5 selects each → ~250
    # queries on a busy /inbox). Replace with a single JOIN/CTE keyed by
    # raw_email_id. Bounded for v0.9.0 — runs per-render, not in a hot loop.
    consolidation_by_raw: dict[uuid.UUID, list[ConsolidationCandidate]] = {}
    for raw in review_rows:
        raw_segments = (
            (await db.execute(select(Segment).where(Segment.raw_email_id == raw.id)))
            .scalars()
            .all()
        )
        if not raw_segments:
            continue
        # Segment.trip_id is non-optional (Mapped[UUID]); load the Trip row.
        auto_trip = (
            await db.execute(select(Trip).where(Trip.id == raw_segments[0].trip_id))
        ).scalar_one_or_none()
        if auto_trip is None:
            continue
        target = ConsolidationTarget.from_trip(auto_trip, list(raw_segments))
        consolidation_by_raw[raw.id] = await consolidation_candidates(db, user, target)

    return templates.TemplateResponse(
        request,
        "inbox/list.html",
        {
            "user": user,
            "review_rows": review_rows,
            "no_seg_rows": no_seg_rows,
            "duplicate_rows": duplicate_rows,
            "consolidation_by_raw": consolidation_by_raw,
        },
    )


async def _maybe_create_expense(
    db: AsyncSession,
    seg: Segment,
    user: User,
    redis: AsyncRedis,
) -> None:
    """Create an Expense from a Segment's JSON-LD pricing data if present.

    Guards:
    - Segment must be confirmed (not cancelled).
    - Segment must have both total_price and price_currency in details.
    - No existing Expense already linked to this segment (idempotency).
    """
    if seg.status == "cancelled":
        return

    details = seg.details or {}
    total_price = details.get("total_price")
    price_currency = details.get("price_currency")
    if total_price is None or not price_currency:
        return

    # Idempotency: skip if an expense is already linked to this segment
    existing = (
        await db.execute(select(Expense).where(Expense.segment_id == seg.id))
    ).scalar_one_or_none()
    if existing is not None:
        return

    # Convert float total_price to minor units (e.g. 38.50 USD → 3850 cents)
    scale = 10 ** minor_digits(price_currency)
    amount_minor = round(total_price * scale)

    # Freeze FX rate at approval time; on failure, log and skip — don't block confirm.
    try:
        fx_rate, amount_home_minor = await freeze_fx(
            amount_minor,
            price_currency,
            user.home_currency,
            redis,  # type: ignore[arg-type]
        )
    except FxError as exc:
        logger.warning(
            "FX unavailable for segment %s (%s %s); skipping auto-expense: %s",
            seg.id,
            total_price,
            price_currency,
            exc,
        )
        return

    category = _SEGMENT_TYPE_TO_CATEGORY.get(seg.type, Category.OTHER)
    incurred_on = _incurred_on(details, seg.start_at)

    exp = Expense(
        trip_id=seg.trip_id,
        segment_id=seg.id,
        owner_user_id=user.id,
        amount_minor=amount_minor,
        currency=price_currency,
        fx_rate=fx_rate,
        amount_home_minor=amount_home_minor,
        home_currency=user.home_currency,
        category=category.value,
        incurred_on=incurred_on,
        status="paid",
    )
    db.add(exp)


def _incurred_on(details: dict[str, object], start_at: datetime) -> date:
    """Date the expense was incurred — booking_time if known, else travel date.

    `details.booking_time` (set by the JSON-LD parser from schema.org
    `bookingTime`) is when the user actually paid for the reservation.
    `start_at` is when they travel — useful as a fallback because emails
    often omit bookingTime, but semantically the booking date is what
    `incurred_on` is supposed to mean.

    Malformed booking_time strings fall back to start_at silently rather
    than blocking expense creation; the worst case is "wrong by a few weeks"
    instead of "no expense at all", which the user can fix by editing.
    """
    booking_iso = details.get("booking_time")
    if isinstance(booking_iso, str):
        try:
            return datetime.fromisoformat(booking_iso).date()
        except ValueError:
            pass
    return start_at.date()


@router.post("/{raw_id}/confirm", response_model=None)
async def confirm(
    raw_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
    target_trip: uuid.UUID | None = Query(default=None),  # noqa: B008
) -> Response:
    raw = await _load_owned(db, user, raw_id)
    raw.parse_status = "parsed"

    if target_trip is not None:
        # Validate the target trip.
        target = (await db.execute(select(Trip).where(Trip.id == target_trip))).scalar_one_or_none()
        if target is None:
            raise HTTPException(status_code=404)
        if target.created_by != user.id:
            raise HTTPException(status_code=403)
        if target.merged_into_id is not None:
            raise HTTPException(
                status_code=400,
                detail="Target trip has been merged; choose an active trip",
            )

        # Snapshot the segment trip_ids BEFORE reassignment so we can clean
        # up newly-empty trips after.
        old_trip_ids = list(
            (
                await db.execute(
                    select(Segment.trip_id).where(Segment.raw_email_id == raw_id).distinct()
                )
            )
            .scalars()
            .all()
        )

        # Reassign segments to target_trip.
        await db.execute(
            update(Segment).where(Segment.raw_email_id == raw_id).values(trip_id=target_trip)
        )

        # Widen target's date range to span the moved segments.
        seg_date_extents = (
            await db.execute(
                select(
                    func.min(Segment.start_at),
                    func.max(Segment.end_at),
                    func.max(Segment.start_at),
                ).where(Segment.trip_id == target_trip)
            )
        ).one()
        seg_min_start, seg_max_end, seg_max_start = seg_date_extents
        if seg_min_start is not None:
            target.start_date = min(target.start_date, seg_min_start.date())
            # Use max() across both end_at and start_at maxima — `or` would
            # short-circuit on the truthy max(end_at) and miss a later
            # start_at from a no-end_at segment (e.g. one-way flight after
            # a hotel that already ended).
            latest_candidates = [d for d in (seg_max_end, seg_max_start) if d is not None]
            if latest_candidates:
                target.end_date = max(target.end_date, max(latest_candidates).date())
            target.updated_at = datetime.now(UTC)

        # Clean up trips that are now empty (had only segments from this raw_email).
        # Only delete if the trip is owned by this user (defensive). Capture
        # deleted IDs so we can enqueue meili removals after commit.
        deleted_trip_ids: list[uuid.UUID] = []
        for old_trip_id in old_trip_ids:
            if old_trip_id is None or old_trip_id == target_trip:
                continue
            remaining = (
                await db.execute(
                    select(func.count(Segment.id)).where(Segment.trip_id == old_trip_id)
                )
            ).scalar_one()
            if remaining == 0:
                old_trip = (
                    await db.execute(
                        select(Trip).where(
                            Trip.id == old_trip_id,
                            Trip.created_by == user.id,
                        )
                    )
                ).scalar_one_or_none()
                if old_trip is not None:
                    await db.delete(old_trip)
                    deleted_trip_ids.append(old_trip_id)

    # Auto-create Expense rows from JSON-LD pricing data on each approved segment.
    segments = (
        (await db.execute(select(Segment).where(Segment.raw_email_id == raw_id))).scalars().all()
    )
    if segments:
        settings: Settings = request.app.state.settings
        redis: AsyncRedis = AsyncRedis.from_url(str(settings.redis_url))
        try:
            for seg in segments:
                await _maybe_create_expense(db, seg, user, redis)
        finally:
            await redis.aclose()

    await db.commit()

    # Enqueue meili sync for the target + each deleted old trip (worker
    # removes from index when the row is gone post-commit).
    if target_trip is not None:
        settings_for_sync: Settings = request.app.state.settings
        await enqueue_meili_sync(settings_for_sync, entity="trip", entity_id=target_trip)
        for deleted_id in deleted_trip_ids:
            await enqueue_meili_sync(settings_for_sync, entity="trip", entity_id=deleted_id)

    return RedirectResponse("/inbox", status_code=303)


@router.post("/{raw_id}/discard", response_model=None)
async def discard(
    request: Request,
    raw_id: uuid.UUID,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Spec §6.1: discard sets parse_status='no_segments' AND deletes any
    segment(s) the parser auto-created from this email.
    """
    raw = await _load_owned(db, user, raw_id)
    raw.parse_status = "no_segments"
    deleted_segment_ids = (
        (await db.execute(select(Segment.id).where(Segment.raw_email_id == raw_id))).scalars().all()
    )
    await db.execute(delete(Segment).where(Segment.raw_email_id == raw_id))
    await db.commit()
    settings: Settings = request.app.state.settings
    for sid in deleted_segment_ids:
        await enqueue_meili_sync(settings, entity="segment", entity_id=sid)
    return RedirectResponse("/inbox", status_code=303)


@router.post("/{raw_id}/reparse", response_model=None)
async def reparse(
    raw_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    raw = await _load_owned(db, user, raw_id)
    raw.parse_status = "pending"
    await db.commit()
    settings: Settings = request.app.state.settings
    await enqueue_parse(settings, raw.id)
    return RedirectResponse("/inbox", status_code=303)


@router.post("/{raw_id}/not-a-duplicate", response_model=None)
async def not_a_duplicate(
    raw_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """User overrides the dedup verdict. Clear the header, requeue parse."""
    raw = await _load_owned(db, user, raw_id)
    raw.parse_status = "pending"
    new_headers = dict(raw.headers or {})
    new_headers.pop("X-Tt-Dedup-Against", None)
    raw.headers = new_headers
    await db.commit()
    settings: Settings = request.app.state.settings
    await enqueue_parse(settings, raw.id)
    return RedirectResponse("/inbox", status_code=303)


@router.post("/{raw_id}/reask", response_model=None)
async def reask(
    raw_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
    hint: str = Form(...),
) -> Response:
    """Re-runs the parse with a user-supplied hint stored in raw.headers.

    Worker doesn't consume X-Tt-Hint yet — Phase 3.5 enhancement.
    """
    raw = await _load_owned(db, user, raw_id)
    new_headers = dict(raw.headers or {})
    new_headers["X-Tt-Hint"] = hint
    raw.headers = new_headers
    raw.parse_status = "pending"
    await db.commit()
    settings: Settings = request.app.state.settings
    await enqueue_parse(settings, raw.id)
    return RedirectResponse("/inbox", status_code=303)
