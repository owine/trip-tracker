"""Inbox routes: 3 buckets + 5 actions.

Auth scoping: same case-insensitive lowering pattern as admin raw-emails
(extract local-part of to_address, lower, join to forwarding_aliases).
Admins (is_admin=True) see all RawEmails.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from redis.asyncio import Redis as AsyncRedis
from sqlalchemy import delete, select
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
from trip_tracker.models.user import User
from trip_tracker.search.sync import enqueue_meili_sync
from trip_tracker.templating import register_globals

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
    return templates.TemplateResponse(
        request,
        "inbox/list.html",
        {
            "user": user,
            "review_rows": review_rows,
            "no_seg_rows": no_seg_rows,
            "duplicate_rows": [],  # Phase 3.5: duplicate detection deferred
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
        incurred_on=seg.start_at.date(),
        status="paid",
    )
    db.add(exp)


@router.post("/{raw_id}/confirm", response_model=None)
async def confirm(
    raw_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    raw = await _load_owned(db, user, raw_id)
    raw.parse_status = "parsed"

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
