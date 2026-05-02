"""Trainline parser pack — type='train'.

Trainline emails carry schema.org JSON-LD with `TrainReservation`, so the
JSON-LD strategy normally handles them at confidence 0.95. This pack exists as
a fallback for cases where JSON-LD is absent/mangled, and as enrichment when
JSON-LD's `trainName`/`trainNumber` are empty (Trainline emits them empty in
practice — the carrier+train number live only in the HTML body).
"""

from __future__ import annotations

import re
from datetime import datetime
from email.message import EmailMessage
from typing import ClassVar
from zoneinfo import ZoneInfo

from trip_tracker.parsers._email_text import extract_text
from trip_tracker.parsers.base import ParseResult, SegmentDraft, VendorParser

# "SNCF Voyageurs TGV INOUI 8536" or "Eurostar 9114" — operator + train number.
_OPERATOR_TRAIN = re.compile(
    r"((?:SNCF\s+Voyageurs\s+)?(?:TGV\s+INOUI|TGV|Eurostar|Intercit[ée]s?))\s+(\d{3,5})",
    re.I,
)
# "Ticket PNR reference\n... 86FAEY" — letters+digits, 6 chars in samples.
_PNR = re.compile(r"\b([A-Z0-9]{6})\b")
# "13:52 Bordeaux St-Jean" — time + station name. The U+2019 in the char
# class lets us match French station names rendered with typographic
# punctuation (e.g. "Saint-Cyr-l'Ecole"); ASCII ' covers plain rendering.
_DEP_LINE = re.compile(r"(\d{2}):(\d{2})\s+([A-Z][\w\s\-’']+?)(?=\s*(?:\n|$))")  # noqa: RUF001
_DATE_LINE = re.compile(
    r"(?:Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday),\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(\d{1,2}),?\s+(\d{4})",
    re.I,
)


class TrainlineParser(VendorParser):
    name: ClassVar[str] = "trainline"
    sender_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"@(info\.)?thetrainline\.com$", re.I),
    ]

    def parse(self, msg: EmailMessage) -> ParseResult:
        body = extract_text(msg)
        op_match = _OPERATOR_TRAIN.search(body)
        pnr_match = _PNR.search(body)
        dep_match = _DEP_LINE.search(body)
        date_match = _DATE_LINE.search(body)

        if not (op_match and dep_match and date_match):
            return ParseResult(segments=[], confidence=0.0, source="rules:trainline")

        hh, mm = int(dep_match.group(1)), int(dep_match.group(2))
        month_name, day_str, year_str = date_match.groups()
        month = datetime.strptime(month_name[:3], "%b").month
        start_at = datetime(int(year_str), month, int(day_str), hh, mm, tzinfo=ZoneInfo("UTC"))

        seg = SegmentDraft(
            type="train",
            confirmation_number=pnr_match.group(1) if pnr_match else None,
            provider="Trainline",
            start_at=start_at,
            start_tz="UTC",
            start_location={"name": dep_match.group(3).strip()},
            details={
                "operator": op_match.group(1).strip(),
                "train_number": op_match.group(2),
            },
        )
        return ParseResult(segments=[seg], confidence=0.85, source="rules:trainline")
