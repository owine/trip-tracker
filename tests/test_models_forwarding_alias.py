"""ForwardingAlias model: uniqueness, FK cascade."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.forwarding_alias import ForwardingAlias
from trip_tracker.models.user import User


async def _user(db: AsyncSession, *, email: str = "u@example.com") -> User:
    u = User(email=email, display_name="U")
    db.add(u)
    await db.commit()
    return u


@pytest.mark.asyncio
async def test_create_alias(db_session: AsyncSession) -> None:
    user = await _user(db_session)
    alias = ForwardingAlias(user_id=user.id, local_part="oliver")
    db_session.add(alias)
    await db_session.commit()

    fetched = (
        await db_session.execute(
            select(ForwardingAlias).where(ForwardingAlias.local_part == "oliver")
        )
    ).scalar_one()
    assert fetched.user_id == user.id


@pytest.mark.asyncio
async def test_local_part_unique(db_session: AsyncSession) -> None:
    u1 = await _user(db_session, email="a@example.com")
    u2 = await _user(db_session, email="b@example.com")
    db_session.add(ForwardingAlias(user_id=u1.id, local_part="dup"))
    await db_session.commit()
    db_session.add(ForwardingAlias(user_id=u2.id, local_part="dup"))
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_cascade_on_user_delete(db_session: AsyncSession) -> None:
    user = await _user(db_session)
    db_session.add(ForwardingAlias(user_id=user.id, local_part="ada"))
    await db_session.commit()

    await db_session.delete(user)
    await db_session.commit()

    rows = (await db_session.execute(select(ForwardingAlias))).scalars().all()
    assert rows == []
