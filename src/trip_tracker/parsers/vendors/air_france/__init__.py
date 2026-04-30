"""Air France parser pack."""

from __future__ import annotations

import re
from datetime import datetime
from email.message import EmailMessage
from typing import ClassVar
from zoneinfo import ZoneInfo

from trip_tracker.parsers.base import ParseResult, SegmentDraft, VendorParser

_FLIGHT_NUM = re.compile(r"\b(AF|KL)\s?(\d{2,4})\b")
_IATA_PAIR = re.compile(r"\b([A-Z]{3})\s*(?:→|->|to)\s*([A-Z]{3})\b")
_DATE_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", flags=re.IGNORECASE)
_CONFIRMATION = re.compile(r"\b(?:confirmation|reservation|réservation)[\s:]+([A-Z0-9]{6,8})", re.I)


class AirFranceParser(VendorParser):
    name: ClassVar[str] = "air_france"
    sender_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"@airfrance\.(fr|com)$", re.I),
        re.compile(r"flyingblue@airfrance\.com$", re.I),
    ]

    def parse(self, msg: EmailMessage) -> ParseResult:
        body = _extract_text(msg)
        flight_match = _FLIGHT_NUM.search(body)
        iata_match = _IATA_PAIR.search(body)
        dt_matches = _DATE_TIME.findall(body)
        conf_match = _CONFIRMATION.search(body)

        if not (flight_match and iata_match and dt_matches):
            return ParseResult(
                segments=[],
                confidence=0.0,
                source="rules:air_france",
                warnings=["could not locate flight number + IATA pair + datetime"],
            )

        flight_no = f"{flight_match.group(1)}{flight_match.group(2)}"
        origin, dest = iata_match.group(1), iata_match.group(2)
        y, m, d, hh, mm = dt_matches[0]
        start_at = datetime(int(y), int(m), int(d), int(hh), int(mm), tzinfo=ZoneInfo("UTC"))

        seg = SegmentDraft(
            type="flight",
            confirmation_number=conf_match.group(1) if conf_match else None,
            provider="Air France",
            start_at=start_at,
            start_tz="UTC",
            start_location={"iata": origin},
            end_location={"iata": dest},
            details={"flight_number": flight_no},
        )
        return ParseResult(segments=[seg], confidence=0.9, source="rules:air_france")


def _extract_text(msg: EmailMessage) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return str(msg.get_payload() or "")
