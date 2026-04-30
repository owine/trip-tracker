"""Auto-parameterized over every vendors/*/fixtures/*.eml.

Adding a new vendor PR drops new fixture files; this test picks them up
automatically. No test code changes needed.
"""

from __future__ import annotations

import json
from email import message_from_bytes
from email.policy import default as email_policy_default
from pathlib import Path
from typing import Any

import pytest

import trip_tracker.parsers.vendors  # noqa: F401  # triggers registration
from trip_tracker.parsers.base import VendorParser, get_registry

_VENDORS_DIR = Path(__file__).parent.parent / "src" / "trip_tracker" / "parsers" / "vendors"


def _fixture_pairs() -> list[tuple[str, Path, Path]]:
    pairs: list[tuple[str, Path, Path]] = []
    for vendor_dir in _VENDORS_DIR.iterdir():
        if not vendor_dir.is_dir():
            continue
        fixtures = vendor_dir / "fixtures"
        if not fixtures.is_dir():
            continue
        for eml in sorted(fixtures.glob("*.eml")):
            expected = eml.with_suffix(".expected.json")
            if expected.exists():
                pairs.append((f"{vendor_dir.name}/{eml.stem}", eml, expected))
    return pairs


def _find_parser(vendor: str) -> type[VendorParser]:
    for cls in get_registry():
        if cls.name == vendor:
            return cls
    raise RuntimeError(f"no registered parser for vendor: {vendor}")


@pytest.mark.parametrize(
    ("name", "eml_path", "expected_path"),
    _fixture_pairs(),
    ids=lambda x: x if isinstance(x, str) else None,
)
def test_vendor_fixture(name: str, eml_path: Path, expected_path: Path) -> None:
    """Each fixture is parsed by its vendor's parser; output is compared to expected."""
    vendor_name = name.split("/")[0]
    parser_cls = _find_parser(vendor_name)
    parser = parser_cls()

    msg = message_from_bytes(eml_path.read_bytes(), policy=email_policy_default)
    result = parser.parse(msg)
    expected: dict[str, Any] = json.loads(expected_path.read_text())

    assert result.source == expected["source"], f"{name}: source mismatch"
    assert result.confidence >= expected["confidence"] - 0.001, f"{name}: confidence too low"
    assert len(result.segments) == len(expected["segments"]), f"{name}: segment count mismatch"

    for actual_seg, expected_seg in zip(result.segments, expected["segments"], strict=True):
        for key, expected_val in expected_seg.items():
            actual_val = getattr(actual_seg, key)
            assert actual_val == expected_val, (
                f"{name}: {key} mismatch — got {actual_val!r}, expected {expected_val!r}"
            )


def test_at_least_one_fixture_pair_exists() -> None:
    """If this fails, a vendor parser exists but has no fixtures (CI gate)."""
    assert len(_fixture_pairs()) >= 1


@pytest.mark.parametrize("parser_cls", get_registry(), ids=lambda c: c.name)
def test_vendor_returns_empty_on_unmatchable_email(parser_cls: type[VendorParser]) -> None:
    """Each vendor parser returns segments=[], confidence=0.0 when its regexes
    don't match the email body. Covers the 'required fields not found' early
    return that fixtures don't otherwise exercise."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = "Unrelated"
    msg["From"] = "noreply@example-not-real-vendor.invalid"
    msg["To"] = "oliver@trips.example.com"
    msg.set_content("This email contains no parseable itinerary data whatsoever.")

    result = parser_cls().parse(msg)
    assert result.segments == []
    assert result.confidence == 0.0
    assert result.source.startswith("rules:")
