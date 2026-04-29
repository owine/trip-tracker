"""Admin routes: aliases + raw-emails."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.models.forwarding_alias import ForwardingAlias
from trip_tracker.models.user import User


def _cookie(user: User, settings: Settings) -> dict[str, str]:
    return {
        "tt_session": encode_session(
            SessionPayload(user_id=user.id, oidc_subject=user.oidc_subject),
            secret=settings.session_secret.get_secret_value(),
            max_age=3600,
        )
    }


@pytest.mark.asyncio
async def test_non_admin_blocked(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = User(oidc_subject="x", email="x@x.com", display_name="X", is_admin=False)
    db_session.add(user)
    await db_session.commit()
    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies=_cookie(user, settings),
        ) as c,
    ):
        r = await c.get("/admin/aliases")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_alias_crud(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    admin = User(oidc_subject="a", email="a@x.com", display_name="A", is_admin=True)
    db_session.add(admin)
    await db_session.commit()
    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies=_cookie(admin, settings),
        ) as c,
    ):
        r = await c.post(
            "/admin/aliases",
            data={"local_part": "oliver", "user_id": str(admin.id)},
            follow_redirects=False,
        )
        assert r.status_code == 303

        r = await c.get("/admin/aliases")
        assert "oliver" in r.text

        alias = (
            await db_session.execute(
                select(ForwardingAlias).where(ForwardingAlias.local_part == "oliver")
            )
        ).scalar_one()

        r = await c.post(f"/admin/aliases/{alias.id}/delete", follow_redirects=False)
        assert r.status_code == 303
        rows = (await db_session.execute(select(ForwardingAlias))).scalars().all()
        assert rows == []
