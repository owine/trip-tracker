"""Chase Travel parser pack — multi-segment booking portal.

Chase Travel emails commonly contain flight + hotel + car bundles. We
parse each section into its own SegmentDraft. Most Chase Travel templates
embed JSON-LD which is handled by the upstream JSON-LD strategy at higher
confidence; this parser is the fallback for plain-text Chase Travel
emails or future template changes.
"""

from __future__ import annotations

import re
from datetime import datetime
from email.message import EmailMessage
from typing import ClassVar
from zoneinfo import ZoneInfo

from trip_tracker.parsers._email_text import extract_text
from trip_tracker.parsers.base import ParseResult, SegmentDraft, VendorParser

# Section markers, label-based extraction
_FLIGHT_BLOCK = re.compile(
    r"Flight:.*?(?=\n\s*(?:Hotel|Car|Confirmation|$))",
    re.I | re.S,
)
_HOTEL_BLOCK = re.compile(
    r"Hotel:.*?(?=\n\s*(?:Flight|Car|Confirmation|$))",
    re.I | re.S,
)
_CAR_BLOCK = re.compile(
    r"Car(?:\s+rental)?:.*?(?=\n\s*(?:Flight|Hotel|Confirmation|$))",
    re.I | re.S,
)

_FLIGHT_NUM = re.compile(r"\b([A-Z]{2})\s?(\d{1,4})\b")
_IATA_PAIR = re.compile(r"\b([A-Z]{3})\s*(?:→|->|to)\s*([A-Z]{3})\b")
_DATETIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})")
_CHECK_DATE = re.compile(r"check[\s-]?(?:in|out)[:\s]+(\d{4}-\d{2}-\d{2})", re.I)
_HOTEL_NAME = re.compile(r"(?:Hotel|Property)[:\s]+([^\n]+)", re.I)
_PICKUP_LOC = re.compile(r"pick[\s-]?up[:\s]+([^\n]+)", re.I)
_DROPOFF_LOC = re.compile(r"drop[\s-]?off[:\s]+([^\n]+)", re.I)
_CONFIRMATION = re.compile(r"(?:confirmation|booking)[:\s]+([A-Z0-9]{6,12})", re.I)


class ChaseTravelParser(VendorParser):
    name: ClassVar[str] = "chase_travel"
    sender_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"@chasetravel\.com$", re.I),
        re.compile(r"chase[._-]travel.*@chase\.com$", re.I),
        re.compile(r"noreply@chase\.com$", re.I),  # generic fallback if Chase Travel uses this
    ]

    def parse(self, msg: EmailMessage) -> ParseResult:
        body = extract_text(msg)
        conf_match = _CONFIRMATION.search(body)
        confirmation = conf_match.group(1) if conf_match else None

        segments: list[SegmentDraft] = []

        # Flight section
        flight_block = _FLIGHT_BLOCK.search(body)
        if flight_block:
            seg = _parse_flight(flight_block.group(0), confirmation)
            if seg:
                segments.append(seg)

        # Hotel section
        hotel_block = _HOTEL_BLOCK.search(body)
        if hotel_block:
            seg = _parse_hotel(hotel_block.group(0), confirmation)
            if seg:
                segments.append(seg)

        # Car section
        car_block = _CAR_BLOCK.search(body)
        if car_block:
            seg = _parse_car(car_block.group(0), confirmation)
            if seg:
                segments.append(seg)

        if not segments:
            return ParseResult(segments=[], confidence=0.0, source="rules:chase_travel")
        return ParseResult(segments=segments, confidence=0.9, source="rules:chase_travel")


def _parse_flight(block: str, confirmation: str | None) -> SegmentDraft | None:
    flight = _FLIGHT_NUM.search(block)
    iata = _IATA_PAIR.search(block)
    dt = _DATETIME.search(block)
    if not (flight and iata and dt):
        return None
    y, m, d, hh, mm = (int(g) for g in dt.groups())
    start_at = datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("UTC"))
    return SegmentDraft(
        type="flight",
        confirmation_number=confirmation,
        provider="Chase Travel",
        start_at=start_at,
        start_tz="UTC",
        start_location={"iata": iata.group(1)},
        end_location={"iata": iata.group(2)},
        details={"flight_number": f"{flight.group(1)}{flight.group(2)}"},
    )


def _parse_hotel(block: str, confirmation: str | None) -> SegmentDraft | None:
    name = _HOTEL_NAME.search(block)
    dates = _CHECK_DATE.findall(block)
    if not (name and len(dates) >= 2):
        return None
    ci = datetime.fromisoformat(dates[0]).replace(hour=15, tzinfo=ZoneInfo("UTC"))
    co = datetime.fromisoformat(dates[1]).replace(hour=11, tzinfo=ZoneInfo("UTC"))
    return SegmentDraft(
        type="lodging",
        confirmation_number=confirmation,
        provider="Chase Travel",
        start_at=ci,
        start_tz="UTC",
        end_at=co,
        end_tz="UTC",
        start_location={"name": name.group(1).strip()},
    )


def _parse_car(block: str, confirmation: str | None) -> SegmentDraft | None:
    pickup = _PICKUP_LOC.search(block)
    dropoff = _DROPOFF_LOC.search(block)
    dts = _DATETIME.findall(block)
    if not (pickup and dropoff and len(dts) >= 2):
        return None
    pu_y, pu_m, pu_d, pu_hh, pu_mm = (int(g) for g in dts[0])
    do_y, do_m, do_d, do_hh, do_mm = (int(g) for g in dts[1])
    return SegmentDraft(
        type="car",
        confirmation_number=confirmation,
        provider="Chase Travel",
        start_at=datetime(pu_y, pu_m, pu_d, pu_hh, pu_mm, tzinfo=ZoneInfo("UTC")),
        start_tz="UTC",
        end_at=datetime(do_y, do_m, do_d, do_hh, do_mm, tzinfo=ZoneInfo("UTC")),
        end_tz="UTC",
        start_location={"name": pickup.group(1).strip()},
        end_location={"name": dropoff.group(1).strip()},
    )
