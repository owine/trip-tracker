"""Health check endpoint. Used by Traefik + Docker healthcheck.

Reports `git_sha` from the `TRIP_TRACKER_GIT_SHA` env var, baked into the image
at build time by CI (see Dockerfile + .github/workflows/image.yml). Defaults to
"unknown" when running outside a CI-built image (local dev, tests).
"""

from __future__ import annotations

import os

from fastapi import APIRouter

from trip_tracker import __version__

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {
        "status": "ok",
        "version": __version__,
        "git_sha": os.environ.get("TRIP_TRACKER_GIT_SHA", "unknown"),
    }
