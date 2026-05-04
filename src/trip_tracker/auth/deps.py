"""FastAPI dependencies that resolve the current user from the session cookie."""

from __future__ import annotations

import uuid as _uuid

from fastapi import Cookie, Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.session import SessionExpired, SessionTampered, decode_session
from trip_tracker.config import Settings
from trip_tracker.db import get_session
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User


def get_settings() -> Settings:
    return Settings()


async def current_user(
    db: AsyncSession = Depends(get_session),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
    tt_session: str | None = Cookie(default=None),
) -> User | None:
    if not tt_session:
        return None
    try:
        payload = decode_session(
            tt_session,
            secret=settings.session_secret.get_secret_value(),
            max_age=settings.session_max_age_seconds,
        )
    except (SessionTampered, SessionExpired):
        return None
    return (await db.execute(select(User).where(User.id == payload.user_id))).scalar_one_or_none()


async def require_user(user: User | None = Depends(current_user)) -> User:  # noqa: B008
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


async def require_admin(user: User = Depends(require_user)) -> User:  # noqa: B008
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return user


async def require_traveler(
    trip_id: _uuid.UUID = Path(...),  # noqa: B008
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> Trip:
    """Return the Trip if the current user is one of its travelers; else 404."""
    stmt = (
        select(Trip)
        .join(TripTraveler, TripTraveler.trip_id == Trip.id)
        .where(
            Trip.id == trip_id,
            TripTraveler.user_id == user.id,
            Trip.merged_into_id.is_(None),
        )
    )
    trip = (await db.execute(stmt)).scalar_one_or_none()
    if trip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return trip


async def require_traveler_including_merged(
    trip_id: _uuid.UUID = Path(...),  # noqa: B008
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> Trip:
    """Like require_traveler but does NOT filter soft-deleted trips.

    Used by handlers that must distinguish 'soft-deleted trip' from
    'never-existed trip' (e.g. /trips/{id} returns 410 for the former, 404
    for the latter).

    DO NOT use on mutation endpoints (edit, delete, merge-into, undo-merge,
    dismiss-merge). Mutations on soft-deleted trips must 404 — use the
    strict `require_traveler` instead.
    """
    stmt = (
        select(Trip)
        .join(TripTraveler, TripTraveler.trip_id == Trip.id)
        .where(
            Trip.id == trip_id,
            TripTraveler.user_id == user.id,
        )
    )
    trip = (await db.execute(stmt)).scalar_one_or_none()
    if trip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return trip
