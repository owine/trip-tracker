"""Home page — anonymous landing or signed-in greeting."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from trip_tracker.auth.deps import current_user
from trip_tracker.models.user import User
from trip_tracker.templating import register_globals

router = APIRouter()

# Templates path computed from __file__ for CWD-independence.
_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
register_globals(templates)


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, user: User | None = Depends(current_user)) -> HTMLResponse:  # noqa: B008
    return templates.TemplateResponse(request, "home.html", {"user": user})
