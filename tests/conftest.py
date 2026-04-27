"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from pytest_postgresql import factories
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# pytest-postgresql provides a real ephemeral PG per test session.
# CI provides Postgres via service container; locally it falls back to
# whatever 'pg_ctl' / 'postgres' is on PATH.
_postgresql_proc = factories.postgresql_proc(port=None, unixsocketdir="/tmp")
postgresql = factories.postgresql("_postgresql_proc")


def _async_url_from_psycopg(conn) -> str:  # type: ignore[no-untyped-def]
    p = conn.info
    return f"postgresql+asyncpg://{p.user}:{p.password or ''}@{p.host}:{p.port}/{p.dbname}"


@pytest_asyncio.fixture
async def db_url(postgresql) -> str:  # type: ignore[no-untyped-def]
    return _async_url_from_psycopg(postgresql)


@pytest_asyncio.fixture
async def db_session(db_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(db_url, echo=False, future=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    # Create schema for every test session — Phase 1 only has `users`,
    # so we bring it up via metadata.create_all rather than running Alembic
    # in test (Alembic is exercised by its own test in Task 6).
    import trip_tracker.models.user  # noqa: F401  (register mapper)
    from trip_tracker.models.base import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        yield session

    await engine.dispose()


@pytest.fixture(autouse=True)
def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate required env vars so importing config.Settings doesn't fail."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("OIDC_ISSUER", "https://auth.example.com")
    monkeypatch.setenv("OIDC_CLIENT_ID", "trip-tracker")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "secret")
    monkeypatch.setenv("OIDC_REDIRECT_URI", "https://trips.example.com/auth/callback")
    monkeypatch.setenv("BASE_URL", "https://trips.example.com")
    monkeypatch.setenv("LOG_FORMAT", "console")
