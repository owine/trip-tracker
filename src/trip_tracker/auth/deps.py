"""FastAPI dependencies that resolve the current user from the session cookie."""

from __future__ import annotations

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.session import SessionExpired, SessionTampered, decode_session
from trip_tracker.config import Settings
from trip_tracker.db import get_session
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
