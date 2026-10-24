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
    # git_sha / build_date are "unknown" outside a CI-built image, but the keys
    # must be present so deploy-side health probes can rely on them.
    assert "git_sha" in body
    assert "git_sha_short" in body
    assert body["build_date"]  # never empty
    # No release version: a release retags an existing digest, so the image
    # can't know which release name(s) it is being pulled under.
    assert "version" not in body
