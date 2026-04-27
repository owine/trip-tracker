"""GET /healthz returns 200 with a small JSON body."""

from __future__ import annotations

import httpx
import pytest

from trip_tracker.app import create_app


@pytest.mark.asyncio
async def test_healthz_ok() -> None:
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body
