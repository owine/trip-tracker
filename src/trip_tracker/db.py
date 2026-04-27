"""Async SQLAlchemy engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from trip_tracker.config import Settings


def build_engine(database_url: str) -> AsyncEngine:
    """Create an async engine with sane production defaults."""
    return create_async_engine(
        database_url,
        echo=False,
        future=True,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


# Module-level singleton for the running app. Tests do not use these.
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_db(settings: Settings) -> None:
    """Initialize module-level engine + session factory. Called on app startup."""
    global _engine, _session_factory
    _engine = build_engine(settings.database_url)
    _session_factory = build_session_factory(_engine)


async def dispose_db() -> None:
    """Dispose the engine. Called on app shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an AsyncSession."""
    if _session_factory is None:
        raise RuntimeError("DB not initialized — call init_db() first")
    async with _session_factory() as session:
        yield session
