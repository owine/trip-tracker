"""Anthropic tool-use schemas for the parser fallback.

The Haiku call uses tool-use to force structured output matching
`SegmentDraft`. We mirror SegmentDraft fields here as the tool's
input schema (Anthropic's ToolInputSchema).
"""

from __future__ import annotations

from typing import Any

EXTRACT_SEGMENTS_TOOL: dict[str, Any] = {
    "name": "extract_segments",
    "description": (
        "Extract zero or more travel segments from the email body. "
        "Each segment is one leg: a flight, lodging stay, car rental, "
        "train, transfer (taxi/Uber/private car), or activity (event/tour). "
        "Return an empty list ONLY if the email contains no itinerary content "
        "(marketing, receipts, etc.). When unsure, prefer extracting with low "
        "confidence over returning empty."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "segments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["flight", "lodging", "car", "train", "transfer", "activity"],
                        },
                        "status": {
                            "type": "string",
                            "enum": ["confirmed", "tentative", "cancelled"],
                        },
                        "confirmation_number": {"type": ["string", "null"]},
                        "provider": {"type": ["string", "null"]},
                        "start_at": {
                            "type": "string",
                            "description": "ISO 8601 datetime with timezone offset",
                        },
                        "start_tz": {"type": "string", "description": "IANA tz name"},
                        "end_at": {"type": ["string", "null"]},
                        "end_tz": {"type": ["string", "null"]},
                        "start_location": {
                            "type": ["object", "null"],
                            "additionalProperties": True,
                        },
                        "end_location": {
                            "type": ["object", "null"],
                            "additionalProperties": True,
                        },
                        "details": {"type": "object", "additionalProperties": True},
                    },
                    "required": ["type", "start_at", "start_tz"],
                },
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": (
                    "Self-rated confidence in this extraction. Use ≥0.85 for "
                    "obvious itineraries (clear sender + clear fields), 0.5-0.85 "
                    "for plausible-but-noisy cases, and <0.5 if you suspect "
                    "this isn't actually an itinerary."
                ),
            },
        },
        "required": ["segments", "confidence"],
    },
}

SYSTEM_PROMPT = """\
You parse forwarded confirmation emails into structured travel segments.

Input: the raw text/HTML of one email.
Output: a single tool call to extract_segments with the segments array.

Rules:
- One leg per segment (a round-trip flight = 2 segments).
- Datetimes MUST include timezone info (offset OR IANA tz name in start_tz/end_tz).
- If you can't determine a timezone, use the airport's tz for flights or the city's
  tz for lodging. Default to UTC only as a last resort.
- For type='lodging', start_at = check-in, end_at = check-out.
- Marketing emails, receipts (non-itinerary), and confirmations of past travel:
  return segments=[] with confidence ≥0.85.
- Cap your self-rated confidence at 0.85 even when very sure — vendor-specific
  rules will override your output if they later cover this sender.

Pricing (when present, populate the segment's `details` object):
- `details.total_price` — number, the total amount the user actually paid for
  THIS segment (or the full booking if it's a single segment). Examples:
  "Total amount: $351.76" → 351.76; "Total: USD 1,250.00" → 1250.0;
  "Vous avez payé 89,90 EUR" → 89.90 (note European decimal comma).
- `details.price_currency` — uppercase 3-letter ISO 4217 code: "USD", "EUR",
  "GBP", "JPY", etc. Even if the email shows "$" with no explicit code, infer
  from country context (e.g. an Air France email almost always means EUR
  unless it's a US domestic itinerary).
- ONLY populate pricing when there is a clearly-stated total. Do NOT extract:
    * marketing prices ("from $99")
    * per-passenger or per-component breakdowns when the total isn't given
    * estimates or "you saved $X" callouts
  When in doubt, omit pricing rather than guess.
- For multi-segment bookings (a ReservationPackage with flight + hotel for
  one total amount), put the total_price ONLY on the FIRST segment, not all.

Booking timestamp (when present):
- `details.booking_time` — ISO 8601 string, when the user actually paid for
  the booking (not when they travel). Look for "Booked on", "Date of purchase",
  "Order date", or "Confirmation sent at". Used downstream to set the
  expense's incurred_on date correctly. If absent, omit the field.
"""
