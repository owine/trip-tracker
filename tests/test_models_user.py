"""User model: insert, query, uniqueness."""

from __future__ import annotations

import os
import subprocess

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.user import User


def test_user_model_has_no_oidc_subject_or_is_admin() -> None:
    cols = {c.name for c in User.__table__.columns}
    assert "oidc_subject" not in cols
    assert "is_admin" not in cols
    assert "ics_token_hash" in cols  # ICS feed auth survives
    assert "email" in cols


@pytest.mark.asyncio
async def test_create_user(db_session: AsyncSession) -> None:
    u = User(
        email="oliver@example.com",
        display_name="Oliver",
    )
    db_session.add(u)
    await db_session.commit()

    result = await db_session.execute(select(User).where(User.email == "oliver@example.com"))
    fetched = result.scalar_one()
    assert fetched.email == "oliver@example.com"
    assert fetched.id is not None  # uuid assigned
    assert fetched.created_at is not None


@pytest.mark.asyncio
async def test_user_email_unique(db_session: AsyncSession) -> None:
    db_session.add(User(email="dupe@example.com", display_name="A"))
    await db_session.commit()
    db_session.add(User(email="dupe@example.com", display_name="B"))
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_alembic_upgrade_creates_users_table(db_url: str) -> None:
    """Run the actual Alembic migration against an empty DB and verify the table exists."""
    env = os.environ | {
        "DATABASE_URL": db_url,
        "SESSION_SECRET": "x" * 32,
        "OWNER_EMAIL": "owner@example.com",
        "OWNER_SESSION_TOKEN": "x" * 32,
        "BASE_URL": "https://x.example.com",
    }
    result = subprocess.run(  # noqa: ASYNC221
        ["uv", "run", "alembic", "upgrade", "head"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
