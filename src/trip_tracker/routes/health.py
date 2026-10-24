"""Health check endpoint. Used by Traefik + Docker healthcheck.

Reports the same ``git_sha`` / ``git_sha_short`` / ``build_date`` the page
footer renders (see ``trip_tracker.build_info``), so a deploy-side probe can
confirm the running image without scraping HTML. To map a SHA to a release,
compare it against the digest the ``X.Y.Z`` image tag resolves to.
"""

from __future__ import annotations

from fastapi import APIRouter

from trip_tracker.build_info import BUILD_DATE, GIT_SHA, GIT_SHA_SHORT

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {
        "status": "ok",
        "git_sha": GIT_SHA,
        "git_sha_short": GIT_SHA_SHORT,
        "build_date": BUILD_DATE,
    }
