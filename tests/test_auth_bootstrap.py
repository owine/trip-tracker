"""Tests for /auth/bootstrap single-owner cookie auth route."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

_VALID_TOKEN = "x" * 32


@pytest_asyncio.fixture
async def async_client(monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    """Async HTTP client wired to the FastAPI app with bootstrap env vars set."""
    monkeypatch.setenv("OWNER_EMAIL", "owner@example.com")
    monkeypatch.setenv("OWNER_SESSION_TOKEN", _VALID_TOKEN)

    # Import after env vars are in place so Settings() succeeds.
    from trip_tracker.app import create_app
    from trip_tracker.config import Settings

    app = create_app(Settings())
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_bootstrap_with_correct_token_sets_cookie_and_redirects(
    async_client: AsyncClient,
) -> None:
    response = await async_client.get(
        "/auth/bootstrap?token=" + _VALID_TOKEN,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert "tt_session" in response.cookies


@pytest.mark.asyncio
async def test_bootstrap_with_wrong_token_returns_401(
    async_client: AsyncClient,
) -> None:
    response = await async_client.get("/auth/bootstrap?token=wrong")
    assert response.status_code == 401
    assert "tt_session" not in response.cookies


@pytest.mark.asyncio
async def test_bootstrap_without_token_returns_400(
    async_client: AsyncClient,
) -> None:
    response = await async_client.get("/auth/bootstrap")
    assert response.status_code == 400
