"""Admin routes: forwarding-alias CRUD + raw-email viewer."""

from __future__ import annotations

import re
import uuid
from email.parser import BytesParser
from email.policy import default as email_policy_default
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.deps import require_user  # TODO(phase11-T5): admin.py deleted in T5
from trip_tracker.db import get_session
from trip_tracker.models.forwarding_alias import ForwardingAlias
from trip_tracker.models.raw_email import RawEmail
from trip_tracker.models.user import User
from trip_tracker.templating import register_globals

router = APIRouter(prefix="/admin", tags=["admin"])
_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
register_globals(templates)


@router.get("/aliases", response_class=HTMLResponse)
async def alias_list(
    request: Request,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    rows = (
        await db.execute(
            select(ForwardingAlias, User)
            .join(User, User.id == ForwardingAlias.user_id)
            .order_by(ForwardingAlias.local_part)
        )
    ).all()
    return templates.TemplateResponse(
        request,
        "admin/alias_list.html",
        {"user": user, "rows": rows},
    )


@router.get("/aliases/new", response_class=HTMLResponse)
async def alias_new_form(
    request: Request,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    users = (await db.execute(select(User).order_by(User.email))).scalars().all()
    return templates.TemplateResponse(
        request,
        "admin/alias_form.html",
        {"user": user, "users": users, "alias": None, "errors": {}},
    )


# RFC-5321 valid local-part chars (lowercase only — we normalize on input).
# Spec §4: forwarding_aliases.local_part is "lowercase, RFC-5321 valid local-part chars only".
_LOCAL_PART_RE = re.compile(r"^[a-z0-9._%+\-]+$")


@router.post("/aliases", response_model=None)
async def alias_create(
    request: Request,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
    local_part: str = Form(...),
    user_id: uuid.UUID = Form(...),  # noqa: B008
) -> Response:
    normalized = local_part.lower().strip()
    if not _LOCAL_PART_RE.match(normalized) or len(normalized) > 64:
        users = (await db.execute(select(User).order_by(User.email))).scalars().all()
        return templates.TemplateResponse(
            request,
            "admin/alias_form.html",
            {
                "user": user,
                "users": users,
                "alias": None,
                "errors": {"_form": "invalid local part"},
            },
            status_code=200,
        )
    try:
        db.add(ForwardingAlias(local_part=normalized, user_id=user_id))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        users = (await db.execute(select(User).order_by(User.email))).scalars().all()
        return templates.TemplateResponse(
            request,
            "admin/alias_form.html",
            {
                "user": user,
                "users": users,
                "alias": None,
                "errors": {"_form": f"alias {normalized!r} already exists"},
            },
            status_code=200,
        )
    return RedirectResponse("/admin/aliases", status_code=303)


@router.get("/aliases/{alias_id}/edit", response_class=HTMLResponse)
async def alias_edit_form(
    request: Request,
    alias_id: uuid.UUID,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    alias = await db.get(ForwardingAlias, alias_id)
    if alias is None:
        raise HTTPException(404)
    users = (await db.execute(select(User).order_by(User.email))).scalars().all()
    return templates.TemplateResponse(
        request,
        "admin/alias_form.html",
        {"user": user, "users": users, "alias": alias, "errors": {}},
    )


@router.post("/aliases/{alias_id}", response_model=None)
async def alias_update(
    request: Request,
    alias_id: uuid.UUID,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
    local_part: str = Form(...),
    user_id: uuid.UUID = Form(...),  # noqa: B008
) -> Response:
    alias = await db.get(ForwardingAlias, alias_id)
    if alias is None:
        raise HTTPException(404)
    normalized = local_part.lower().strip()
    if not _LOCAL_PART_RE.match(normalized) or len(normalized) > 64:
        users = (await db.execute(select(User).order_by(User.email))).scalars().all()
        return templates.TemplateResponse(
            request,
            "admin/alias_form.html",
            {
                "user": user,
                "users": users,
                "alias": alias,
                "errors": {"_form": "invalid local part"},
            },
            status_code=200,
        )
    alias.local_part = normalized
    alias.user_id = user_id
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        users = (await db.execute(select(User).order_by(User.email))).scalars().all()
        return templates.TemplateResponse(
            request,
            "admin/alias_form.html",
            {
                "user": user,
                "users": users,
                "alias": alias,
                "errors": {"_form": f"alias {normalized!r} already exists"},
            },
            status_code=200,
        )
    return RedirectResponse("/admin/aliases", status_code=303)


@router.post("/aliases/{alias_id}/delete", response_model=None)
async def alias_delete(
    alias_id: uuid.UUID,
    _user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    alias = await db.get(ForwardingAlias, alias_id)
    if alias is None:
        raise HTTPException(404)
    await db.delete(alias)
    await db.commit()
    return RedirectResponse("/admin/aliases", status_code=303)


@router.get("/raw-emails", response_class=HTMLResponse)
async def raw_email_list(
    request: Request,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
    page: int = 1,
) -> HTMLResponse:
    page_size = 50
    offset = max(0, (page - 1) * page_size)
    stmt = (
        select(RawEmail, User)
        .outerjoin(
            ForwardingAlias,
            ForwardingAlias.local_part
            == sa.func.lower(sa.func.split_part(RawEmail.to_address, "@", 1)),
        )
        .outerjoin(User, User.id == ForwardingAlias.user_id)
        .order_by(RawEmail.received_at.desc())
        .limit(page_size)
        .offset(offset)
    )
    rows = (await db.execute(stmt)).all()
    return templates.TemplateResponse(
        request,
        "admin/raw_email_list.html",
        {"user": user, "rows": rows, "page": page},
    )


@router.get("/raw-emails/{raw_email_id}", response_class=HTMLResponse)
async def raw_email_detail(
    request: Request,
    raw_email_id: uuid.UUID,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    email_row = await db.get(RawEmail, raw_email_id)
    if email_row is None:
        raise HTTPException(404)
    msg = BytesParser(policy=email_policy_default).parsebytes(email_row.mime_blob)
    text_body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                raw_payload = part.get_payload(decode=True)
                text_body = raw_payload.decode() if isinstance(raw_payload, bytes) else ""
                break
    else:
        if msg.get_content_type() == "text/plain":
            raw_payload = msg.get_payload(decode=True)
            text_body = raw_payload.decode() if isinstance(raw_payload, bytes) else ""
    return templates.TemplateResponse(
        request,
        "admin/raw_email_detail.html",
        {"user": user, "re": email_row, "text_body": text_body},
    )


@router.get("/raw-emails/{raw_email_id}/eml", response_model=None)
async def raw_email_download(
    raw_email_id: uuid.UUID,
    _user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    email_row = await db.get(RawEmail, raw_email_id)
    if email_row is None:
        raise HTTPException(404)
    return Response(
        content=email_row.mime_blob,
        media_type="message/rfc822",
        headers={"Content-Disposition": f'attachment; filename="{email_row.id}.eml"'},
    )
