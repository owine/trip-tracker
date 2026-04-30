"""parsers.base: VendorParser ABC + ParseResult + registry."""

from __future__ import annotations

import re
from email.message import EmailMessage
from typing import ClassVar

from trip_tracker.parsers.base import (
    ParseResult,
    SegmentDraft,
    VendorParser,
    get_registry,
)


class _FakeBroad(VendorParser):
    """Fake parser for testing the registry. Uses a fake-only domain
    so it doesn't collide with real vendor parsers (e.g. American @aa.com).
    """

    name: ClassVar[str] = "fake_broad"
    sender_patterns: ClassVar[list[re.Pattern[str]]] = [re.compile(r"@fake-vendor\.test$")]

    def parse(self, msg: EmailMessage) -> ParseResult:
        return ParseResult(segments=[], confidence=0.0, source="rules:fake_broad")


class _FakeSpecific(VendorParser):
    name: ClassVar[str] = "fake_specific"
    sender_patterns: ClassVar[list[re.Pattern[str]]] = [re.compile(r"^special@fake-vendor\.test$")]

    def parse(self, msg: EmailMessage) -> ParseResult:
        return ParseResult(segments=[], confidence=0.0, source="rules:fake_specific")


def test_subclass_auto_registers() -> None:
    reg = get_registry()
    names = {p.name for p in reg}
    assert "fake_broad" in names
    assert "fake_specific" in names


def test_match_predicate() -> None:
    assert _FakeBroad.matches("noreply@fake-vendor.test")
    assert not _FakeBroad.matches("notifications@united.com")


def test_dispatch_specific_first() -> None:
    """Longer sender patterns sort first so a narrower regex shadows a broader one."""
    from trip_tracker.parsers.base import select_parsers

    matched = select_parsers("special@fake-vendor.test")
    assert matched[0].name == "fake_specific"
    assert matched[1].name == "fake_broad"


def test_segment_draft_minimal() -> None:
    from datetime import UTC, datetime

    SegmentDraft(
        type="flight",
        start_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_tz="America/New_York",
    )  # should not raise


def test_parse_result_warnings_optional() -> None:
    r = ParseResult(segments=[], confidence=0.5, source="json-ld")
    assert r.warnings == []
