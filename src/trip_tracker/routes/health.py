"""Health check endpoint. Used by Traefik + Docker healthcheck.

Reports the same ``version`` / ``git_sha`` / ``git_sha_short`` triple that
the page footer renders (see ``trip_tracker.build_info``), so a deploy-side
probe can confirm the running image without scraping HTML.
"""

from __future__ import annotations

from fastapi import APIRouter

from trip_tracker.build_info import GIT_SHA, GIT_SHA_SHORT, VERSION

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {
        "status": "ok",
        "version": VERSION,
        "git_sha": GIT_SHA,
        "git_sha_short": GIT_SHA_SHORT,
    }
