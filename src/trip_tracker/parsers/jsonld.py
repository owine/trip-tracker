"""JSON-LD extraction strategy using extruct.

Looks for FlightReservation, LodgingReservation, TrainReservation. Returns
ParseResult with confidence ~0.95 on hit.
"""

from __future__ import annotations

import logging
from datetime import datetime
from email.message import EmailMessage
from typing import Any

import extruct

from trip_tracker.parsers.base import ParseResult, SegmentDraft

logger = logging.getLogger(__name__)


def _extract_html(msg: EmailMessage) -> str:
    """Pull the text/html part if present, else fall back to text/plain."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    if msg.get_content_type() == "text/html":
        payload = msg.get_payload(decode=True)
        if isinstance(payload, bytes):
            return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return ""


def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _flight_from_jsonld(d: dict[str, Any]) -> SegmentDraft | None:
    inner = d.get("reservationFor", {})
    start = _parse_iso(inner.get("departureTime", ""))
    if not start:
        return None
    end = _parse_iso(inner.get("arrivalTime", ""))
    dep = inner.get("departureAirport", {}) or {}
    arr = inner.get("arrivalAirport", {}) or {}
    return SegmentDraft(
        type="flight",
        confirmation_number=d.get("reservationNumber"),
        start_at=start,
        start_tz=str(start.tzinfo) if start.tzinfo else "UTC",
        end_at=end,
        end_tz=str(end.tzinfo) if end and end.tzinfo else None,
        start_location={"iata": dep.get("iataCode"), "name": dep.get("name")},
        end_location={"iata": arr.get("iataCode"), "name": arr.get("name")},
        details={"flight_number": inner.get("flightNumber")},
    )


def _lodging_from_jsonld(d: dict[str, Any]) -> SegmentDraft | None:
    start = _parse_iso(d.get("checkinTime", ""))
    end = _parse_iso(d.get("checkoutTime", ""))
    if not start:
        return None
    inner = d.get("reservationFor", {}) or {}
    addr = inner.get("address", {}) or {}
    return SegmentDraft(
        type="lodging",
        confirmation_number=d.get("reservationNumber"),
        start_at=start,
        start_tz=str(start.tzinfo) if start.tzinfo else "UTC",
        end_at=end,
        end_tz=str(end.tzinfo) if end and end.tzinfo else None,
        start_location={
            "name": inner.get("name"),
            "city": addr.get("addressLocality"),
            "country": addr.get("addressCountry"),
        },
    )


def _train_from_jsonld(d: dict[str, Any]) -> SegmentDraft | None:
    inner = d.get("reservationFor", {}) or {}
    start = _parse_iso(inner.get("departureTime", ""))
    if not start:
        return None
    end = _parse_iso(inner.get("arrivalTime", ""))
    dep = inner.get("departureStation", {}) or {}
    arr = inner.get("arrivalStation", {}) or {}
    # trainName/trainNumber are often empty in real-world JSON-LD (Trainline,
    # SNCF) — preserve as None rather than empty string for schema cleanliness.
    train_name = inner.get("trainName") or None
    train_number = inner.get("trainNumber") or None
    return SegmentDraft(
        type="train",
        confirmation_number=d.get("reservationNumber"),
        start_at=start,
        start_tz=str(start.tzinfo) if start.tzinfo else "UTC",
        end_at=end,
        end_tz=str(end.tzinfo) if end and end.tzinfo else None,
        start_location={"name": dep.get("name")},
        end_location={"name": arr.get("name")},
        details={"train_name": train_name, "train_number": train_number},
    )


def parse_jsonld(msg: EmailMessage) -> ParseResult:
    """Run extruct over the email's HTML body, extract reservations."""
    html = _extract_html(msg)
    if not html:
        return ParseResult(segments=[], confidence=0.0, source="json-ld")
    try:
        data = extruct.extract(html, syntaxes=["json-ld"])
    except Exception as exc:
        logger.warning("extruct failed: %s", exc)
        return ParseResult(segments=[], confidence=0.0, source="json-ld", warnings=[str(exc)])

    segments: list[SegmentDraft] = []
    for item in data.get("json-ld") or []:
        t = item.get("@type")
        if t == "FlightReservation":
            seg = _flight_from_jsonld(item)
        elif t == "LodgingReservation":
            seg = _lodging_from_jsonld(item)
        elif t == "TrainReservation":
            seg = _train_from_jsonld(item)
        else:
            continue
        if seg:
            segments.append(seg)

    return ParseResult(
        segments=segments,
        confidence=0.95 if segments else 0.0,
        source="json-ld",
    )
