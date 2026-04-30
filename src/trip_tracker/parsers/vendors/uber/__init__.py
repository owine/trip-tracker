"""Uber receipt parser — type='transfer'. Captures every ride per spec §2."""

from __future__ import annotations

import re
from datetime import datetime
from email.message import EmailMessage
from typing import ClassVar
from zoneinfo import ZoneInfo

from trip_tracker.parsers.base import ParseResult, SegmentDraft, VendorParser

_PICKUP = re.compile(r"(?:pickup|from)[:\s]+([^\n]+)", re.I)
_DROPOFF = re.compile(r"(?:drop[\s-]?off|to)[:\s]+([^\n]+)", re.I)
_DATETIME = re.compile(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})")


class UberParser(VendorParser):
    name: ClassVar[str] = "uber"
    sender_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"@uber\.com$", re.I),
        re.compile(r"@receipt\.uber\.com$", re.I),
    ]

    def parse(self, msg: EmailMessage) -> ParseResult:
        body = _extract_text(msg)
        pu = _PICKUP.search(body)
        do = _DROPOFF.search(body)
        dt = _DATETIME.search(body)
        if not (pu and do and dt):
            return ParseResult(segments=[], confidence=0.0, source="rules:uber")

        date_str, time_str = dt.groups()
        d = datetime.fromisoformat(f"{date_str}T{time_str}").replace(tzinfo=ZoneInfo("UTC"))

        seg = SegmentDraft(
            type="transfer",
            provider="Uber",
            start_at=d,
            start_tz="UTC",
            start_location={"name": pu.group(1).strip()},
            end_location={"name": do.group(1).strip()},
        )
        return ParseResult(segments=[seg], confidence=0.9, source="rules:uber")


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
