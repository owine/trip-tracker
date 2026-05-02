"""Blacklane parser pack — premium private-car service, type='transfer'."""

from __future__ import annotations

import re
from datetime import datetime
from email.message import EmailMessage
from typing import ClassVar
from zoneinfo import ZoneInfo

from trip_tracker.parsers._email_text import extract_text
from trip_tracker.parsers.base import ParseResult, SegmentDraft, VendorParser

_PICKUP = re.compile(r"(?:pickup|from)[:\s]+([^\n]+)", re.I)
_DROPOFF = re.compile(r"(?:drop[\s-]?off|to)[:\s]+([^\n]+)", re.I)
_DATETIME = re.compile(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})")


class BlacklaneParser(VendorParser):
    name: ClassVar[str] = "blacklane"
    sender_patterns: ClassVar[list[re.Pattern[str]]] = [
        # Apex + subdomains. No documented suffix-branded label variants for
        # Blacklane currently, but matching subdomains future-proofs against
        # `notifications@email.blacklane.com`-style transactional streams.
        re.compile(r"@([\w.-]+\.)?blacklane\.com$", re.I),
    ]

    def parse(self, msg: EmailMessage) -> ParseResult:
        body = extract_text(msg)
        pu = _PICKUP.search(body)
        do = _DROPOFF.search(body)
        dt = _DATETIME.search(body)
        if not (pu and do and dt):
            return ParseResult(segments=[], confidence=0.0, source="rules:blacklane")

        date_str, time_str = dt.groups()
        d = datetime.fromisoformat(f"{date_str}T{time_str}").replace(tzinfo=ZoneInfo("UTC"))

        seg = SegmentDraft(
            type="transfer",
            provider="Blacklane",
            start_at=d,
            start_tz="UTC",
            start_location={"name": pu.group(1).strip()},
            end_location={"name": do.group(1).strip()},
        )
        return ParseResult(segments=[seg], confidence=0.9, source="rules:blacklane")
