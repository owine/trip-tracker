"""Per-user Settings page. Currently hosts the ICS feed generate/regenerate UI."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.deps import get_settings, require_user
from trip_tracker.config import Settings
from trip_tracker.db import get_session
from trip_tracker.ics.tokens import generate_token
from trip_tracker.models.user import User
from trip_tracker.templating import register_globals

router = APIRouter(tags=["settings"])

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
register_globals(templates)


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    user: User = Depends(require_user),  # noqa: B008
) -> HTMLResponse:
    flash = None
    if "session" in request.scope:
        flash = request.session.pop("flash", None)
    # Hash suffix as presence indicator (last 5 chars). Never reveal full hash.
    hash_suffix = (user.ics_token_hash or "")[-5:] if user.ics_token_hash else None
    return templates.TemplateResponse(
        request,
        "settings/page.html",
        {
            "user": user,
            "flash": flash,
            "ics_present": user.ics_token_hash is not None,
            "ics_hash_suffix": hash_suffix,
        },
    )


@router.post("/settings/ics/regenerate")
async def regenerate_ics_token(
    request: Request,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> Response:
    plaintext, h = generate_token()
    user.ics_token_hash = h
    db.add(user)
    await db.commit()

    # Pydantic AnyHttpUrl may or may not have a trailing slash; rstrip handles
    # both shapes without regex/replace gymnastics.
    base = str(settings.base_url).rstrip("/")
    feed_url = f"{base}/ics/{plaintext}.ics"
    if "session" in request.scope:
        request.session["flash"] = {
            "kind": "ics_generated",
            "url": feed_url,
        }
    return RedirectResponse("/settings", status_code=303)
