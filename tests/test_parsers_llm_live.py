"""Live-LLM smoke test: real Haiku call to verify prompt + tool schema.

Marked @pytest.mark.live_llm. Skipped in CI. Run locally before each release:

    ANTHROPIC_API_KEY=sk-... uv run pytest -m live_llm -v
"""

from __future__ import annotations

import os
from email.message import EmailMessage

import pytest

from trip_tracker.config import Settings
from trip_tracker.parsers.llm import LLMClient, parse_with_llm


@pytest.mark.live_llm
@pytest.mark.skipif(
    os.getenv("ANTHROPIC_API_KEY", "sk-ant-test") == "sk-ant-test",
    reason="needs a real ANTHROPIC_API_KEY (test placeholder doesn't suffice)",
)
@pytest.mark.asyncio
async def test_haiku_round_trip_with_canonical_email() -> None:
    msg = EmailMessage()
    msg["Subject"] = "Your AirExample flight on 2026-06-01"
    msg["From"] = "confirmations@airexample.com"
    msg["To"] = "oliver@trips.example.com"
    msg.set_content(
        "Confirmation: ABC123\n"
        "Flight: AE42, JFK -> CDG\n"
        "Departs 2026-06-01 09:00 EDT, arrives 2026-06-01 22:00 CEST\n"
        "Seat: 12A\n"
    )

    settings = Settings()
    client = LLMClient(settings)
    outcome = await parse_with_llm(client, msg, hint=None)

    assert outcome.result.source == "llm:haiku-4-5"
    assert outcome.result.confidence <= 0.85
    assert len(outcome.result.segments) >= 1
    seg = outcome.result.segments[0]
    assert seg.type == "flight"
