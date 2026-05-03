"""build_info: version + git SHA resolution from env with sensible fallbacks."""

from __future__ import annotations

import importlib

import pytest


def _reload() -> object:
    import trip_tracker.build_info as bi

    return importlib.reload(bi)


def test_version_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIP_TRACKER_VERSION", "v9.9.9")
    bi = _reload()
    assert bi.VERSION == "v9.9.9"  # type: ignore[attr-defined]


def test_version_falls_back_to_package_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRIP_TRACKER_VERSION", raising=False)
    bi = _reload()
    from trip_tracker import __version__

    assert __version__ == bi.VERSION  # type: ignore[attr-defined]


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


def test_empty_env_treated_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIP_TRACKER_GIT_SHA", "   ")
    monkeypatch.setenv("TRIP_TRACKER_VERSION", "")
    bi = _reload()
    assert bi.GIT_SHA == "unknown"  # type: ignore[attr-defined]
    from trip_tracker import __version__

    assert __version__ == bi.VERSION  # type: ignore[attr-defined]
