"""Bootstrap route for single-user cookie auth.

GET /auth/bootstrap?token=<OWNER_SESSION_TOKEN> validates the env token,
sets a long-lived signed cookie identifying the seeded owner user, and
redirects to /.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import RedirectResponse

from trip_tracker.auth.session import OWNER_USER_ID, set_session_cookie
from trip_tracker.config import Settings, get_settings

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/bootstrap")
async def bootstrap(
    token: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> Response:
    if token is None:
        raise HTTPException(status_code=400, detail="token query param required")
    if not secrets.compare_digest(token, settings.owner_session_token.get_secret_value()):
        raise HTTPException(status_code=401, detail="invalid token")
    response = RedirectResponse(url="/", status_code=302)
    set_session_cookie(response, user_id=OWNER_USER_ID, settings=settings)
    return response
