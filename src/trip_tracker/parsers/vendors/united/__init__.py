"""United Airlines parser pack."""

from __future__ import annotations

import re
from datetime import datetime
from email.message import EmailMessage
from typing import ClassVar
from zoneinfo import ZoneInfo

from trip_tracker.parsers._email_text import extract_text
from trip_tracker.parsers.base import ParseResult, SegmentDraft, VendorParser

_FLIGHT_NUM = re.compile(r"\bUA\s?(\d{1,4})\b")
_IATA_PAIR = re.compile(r"\b([A-Z]{3})\s*(?:→|->|to)\s*([A-Z]{3})\b")
_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_TIME = re.compile(r"(\d{2}):(\d{2})")
_CONFIRMATION = re.compile(r"(?:confirmation|conf #?)[:\s]+([A-Z0-9]{6})", re.I)


class UnitedParser(VendorParser):
    name: ClassVar[str] = "united"
    sender_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"@united\.com$", re.I),
        re.compile(r"@unitedairlines\.com$", re.I),
    ]

    def parse(self, msg: EmailMessage) -> ParseResult:
        body = extract_text(msg)
        flight = _FLIGHT_NUM.search(body)
        iata = _IATA_PAIR.search(body)
        date_m = _DATE.search(body)
        time_m = _TIME.search(body)
        conf = _CONFIRMATION.search(body)

        if not (flight and iata and date_m and time_m):
            return ParseResult(segments=[], confidence=0.0, source="rules:united")

        y, m, d = (int(g) for g in date_m.groups())
        hh, mm = (int(g) for g in time_m.groups())
        start_at = datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("UTC"))

        seg = SegmentDraft(
            type="flight",
            confirmation_number=conf.group(1) if conf else None,
            provider="United Airlines",
            start_at=start_at,
            start_tz="UTC",
            start_location={"iata": iata.group(1)},
            end_location={"iata": iata.group(2)},
            details={"flight_number": f"UA{flight.group(1)}"},
        )
        return ParseResult(segments=[seg], confidence=0.9, source="rules:united")
