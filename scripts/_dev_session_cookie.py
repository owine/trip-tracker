"""Mint a dev session cookie for smoke-testing.

Reads Settings from env (.env in repo root), finds-or-creates a dev user, mints
a signed session cookie via the same `encode_session()` helper the OIDC callback
uses, and prints the cookie name + value.

Usage:
    uv run python scripts/_dev_session_cookie.py                  # default user
    uv run python scripts/_dev_session_cookie.py --email me@dev   # named user

The printed value pairs with the existing app's `Settings.session_cookie_name`
and `Settings.session_secret`, so the same `.env` must be in effect for both
this script and whatever serves the app (`uv run uvicorn ...`).

Not for production. Auth bypass that ships outside dev would be a security hole.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.models.user import User


async def _mint(email: str, display_name: str) -> tuple[str, str]:
    settings = Settings()
    engine = create_async_engine(settings.database_url, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        oidc_subject = f"dev-{email}"
        existing = (
            await db.execute(select(User).where(User.oidc_subject == oidc_subject))
        ).scalar_one_or_none()
        if existing is None:
            user = User(
                oidc_subject=oidc_subject,
                email=email,
                display_name=display_name,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
        else:
            user = existing
        cookie_value = encode_session(
            SessionPayload(user_id=user.id, oidc_subject=user.oidc_subject),
            secret=settings.session_secret.get_secret_value(),
            max_age=settings.session_max_age_seconds,
        )
    await engine.dispose()
    return settings.session_cookie_name, cookie_value


def main() -> int:
    parser = argparse.ArgumentParser(description="Mint a dev session cookie.")
    parser.add_argument("--email", default="dev@local", help="user email (default: dev@local)")
    parser.add_argument("--name", default="Dev User", help="display name (default: 'Dev User')")
    args = parser.parse_args()
    name, value = asyncio.run(_mint(args.email, args.name))
    print(f"{name}={value}")
    print(f"# user: {args.email}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
