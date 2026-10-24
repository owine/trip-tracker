"""Build identifiers for the running image: git SHA + build date.

Both values are resolved once at module import. In CI-built images they
come from environment variables baked in by the Dockerfile; in local dev
they fall back to ``"unknown"`` (and ``"dev"`` for the short SHA).

There is deliberately no release version here. A release (``vX.Y.Z``) retags
an image digest that CI already built from ``main`` — it never rebuilds — so
the image cannot know which release name(s) it will be pulled under. The
commit SHA and build date are true for every tag that digest ever carries.
``pyproject.toml``'s ``version`` is a packaging placeholder, not a release.
"""

from __future__ import annotations

import os


def _resolve(name: str) -> str:
    return os.environ.get(name, "").strip() or "unknown"


GIT_SHA: str = _resolve("TRIP_TRACKER_GIT_SHA")
GIT_SHA_SHORT: str = GIT_SHA[:7] if GIT_SHA != "unknown" else "dev"
# ISO-8601 timestamp (metadata-action's `org.opencontainers.image.created`).
BUILD_DATE: str = _resolve("TRIP_TRACKER_BUILD_DATE")
BUILD_DAY: str = BUILD_DATE[:10] if BUILD_DATE != "unknown" else "unknown"
