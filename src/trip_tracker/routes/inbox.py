"""Inbox routes: 3 buckets + 5 actions.

Auth scoping: same case-insensitive lowering pattern as admin raw-emails
(extract local-part of to_address, lower, join to forwarding_aliases).
Admins (is_admin=True) see all RawEmails.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.deps import require_user
from trip_tracker.config import Settings
from trip_tracker.db import get_session
from trip_tracker.ingest.webhook import enqueue_parse
from trip_tracker.models.forwarding_alias import ForwardingAlias
from trip_tracker.models.raw_email import RawEmail
from trip_tracker.models.segment import Segment
from trip_tracker.models.user import User

router = APIRouter(prefix="/inbox", tags=["inbox"])
_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


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


@router.post("/{raw_id}/confirm", response_model=None)
async def confirm(
    raw_id: uuid.UUID,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    raw = await _load_owned(db, user, raw_id)
    raw.parse_status = "parsed"
    await db.commit()
    return RedirectResponse("/inbox", status_code=303)


@router.post("/{raw_id}/discard", response_model=None)
async def discard(
    raw_id: uuid.UUID,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Spec §6.1: discard sets parse_status='no_segments' AND deletes any
    segment(s) the parser auto-created from this email.
    """
    raw = await _load_owned(db, user, raw_id)
    raw.parse_status = "no_segments"
    await db.execute(delete(Segment).where(Segment.raw_email_id == raw_id))
    await db.commit()
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
