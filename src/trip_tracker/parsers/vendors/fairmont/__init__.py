"""Fairmont (Accor brand) parser pack — type='lodging'."""

from __future__ import annotations

import re
from datetime import datetime
from email.message import EmailMessage
from typing import ClassVar
from zoneinfo import ZoneInfo

from trip_tracker.parsers.base import ParseResult, SegmentDraft, VendorParser

_CHECK_IN = re.compile(r"check[\s-]?in[:\s]+(\d{4}-\d{2}-\d{2})", re.I)
_CHECK_OUT = re.compile(r"check[\s-]?out[:\s]+(\d{4}-\d{2}-\d{2})", re.I)
_HOTEL_NAME = re.compile(r"(Fairmont[^\n,]+)", re.I)
_CONFIRMATION = re.compile(r"confirmation[:\s]+([A-Z0-9]{6,12})", re.I)


class FairmontParser(VendorParser):
    name: ClassVar[str] = "fairmont"
    sender_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"@fairmont\.com$", re.I),
        re.compile(r"@(reservations|email)\.fairmont\.com$", re.I),
    ]

    def parse(self, msg: EmailMessage) -> ParseResult:
        body = _extract_text(msg)
        ci = _CHECK_IN.search(body)
        co = _CHECK_OUT.search(body)
        name = _HOTEL_NAME.search(body)
        conf = _CONFIRMATION.search(body)

        if not (ci and co and name):
            return ParseResult(segments=[], confidence=0.0, source="rules:fairmont")

        ci_dt = datetime.fromisoformat(ci.group(1)).replace(hour=15, tzinfo=ZoneInfo("UTC"))
        co_dt = datetime.fromisoformat(co.group(1)).replace(hour=11, tzinfo=ZoneInfo("UTC"))

        seg = SegmentDraft(
            type="lodging",
            confirmation_number=conf.group(1) if conf else None,
            provider="Fairmont",
            start_at=ci_dt,
            start_tz="UTC",
            end_at=co_dt,
            end_tz="UTC",
            start_location={"name": name.group(1).strip()},
        )
        return ParseResult(segments=[seg], confidence=0.9, source="rules:fairmont")


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
