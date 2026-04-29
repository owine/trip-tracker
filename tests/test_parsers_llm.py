"""LLM strategy with mocked Anthropic SDK."""

from __future__ import annotations

from email.message import EmailMessage
from unittest.mock import AsyncMock, MagicMock

import pytest

from trip_tracker.parsers.llm import LLMClient, parse_with_llm


def _msg() -> EmailMessage:
    m = EmailMessage()
    m["Subject"] = "Sample"
    m["From"] = "x@y.com"
    m["To"] = "oliver@trips.example.com"
    m.set_content("Plain content for the LLM to read.")
    return m


def _fake_response(*, tool_input: dict, input_tokens: int = 100, output_tokens: int = 50):
    """Build a MagicMock matching anthropic.types.Message shape."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = "extract_segments"
    block.input = tool_input
    msg = MagicMock()
    msg.content = [block]
    msg.usage = MagicMock()
    msg.usage.input_tokens = input_tokens
    msg.usage.output_tokens = output_tokens
    return msg


@pytest.mark.asyncio
async def test_parse_decodes_tool_use() -> None:
    client = MagicMock(spec=LLMClient)
    client.call = AsyncMock(
        return_value=_fake_response(
            tool_input={
                "segments": [
                    {
                        "type": "flight",
                        "status": "confirmed",
                        "start_at": "2026-06-01T09:00:00-04:00",
                        "start_tz": "America/New_York",
                        "end_at": "2026-06-01T22:00:00+02:00",
                        "end_tz": "Europe/Paris",
                        "start_location": {"iata": "JFK"},
                        "end_location": {"iata": "CDG"},
                        "details": {"flight_number": "DL44"},
                        "confirmation_number": "ABC123",
                        "provider": "Delta",
                    }
                ],
                "confidence": 0.9,  # will be clamped to 0.85
            },
            input_tokens=100,
            output_tokens=50,
        )
    )
    outcome = await parse_with_llm(client, _msg(), hint=None)
    assert outcome.result.confidence == 0.85
    assert outcome.result.source == "llm:haiku-4-5"
    assert len(outcome.result.segments) == 1
    assert outcome.input_tokens == 100
    assert outcome.output_tokens == 50


@pytest.mark.asyncio
async def test_parse_with_hint_appends_to_user_message() -> None:
    client = MagicMock(spec=LLMClient)
    client.call = AsyncMock(
        return_value=_fake_response(tool_input={"segments": [], "confidence": 0.9})
    )
    await parse_with_llm(client, _msg(), hint="This is a return flight")
    _args, kwargs = client.call.call_args
    user_msg = kwargs["user_content"]
    assert "This is a return flight" in user_msg


@pytest.mark.asyncio
async def test_parse_no_tool_use_returns_empty() -> None:
    """Model that doesn't invoke the tool returns an empty result with a warning."""
    client = MagicMock(spec=LLMClient)
    response = MagicMock()
    response.content = [MagicMock(type="text")]  # not tool_use
    response.usage = MagicMock(input_tokens=50, output_tokens=10)
    client.call = AsyncMock(return_value=response)
    outcome = await parse_with_llm(client, _msg(), hint=None)
    assert outcome.result.segments == []
    assert outcome.result.confidence == 0.0
    assert outcome.result.warnings  # has at least one warning
    assert outcome.input_tokens == 50
    assert outcome.output_tokens == 10
