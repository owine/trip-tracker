"""Admin routes: aliases + raw-emails."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.models.forwarding_alias import ForwardingAlias
from trip_tracker.models.raw_email import RawEmail
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


@pytest.mark.asyncio
async def test_alias_edit_and_update(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    admin = User(oidc_subject="a", email="a@x.com", display_name="A", is_admin=True)
    other = User(oidc_subject="b", email="b@x.com", display_name="B", is_admin=False)
    db_session.add_all([admin, other])
    await db_session.flush()
    alias = ForwardingAlias(local_part="oliver", user_id=admin.id)
    db_session.add(alias)
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
        # Edit form is prefilled with current values.
        r = await c.get(f"/admin/aliases/{alias.id}/edit")
        assert r.status_code == 200
        assert 'value="oliver"' in r.text

        # Update reassigns the alias to a different owner and renames it.
        r = await c.post(
            f"/admin/aliases/{alias.id}",
            data={"local_part": "ow", "user_id": str(other.id)},
            follow_redirects=False,
        )
        assert r.status_code == 303

    await db_session.refresh(alias)
    assert alias.local_part == "ow"
    assert alias.user_id == other.id


@pytest.mark.asyncio
async def test_alias_update_404_for_missing(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    import uuid

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
        bogus = uuid.uuid4()
        r = await c.get(f"/admin/aliases/{bogus}/edit")
        assert r.status_code == 404
        r = await c.post(
            f"/admin/aliases/{bogus}",
            data={"local_part": "x", "user_id": str(admin.id)},
            follow_redirects=False,
        )
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_alias_update_invalid_local_part_renders_form(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    admin = User(oidc_subject="a", email="a@x.com", display_name="A", is_admin=True)
    db_session.add(admin)
    await db_session.flush()
    alias = ForwardingAlias(local_part="oliver", user_id=admin.id)
    db_session.add(alias)
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
            f"/admin/aliases/{alias.id}",
            data={"local_part": "Has Spaces!", "user_id": str(admin.id)},
            follow_redirects=False,
        )
    assert r.status_code == 200
    assert "invalid local part" in r.text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SIMPLE_MIME = (
    b"Subject: hi\r\n"
    b"From: sender@example.com\r\n"
    b"To: alias@inbound.example.com\r\n"
    b"Content-Type: text/plain\r\n"
    b"\r\n"
    b"Hello world\r\n"
)

_MULTIPART_MIME = (
    b"Subject: mp\r\n"
    b"From: sender@example.com\r\n"
    b"To: alias@inbound.example.com\r\n"
    b'Content-Type: multipart/alternative; boundary="boundary42"\r\n'
    b"\r\n"
    b"--boundary42\r\n"
    b"Content-Type: text/plain\r\n"
    b"\r\n"
    b"Multipart text body\r\n"
    b"--boundary42--\r\n"
)


def _make_raw_email(
    to_address: str = "alias@inbound.example.com",
    mime_blob: bytes = _SIMPLE_MIME,
    subject: str | None = "hi",
) -> RawEmail:
    return RawEmail(
        id=uuid.uuid4(),
        received_at=datetime.now(tz=UTC),
        to_address=to_address,
        from_address="sender@example.com",
        subject=subject,
        message_id=f"<{uuid.uuid4()}@test>",
        mime_blob=mime_blob,
        headers={},
        parse_status="pending",
    )


async def _admin_client(
    app,
    admin: User,
    settings: Settings,
):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        cookies=_cookie(admin, settings),
    )


# ---------------------------------------------------------------------------
# Raw-email tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_admin_blocked_raw_emails(
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
        r = await c.get("/admin/raw-emails")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_raw_email_list_shows_orphan_with_dash(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Orphan email (no matching ForwardingAlias) renders owner column as '—'."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    admin = User(oidc_subject="a", email="a@x.com", display_name="A", is_admin=True)
    db_session.add(admin)
    await db_session.flush()

    raw = _make_raw_email(to_address="noalias@inbound.example.com")
    db_session.add(raw)
    await db_session.commit()

    app = create_app(settings=settings)
    async with (
        app.router.lifespan_context(app),
        await _admin_client(app, admin, settings) as c,
    ):
        r = await c.get("/admin/raw-emails")
    assert r.status_code == 200
    assert "—" in r.text


@pytest.mark.asyncio
async def test_raw_email_detail_renders_text_body(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Detail page extracts and renders text/plain body from mime_blob."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    admin = User(oidc_subject="a", email="a@x.com", display_name="A", is_admin=True)
    db_session.add(admin)
    await db_session.flush()

    raw = _make_raw_email()
    db_session.add(raw)
    await db_session.commit()

    app = create_app(settings=settings)
    async with (
        app.router.lifespan_context(app),
        await _admin_client(app, admin, settings) as c,
    ):
        r = await c.get(f"/admin/raw-emails/{raw.id}")
    assert r.status_code == 200
    assert "Hello world" in r.text


@pytest.mark.asyncio
async def test_raw_email_detail_renders_multipart_body(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Detail page extracts text/plain from a multipart message."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    admin = User(oidc_subject="a", email="a@x.com", display_name="A", is_admin=True)
    db_session.add(admin)
    await db_session.flush()

    raw = _make_raw_email(mime_blob=_MULTIPART_MIME, subject="mp")
    db_session.add(raw)
    await db_session.commit()

    app = create_app(settings=settings)
    async with (
        app.router.lifespan_context(app),
        await _admin_client(app, admin, settings) as c,
    ):
        r = await c.get(f"/admin/raw-emails/{raw.id}")
    assert r.status_code == 200
    assert "Multipart text body" in r.text


@pytest.mark.asyncio
async def test_raw_email_eml_download(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """EML download returns message/rfc822 with exact mime_blob bytes."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    admin = User(oidc_subject="a", email="a@x.com", display_name="A", is_admin=True)
    db_session.add(admin)
    await db_session.flush()

    raw = _make_raw_email()
    db_session.add(raw)
    await db_session.commit()

    app = create_app(settings=settings)
    async with (
        app.router.lifespan_context(app),
        await _admin_client(app, admin, settings) as c,
    ):
        r = await c.get(f"/admin/raw-emails/{raw.id}/eml")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("message/rfc822")
    assert r.content == _SIMPLE_MIME


@pytest.mark.asyncio
async def test_raw_email_list_owner_match_is_case_insensitive(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Email To-headers preserve mixed case; aliases are lowercase. The join
    must lowercase the to_address local-part, otherwise legitimate owned
    emails render as orphans."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    admin = User(oidc_subject="a", email="a@x.com", display_name="A", is_admin=True)
    db_session.add(admin)
    await db_session.flush()
    db_session.add(ForwardingAlias(local_part="oliver", user_id=admin.id))
    raw = _make_raw_email(to_address="Oliver@inbound.example.com")
    db_session.add(raw)
    await db_session.commit()

    app = create_app(settings=settings)
    async with (
        app.router.lifespan_context(app),
        await _admin_client(app, admin, settings) as c,
    ):
        r = await c.get("/admin/raw-emails")
    assert r.status_code == 200
    assert "a@x.com" in r.text  # owner email rendered, not "—"


@pytest.mark.asyncio
async def test_raw_email_404_for_missing(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Both detail and eml routes return 404 for an unknown id."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    admin = User(oidc_subject="a", email="a@x.com", display_name="A", is_admin=True)
    db_session.add(admin)
    await db_session.commit()

    bogus = uuid.uuid4()
    app = create_app(settings=settings)
    async with (
        app.router.lifespan_context(app),
        await _admin_client(app, admin, settings) as c,
    ):
        r = await c.get(f"/admin/raw-emails/{bogus}")
        assert r.status_code == 404
        r = await c.get(f"/admin/raw-emails/{bogus}/eml")
        assert r.status_code == 404
