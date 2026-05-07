"""Single-user auth dependencies.

`current_user` decodes the session cookie and loads the owner User row.
`require_user` raises 401 if no valid cookie. `require_admin` is removed
(single-user installs have no admin distinction). `require_traveler` and
`require_traveler_including_merged` collapse to "load trip by id" since
the owner can access any trip.
"""

from __future__ import annotations

import uuid

from fastapi import Cookie, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.session import decode_session_cookie
from trip_tracker.config import Settings, get_settings
from trip_tracker.db import get_session
from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User

# FastAPI binds cookie names to parameter names. We hardcode "tt_session" in
# current_user(); if Settings.session_cookie_name is ever changed from its
# default, the cookie won't match and auth will silently fail. Pin it.
assert Settings.model_fields["session_cookie_name"].default == "tt_session", (
    "auth/deps.py expects session_cookie_name='tt_session'; update both together"
)


async def current_user(
    tt_session: str | None = Cookie(default=None),
    db: AsyncSession = Depends(get_session),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> User | None:
    if tt_session is None:
        return None
    payload = decode_session_cookie(tt_session, settings)
    if payload is None or "user_id" not in payload:
        return None
    try:
        user_uuid = uuid.UUID(payload["user_id"])
    except (ValueError, TypeError):
        return None
    result = await db.execute(select(User).where(User.id == user_uuid))
    return result.scalar_one_or_none()


async def require_user(
    user: User | None = Depends(current_user),  # noqa: B008
) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


async def require_traveler(
    trip_id: uuid.UUID,
    user: User = Depends(require_user),  # noqa: B008, ARG001
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> Trip:
    """Single-user mode: owner can access any trip. Returns Trip or 404.

    Note: the function name is preserved (vs renaming to `require_trip` or similar)
    to minimize call-site churn across the ~58 routes that depend on it. After
    Phase 11 settles, a follow-up rename pass is reasonable.
    """
    result = await db.execute(select(Trip).where(Trip.id == trip_id))
    trip = result.scalar_one_or_none()
    if trip is None:
        raise HTTPException(status_code=404, detail="trip not found")
    return trip
