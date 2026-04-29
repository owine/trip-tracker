"""Anthropic Haiku 4.5 strategy: tool-use forced structured output.

Two layers:
- LLMClient: thin wrapper over anthropic.AsyncAnthropic, async .call() method.
- parse_with_llm(client, msg, hint): orchestrates message construction, tool use,
  response decoding, confidence clamping. Returns LLMOutcome (ParseResult + token counts).

Budget enforcement happens in dispatch.py — this module assumes the call is
allowed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from anthropic import AsyncAnthropic

from trip_tracker.config import Settings
from trip_tracker.parsers.base import ParseResult, SegmentDraft
from trip_tracker.schemas.llm import EXTRACT_SEGMENTS_TOOL, SYSTEM_PROMPT

logger = logging.getLogger(__name__)

HAIKU_CONFIDENCE_CEILING = 0.85


class LLMClient:
    """Thin Anthropic wrapper. Single call() method to keep mocking trivial."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())

    async def call(self, *, user_content: str) -> Any:
        """One Haiku call with prompt-caching enabled on the system prompt."""
        return await self._client.messages.create(
            model=self._settings.llm_model,
            max_tokens=2048,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[EXTRACT_SEGMENTS_TOOL],
            tool_choice={"type": "tool", "name": "extract_segments"},
            messages=[{"role": "user", "content": user_content}],
        )


def _msg_to_text(msg: EmailMessage) -> str:
    parts = [
        f"Subject: {msg.get('Subject', '')}",
        f"From: {msg.get('From', '')}",
        f"To: {msg.get('To', '')}",
        f"Date: {msg.get('Date', '')}",
        "",
    ]
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    charset = part.get_content_charset() or "utf-8"
                    parts.append(payload.decode(charset, errors="replace"))
                break
    else:
        if msg.get_content_type().startswith("text/"):
            payload = msg.get_payload(decode=True)
            if isinstance(payload, bytes):
                parts.append(payload.decode(msg.get_content_charset() or "utf-8", errors="replace"))
    return "\n".join(parts)


@dataclass
class LLMOutcome:
    """ParseResult plus the token counts so callers can record exact LLM cost."""

    result: ParseResult
    input_tokens: int
    output_tokens: int


async def parse_with_llm(client: LLMClient, msg: EmailMessage, *, hint: str | None) -> LLMOutcome:
    """Run Haiku once, decode the tool-use response, return LLMOutcome.

    `hint` (optional): short user-supplied note appended to the user message
    (the "Re-ask Claude with hint" inbox action).

    Returns LLMOutcome — the worker uses input/output_tokens to call
    `cost_cents_for_usage` and `record_usage` (Task 16).
    """
    user_text = _msg_to_text(msg)
    if hint:
        user_text += f"\n\n[User hint: {hint}]"

    response = await client.call(user_content=user_text)

    in_tok = int(getattr(response.usage, "input_tokens", 0) or 0)
    out_tok = int(getattr(response.usage, "output_tokens", 0) or 0)

    tool_input: dict[str, Any] | None = None
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "extract_segments":
            tool_input = block.input
            break
    if tool_input is None:
        return LLMOutcome(
            result=ParseResult(
                segments=[],
                confidence=0.0,
                source="llm:haiku-4-5",
                warnings=["model did not invoke extract_segments tool"],
            ),
            input_tokens=in_tok,
            output_tokens=out_tok,
        )

    raw_conf = float(tool_input.get("confidence", 0.0))
    confidence = min(raw_conf, HAIKU_CONFIDENCE_CEILING)
    segments = [SegmentDraft.model_validate(s) for s in tool_input.get("segments", [])]

    return LLMOutcome(
        result=ParseResult(
            segments=segments,
            confidence=confidence,
            source="llm:haiku-4-5",
        ),
        input_tokens=in_tok,
        output_tokens=out_tok,
    )
