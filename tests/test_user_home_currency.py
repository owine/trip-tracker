"""User.home_currency column — default USD."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.user import User


@pytest.mark.asyncio
async def test_user_default_home_currency(db_session: AsyncSession) -> None:
    u = User(oidc_subject="hc1", email="hc1@x.com", display_name="HC1")
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    assert u.home_currency == "USD"
