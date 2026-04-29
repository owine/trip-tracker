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


class _FakeAA(VendorParser):
    name: ClassVar[str] = "fake_aa"
    sender_patterns: ClassVar[list[re.Pattern[str]]] = [re.compile(r"@aa\.com$")]

    def parse(self, msg: EmailMessage) -> ParseResult:
        return ParseResult(segments=[], confidence=0.0, source="rules:fake_aa")


class _FakeAASpecific(VendorParser):
    name: ClassVar[str] = "fake_aa_aadvantage"
    sender_patterns: ClassVar[list[re.Pattern[str]]] = [re.compile(r"^aadvantage@aa\.com$")]

    def parse(self, msg: EmailMessage) -> ParseResult:
        return ParseResult(segments=[], confidence=0.0, source="rules:fake_aa_aadv")


def test_subclass_auto_registers() -> None:
    reg = get_registry()
    names = {p.name for p in reg}
    assert "fake_aa" in names
    assert "fake_aa_aadvantage" in names


def test_match_predicate() -> None:
    assert _FakeAA.matches("noreply@aa.com")
    assert not _FakeAA.matches("notifications@united.com")


def test_dispatch_specific_first() -> None:
    """Longer sender patterns sort first so a narrower regex shadows a broader one."""
    from trip_tracker.parsers.base import select_parsers

    matched = select_parsers("aadvantage@aa.com")
    assert matched[0].name == "fake_aa_aadvantage"
    assert matched[1].name == "fake_aa"


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
