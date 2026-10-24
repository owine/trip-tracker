"""Page footer reports commit SHA + build date, never a release version."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.templating import Jinja2Templates

import trip_tracker
from trip_tracker import templating
from trip_tracker.templating import register_globals

TEMPLATES_DIR = Path(trip_tracker.__file__).parent / "templates"


def _render_footer(**overrides: str) -> str:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    register_globals(templates)
    templates.env.globals.update(overrides)
    html = templates.env.get_template("base.html").render(user=None)
    return html[html.index("<footer") : html.index("</footer>")]


def test_footer_ci_build_links_commit_and_shows_build_day() -> None:
    footer = _render_footer(
        git_sha="deadbeefcafef00d",
        git_sha_short="deadbee",
        build_date="2026-10-23T14:05:09.123Z",
        build_day="2026-10-23",
    )
    assert 'href="https://github.com/owine/trip-tracker/commit/deadbeefcafef00d"' in footer
    assert ">deadbee</a>" in footer
    assert 'datetime="2026-10-23T14:05:09.123Z"' in footer
    assert ">2026-10-23</time>" in footer


def test_footer_local_dev_build() -> None:
    footer = _render_footer(
        git_sha="unknown", git_sha_short="dev", build_date="unknown", build_day="unknown"
    )
    assert "dev" in footer
    assert "<a " not in footer
    assert "built" not in footer


def test_register_globals_wires_build_info(monkeypatch: pytest.MonkeyPatch) -> None:
    # The render tests above override globals; this pins the wiring itself.
    # Sentinels (not build_info's live values) so test_build_info's module
    # reloads can't make this order-dependent.
    for name in ("GIT_SHA", "GIT_SHA_SHORT", "BUILD_DATE", "BUILD_DAY"):
        monkeypatch.setattr(templating, name, f"sentinel-{name}")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templating.register_globals(templates)
    g = templates.env.globals
    assert g["git_sha"] == "sentinel-GIT_SHA"
    assert g["git_sha_short"] == "sentinel-GIT_SHA_SHORT"
    assert g["build_date"] == "sentinel-BUILD_DATE"
    assert g["build_day"] == "sentinel-BUILD_DAY"
    assert "app_version" not in g
