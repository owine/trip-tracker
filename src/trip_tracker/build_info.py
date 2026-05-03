"""Build identifiers for the running image: version + git SHA.

Both values are resolved once at module import. In CI-built images they
come from environment variables baked in by the Dockerfile; in local dev
they fall back to the package version literal and ``"unknown"``/``"dev"``
respectively.

``TRIP_TRACKER_VERSION`` is reserved for a future Dockerfile/CI build arg
that injects the actual release tag (e.g. ``"v0.8.1"``). Until that
plumbing lands, the version field falls back to ``trip_tracker.__version__``
in ``pyproject.toml`` — bump that on each tag to keep the footer accurate.
"""

from __future__ import annotations

import os

from trip_tracker import __version__


def _resolve_version() -> str:
    return os.environ.get("TRIP_TRACKER_VERSION", "").strip() or __version__


def _resolve_git_sha() -> str:
    return os.environ.get("TRIP_TRACKER_GIT_SHA", "").strip() or "unknown"


VERSION: str = _resolve_version()
GIT_SHA: str = _resolve_git_sha()
GIT_SHA_SHORT: str = GIT_SHA[:7] if GIT_SHA != "unknown" else "dev"
