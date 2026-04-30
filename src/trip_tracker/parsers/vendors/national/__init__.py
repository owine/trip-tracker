"""National parser pack — type='car'. Covers National + Enterprise + Alamo."""

from __future__ import annotations

import re
from datetime import datetime
from email.message import EmailMessage
from typing import ClassVar
from zoneinfo import ZoneInfo

from trip_tracker.parsers.base import ParseResult, SegmentDraft, VendorParser

_PICKUP = re.compile(
    r"pick[\s-]?up[:\s]+([\w\s]+?)\s+(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", re.I
)
_DROPOFF = re.compile(
    r"drop[\s-]?off[:\s]+([\w\s]+?)\s+(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", re.I
)
_CONFIRMATION = re.compile(r"confirmation[:\s]+([A-Z0-9]{6,12})", re.I)


class NationalParser(VendorParser):
    name: ClassVar[str] = "national"
    sender_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"@(nationalcar|enterprise|alamo|enterpriseplus)\.com$", re.I),
    ]

    def parse(self, msg: EmailMessage) -> ParseResult:
        body = _extract_text(msg)
        pu = _PICKUP.search(body)
        do = _DROPOFF.search(body)
        conf = _CONFIRMATION.search(body)
        if not (pu and do):
            return ParseResult(segments=[], confidence=0.0, source="rules:national")

        pu_loc, py, pm, pd, ph, pmm = pu.groups()
        do_loc, dy, dm, dd, dh, dmm = do.groups()

        seg = SegmentDraft(
            type="car",
            confirmation_number=conf.group(1) if conf else None,
            provider="National",
            start_at=datetime(int(py), int(pm), int(pd), int(ph), int(pmm), tzinfo=ZoneInfo("UTC")),
            start_tz="UTC",
            end_at=datetime(int(dy), int(dm), int(dd), int(dh), int(dmm), tzinfo=ZoneInfo("UTC")),
            end_tz="UTC",
            start_location={"name": pu_loc.strip()},
            end_location={"name": do_loc.strip()},
        )
        return ParseResult(segments=[seg], confidence=0.9, source="rules:national")


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
