"""DB engine + session sanity tests."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.db import build_engine, build_session_factory


@pytest.mark.asyncio
async def test_engine_executes_select_one(db_url: str) -> None:
    engine = build_engine(db_url)
    Session = build_session_factory(engine)
    async with Session() as session:
        result = await session.execute(text("SELECT 1"))
        assert result.scalar_one() == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_db_session_fixture_works(db_session: AsyncSession) -> None:
    result = await db_session.execute(text("SELECT 1"))
    assert result.scalar_one() == 1
