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
    # git_sha is "unknown" outside a CI-built image, but the key must be present
    # so deploy-side health probes can rely on it for verification.
    assert "git_sha" in body
