"""Dispatcher: JSON-LD → vendor → LLM, with confidence floor + budget."""

from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trip_tracker.parsers.base import ParseResult, SegmentDraft
from trip_tracker.parsers.dispatch import dispatch_parse
from trip_tracker.parsers.llm import LLMOutcome


def _msg() -> EmailMessage:
    m = EmailMessage()
    m["From"] = "x@y.com"
    m.set_content("body")
    return m


def _draft() -> SegmentDraft:
    return SegmentDraft(
        type="flight",
        start_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_tz="UTC",
    )


@pytest.mark.asyncio
async def test_jsonld_short_circuits() -> None:
    """If JSON-LD returns confidence ≥ ceiling, vendor + LLM never called."""
    with (
        patch("trip_tracker.parsers.dispatch.parse_jsonld") as jsonld,
        patch("trip_tracker.parsers.dispatch.select_parsers") as vendors,
        patch("trip_tracker.parsers.dispatch.parse_with_llm") as llm,
    ):
        jsonld.return_value = ParseResult(segments=[_draft()], confidence=0.95, source="json-ld")
        outcome = await dispatch_parse(
            _msg(), llm_client=MagicMock(), db=MagicMock(), cap_cents=100
        )
        assert outcome.result.source == "json-ld"
        vendors.assert_not_called()
        llm.assert_not_called()


@pytest.mark.asyncio
async def test_vendor_runs_when_jsonld_empty() -> None:
    fake_vendor_cls = MagicMock()
    fake_vendor_cls.confidence_floor = 0.85
    fake_vendor_cls.return_value.parse.return_value = ParseResult(
        segments=[_draft()],
        confidence=0.9,
        source="rules:fake",
    )
    with (
        patch(
            "trip_tracker.parsers.dispatch.parse_jsonld",
            return_value=ParseResult(segments=[], confidence=0.0, source="json-ld"),
        ),
        patch("trip_tracker.parsers.dispatch.select_parsers", return_value=[fake_vendor_cls]),
        patch("trip_tracker.parsers.dispatch.parse_with_llm") as llm,
    ):
        outcome = await dispatch_parse(
            _msg(), llm_client=MagicMock(), db=MagicMock(), cap_cents=100
        )
        assert outcome.result.source == "rules:fake"
        llm.assert_not_called()


@pytest.mark.asyncio
async def test_llm_runs_when_vendor_below_floor() -> None:
    fake_vendor_cls = MagicMock()
    fake_vendor_cls.confidence_floor = 0.85
    fake_vendor_cls.return_value.parse.return_value = ParseResult(
        segments=[_draft()],
        confidence=0.4,
        source="rules:fake",
    )
    llm_outcome = LLMOutcome(
        result=ParseResult(segments=[_draft()], confidence=0.85, source="llm:haiku-4-5"),
        input_tokens=100,
        output_tokens=50,
    )
    with (
        patch(
            "trip_tracker.parsers.dispatch.parse_jsonld",
            return_value=ParseResult(segments=[], confidence=0.0, source="json-ld"),
        ),
        patch("trip_tracker.parsers.dispatch.select_parsers", return_value=[fake_vendor_cls]),
        patch("trip_tracker.parsers.dispatch.is_over_budget", new=AsyncMock(return_value=False)),
        patch(
            "trip_tracker.parsers.dispatch.parse_with_llm", new=AsyncMock(return_value=llm_outcome)
        ) as llm,
    ):
        outcome = await dispatch_parse(
            _msg(), llm_client=MagicMock(), db=MagicMock(), cap_cents=100
        )
        llm.assert_awaited_once()
        assert outcome.result.source == "llm:haiku-4-5"
        assert outcome.llm_input_tokens == 100
        assert outcome.llm_output_tokens == 50


@pytest.mark.asyncio
async def test_budget_skips_llm() -> None:
    """Over budget: LLM step skipped; outcome carries the best earlier result."""
    with (
        patch(
            "trip_tracker.parsers.dispatch.parse_jsonld",
            return_value=ParseResult(segments=[], confidence=0.0, source="json-ld"),
        ),
        patch("trip_tracker.parsers.dispatch.select_parsers", return_value=[]),
        patch("trip_tracker.parsers.dispatch.is_over_budget", new=AsyncMock(return_value=True)),
        patch("trip_tracker.parsers.dispatch.parse_with_llm") as llm,
    ):
        outcome = await dispatch_parse(
            _msg(), llm_client=MagicMock(), db=MagicMock(), cap_cents=100
        )
        llm.assert_not_called()
        assert outcome.budget_skipped is True
        assert outcome.result.segments == []
