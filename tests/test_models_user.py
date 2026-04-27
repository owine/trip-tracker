"""User model: insert, query, uniqueness."""

from __future__ import annotations

import os
import subprocess

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.user import User


@pytest.mark.asyncio
async def test_create_user(db_session: AsyncSession) -> None:
    u = User(
        oidc_subject="abc-123",
        email="oliver@example.com",
        display_name="Oliver",
        is_admin=True,
    )
    db_session.add(u)
    await db_session.commit()

    result = await db_session.execute(select(User).where(User.email == "oliver@example.com"))
    fetched = result.scalar_one()
    assert fetched.oidc_subject == "abc-123"
    assert fetched.is_admin is True
    assert fetched.id is not None  # uuid assigned
    assert fetched.created_at is not None


@pytest.mark.asyncio
async def test_user_oidc_subject_unique(db_session: AsyncSession) -> None:
    db_session.add(User(oidc_subject="dup", email="a@example.com", display_name="A"))
    await db_session.commit()
    db_session.add(User(oidc_subject="dup", email="b@example.com", display_name="B"))
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_user_email_unique(db_session: AsyncSession) -> None:
    db_session.add(User(oidc_subject="s1", email="dupe@example.com", display_name="A"))
    await db_session.commit()
    db_session.add(User(oidc_subject="s2", email="dupe@example.com", display_name="B"))
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_alembic_upgrade_creates_users_table(db_url: str) -> None:
    """Run the actual Alembic migration against an empty DB and verify the table exists."""
    env = os.environ | {
        "DATABASE_URL": db_url,
        "SESSION_SECRET": "x" * 32,
        "OIDC_ISSUER": "https://x.example.com",
        "OIDC_CLIENT_ID": "x",
        "OIDC_CLIENT_SECRET": "x",
        "OIDC_REDIRECT_URI": "https://x.example.com/cb",
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
