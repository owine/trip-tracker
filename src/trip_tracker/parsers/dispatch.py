"""Strategy chain: JSON-LD → matched vendor → Haiku.

Caller (the worker) handles persistence + clustering + status assignment.
This module is pure orchestration over an EmailMessage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from email.message import EmailMessage

from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.parsers.base import ParseResult, select_parsers
from trip_tracker.parsers.budget import is_over_budget
from trip_tracker.parsers.forwarded import effective_from
from trip_tracker.parsers.jsonld import parse_jsonld
from trip_tracker.parsers.llm import LLMClient, parse_with_llm

logger = logging.getLogger(__name__)


@dataclass
class ParseOutcome:
    result: ParseResult
    budget_skipped: bool = False  # True when LLM was needed but budget exhausted
    llm_input_tokens: int = 0  # populated when strategy 3 actually ran
    llm_output_tokens: int = 0  # populated when strategy 3 actually ran


async def dispatch_parse(
    msg: EmailMessage,
    *,
    llm_client: LLMClient,
    db: AsyncSession,
    cap_cents: int,
    hint: str | None = None,
) -> ParseOutcome:
    """Run JSON-LD, then matched vendor, then Haiku. Return the first
    high-confidence result, or the best low-confidence result, or empty.

    Strategy fall-through:
      - confidence ≥ ceiling (per strategy) → return immediately
      - confidence < ceiling → keep best so far, try next strategy
      - all strategies done → return best (may be confidence=0)
    """
    best: ParseResult = ParseResult(segments=[], confidence=0.0, source="none")

    # Strategy 1 — JSON-LD
    try:
        r1 = parse_jsonld(msg)
    except Exception as exc:
        logger.warning("jsonld dispatch error: %s", exc)
        r1 = ParseResult(segments=[], confidence=0.0, source="json-ld")
    if r1.segments and r1.confidence >= 0.9:
        return ParseOutcome(result=r1)
    if r1.confidence > best.confidence:
        best = r1

    # Strategy 2 — matched vendor.
    # Use `effective_from` so user-forwarded emails (where the outer From: is
    # the user, not the vendor) still hit the right vendor pack. For direct
    # deliveries this returns the outer From: unchanged.
    from_addr = effective_from(msg)
    for parser_cls in select_parsers(from_addr):
        try:
            r2 = parser_cls().parse(msg)
        except Exception as exc:
            logger.warning("vendor %s raised: %s", parser_cls.name, exc)
            continue
        if r2.confidence >= parser_cls.confidence_floor:
            return ParseOutcome(result=r2)
        if r2.confidence > best.confidence:
            best = r2

    # Strategy 3 — LLM (budget gate)
    if await is_over_budget(db, cap_cents=cap_cents):
        return ParseOutcome(result=best, budget_skipped=True)

    try:
        outcome = await parse_with_llm(llm_client, msg, hint=hint)
    except Exception as exc:
        logger.warning("llm dispatch error: %s", exc)
        return ParseOutcome(result=best)

    r3 = outcome.result
    if r3.confidence > best.confidence:
        best = r3
    return ParseOutcome(
        result=best,
        llm_input_tokens=outcome.input_tokens,
        llm_output_tokens=outcome.output_tokens,
    )
