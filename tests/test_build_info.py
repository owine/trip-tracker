"""build_info: git SHA + build date resolution from env with sensible fallbacks."""

from __future__ import annotations

import importlib

import pytest


def _reload() -> object:
    import trip_tracker.build_info as bi

    return importlib.reload(bi)


def test_git_sha_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIP_TRACKER_GIT_SHA", "deadbeefcafef00d")
    bi = _reload()
    assert bi.GIT_SHA == "deadbeefcafef00d"  # type: ignore[attr-defined]
    assert bi.GIT_SHA_SHORT == "deadbee"  # type: ignore[attr-defined]


def test_git_sha_fallback_when_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRIP_TRACKER_GIT_SHA", raising=False)
    bi = _reload()
    assert bi.GIT_SHA == "unknown"  # type: ignore[attr-defined]
    assert bi.GIT_SHA_SHORT == "dev"  # type: ignore[attr-defined]


def test_build_date_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIP_TRACKER_BUILD_DATE", "2026-10-23T14:05:09.123Z")
    bi = _reload()
    assert bi.BUILD_DATE == "2026-10-23T14:05:09.123Z"  # type: ignore[attr-defined]
    assert bi.BUILD_DAY == "2026-10-23"  # type: ignore[attr-defined]


def test_build_date_fallback_when_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRIP_TRACKER_BUILD_DATE", raising=False)
    bi = _reload()
    assert bi.BUILD_DATE == "unknown"  # type: ignore[attr-defined]
    assert bi.BUILD_DAY == "unknown"  # type: ignore[attr-defined]


def test_empty_env_treated_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIP_TRACKER_GIT_SHA", "   ")
    monkeypatch.setenv("TRIP_TRACKER_BUILD_DATE", "")
    bi = _reload()
    assert bi.GIT_SHA == "unknown"  # type: ignore[attr-defined]
    assert bi.BUILD_DATE == "unknown"  # type: ignore[attr-defined]


def test_no_release_version_exposed() -> None:
    # Releases retag an already-built digest, so nothing inside the image can
    # know its release number. Guard against a VERSION constant creeping back.
    bi = _reload()
    assert not hasattr(bi, "VERSION")
