"""JSON-LD extraction strategy using extruct.

Handles the schema.org reservation hierarchy:
  - FlightReservation       → flight
  - LodgingReservation      → lodging
  - TrainReservation        → train
  - BusReservation          → train (+details.schema_type='bus')
  - BoatReservation         → transfer (+details.schema_type='boat')
  - RentalCarReservation    → car
  - TaxiReservation         → transfer
  - EventReservation        → activity
  - FoodEstablishmentRes.   → activity
  - ReservationPackage      → recurses into subReservation
  - Ticket (nested)         → enriches details (ticketNumber, seatNumber)

bus/boat are coerced to existing SegmentTypes (no UI surface for new types
yet), but the original schema.org @type is preserved in details.schema_type
so downstream code can branch if needed.

Returns ParseResult with confidence 0.95 on hit.
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


def _tz_str(dt: datetime | None) -> str | None:
    """Return a tz string for a datetime, or None if naive/missing."""
    if dt is None or dt.tzinfo is None:
        return None
    return str(dt.tzinfo)


def _ticket_details(d: dict[str, Any]) -> dict[str, Any]:
    """Extract enrichment from a Ticket node attached to a reservation.

    schema.org Ticket carries `ticketNumber` and `ticketedSeat` (a Seat with
    seatNumber/seatRow/seatSection). Returns a flat dict that callers merge
    into their segment's details. Returns empty dict when no Ticket present
    or when keys are empty strings (real-world emails often emit empty values).
    """
    ticket = d.get("reservedTicket") or {}
    if not isinstance(ticket, dict):
        return {}
    out: dict[str, Any] = {}
    if num := ticket.get("ticketNumber"):
        out["ticket_number"] = num
    seat = ticket.get("ticketedSeat") or {}
    if isinstance(seat, dict):
        if sn := seat.get("seatNumber"):
            out["seat_number"] = sn
        if sr := seat.get("seatRow"):
            out["seat_row"] = sr
    return out


def _flight_from_jsonld(d: dict[str, Any]) -> SegmentDraft | None:
    inner = d.get("reservationFor", {}) or {}
    start = _parse_iso(inner.get("departureTime", ""))
    if not start:
        return None
    end = _parse_iso(inner.get("arrivalTime", ""))
    dep = inner.get("departureAirport", {}) or {}
    arr = inner.get("arrivalAirport", {}) or {}
    details: dict[str, Any] = {"flight_number": inner.get("flightNumber")}
    details.update(_ticket_details(d))
    return SegmentDraft(
        type="flight",
        confirmation_number=d.get("reservationNumber"),
        start_at=start,
        start_tz=_tz_str(start) or "UTC",
        end_at=end,
        end_tz=_tz_str(end),
        start_location={"iata": dep.get("iataCode"), "name": dep.get("name")},
        end_location={"iata": arr.get("iataCode"), "name": arr.get("name")},
        details=details,
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
        start_tz=_tz_str(start) or "UTC",
        end_at=end,
        end_tz=_tz_str(end),
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
    details: dict[str, Any] = {
        "train_name": inner.get("trainName") or None,
        "train_number": inner.get("trainNumber") or None,
    }
    details.update(_ticket_details(d))
    return SegmentDraft(
        type="train",
        confirmation_number=d.get("reservationNumber"),
        start_at=start,
        start_tz=_tz_str(start) or "UTC",
        end_at=end,
        end_tz=_tz_str(end),
        start_location={"name": dep.get("name")},
        end_location={"name": arr.get("name")},
        details=details,
    )


def _bus_from_jsonld(d: dict[str, Any]) -> SegmentDraft | None:
    """BusReservation → train segment (closest existing SegmentType).

    schema.org BusTrip uses `departureBusStop`/`arrivalBusStop` (vs Train's
    `departureStation`). Otherwise the shape mirrors TrainReservation.
    Original schema_type is preserved so downstream UI can distinguish.
    """
    inner = d.get("reservationFor", {}) or {}
    start = _parse_iso(inner.get("departureTime", ""))
    if not start:
        return None
    end = _parse_iso(inner.get("arrivalTime", ""))
    dep = inner.get("departureBusStop", {}) or {}
    arr = inner.get("arrivalBusStop", {}) or {}
    details: dict[str, Any] = {
        "schema_type": "bus",
        "bus_name": inner.get("busName") or None,
        "bus_number": inner.get("busNumber") or None,
    }
    details.update(_ticket_details(d))
    return SegmentDraft(
        type="train",  # ← coerced (no `bus` SegmentType yet)
        confirmation_number=d.get("reservationNumber"),
        start_at=start,
        start_tz=_tz_str(start) or "UTC",
        end_at=end,
        end_tz=_tz_str(end),
        start_location={"name": dep.get("name")},
        end_location={"name": arr.get("name")},
        details=details,
    )


def _boat_from_jsonld(d: dict[str, Any]) -> SegmentDraft | None:
    """BoatReservation → transfer segment (closest existing SegmentType).

    schema.org BoatTrip uses `departureBoatTerminal`/`arrivalBoatTerminal`.
    Coerced to `transfer` because cruises/ferries are point-to-point
    surface transit; original schema_type preserved in details.
    """
    inner = d.get("reservationFor", {}) or {}
    start = _parse_iso(inner.get("departureTime", ""))
    if not start:
        return None
    end = _parse_iso(inner.get("arrivalTime", ""))
    dep = inner.get("departureBoatTerminal", {}) or {}
    arr = inner.get("arrivalBoatTerminal", {}) or {}
    details: dict[str, Any] = {"schema_type": "boat"}
    details.update(_ticket_details(d))
    return SegmentDraft(
        type="transfer",  # ← coerced (no `ferry` SegmentType yet)
        confirmation_number=d.get("reservationNumber"),
        start_at=start,
        start_tz=_tz_str(start) or "UTC",
        end_at=end,
        end_tz=_tz_str(end),
        start_location={"name": dep.get("name")},
        end_location={"name": arr.get("name")},
        details=details,
    )


def _rentalcar_from_jsonld(d: dict[str, Any]) -> SegmentDraft | None:
    """RentalCarReservation: pickup/dropoff time+location are top-level on
    the reservation (not under reservationFor). reservationFor is the Vehicle
    (RentalCar/Car) describing what's being rented."""
    start = _parse_iso(d.get("pickupTime", ""))
    if not start:
        return None
    end = _parse_iso(d.get("dropoffTime", ""))
    pickup = d.get("pickupLocation", {}) or {}
    dropoff = d.get("dropoffLocation", {}) or {}
    vehicle = d.get("reservationFor", {}) or {}
    pickup_addr = pickup.get("address", {}) or {}
    dropoff_addr = dropoff.get("address", {}) or {}
    return SegmentDraft(
        type="car",
        confirmation_number=d.get("reservationNumber"),
        start_at=start,
        start_tz=_tz_str(start) or "UTC",
        end_at=end,
        end_tz=_tz_str(end),
        start_location={
            "name": pickup.get("name"),
            "city": pickup_addr.get("addressLocality"),
        },
        end_location={
            "name": dropoff.get("name"),
            "city": dropoff_addr.get("addressLocality"),
        },
        details={"vehicle": vehicle.get("name") or None},
    )


def _taxi_from_jsonld(d: dict[str, Any]) -> SegmentDraft | None:
    """TaxiReservation: pickup time/location are top-level. reservationFor is
    a Taxi (the service)."""
    start = _parse_iso(d.get("pickupTime", ""))
    if not start:
        return None
    pickup = d.get("pickupLocation", {}) or {}
    return SegmentDraft(
        type="transfer",
        confirmation_number=d.get("reservationNumber"),
        start_at=start,
        start_tz=_tz_str(start) or "UTC",
        start_location={"name": pickup.get("name")},
        details={"party_size": d.get("partySize")},
    )


def _event_from_jsonld(d: dict[str, Any]) -> SegmentDraft | None:
    """EventReservation → activity. reservationFor is an Event with startDate."""
    inner = d.get("reservationFor", {}) or {}
    start = _parse_iso(inner.get("startDate", ""))
    if not start:
        return None
    end = _parse_iso(inner.get("endDate", ""))
    location = inner.get("location", {}) or {}
    addr = location.get("address", {}) or {}
    return SegmentDraft(
        type="activity",
        confirmation_number=d.get("reservationNumber"),
        start_at=start,
        start_tz=_tz_str(start) or "UTC",
        end_at=end,
        end_tz=_tz_str(end),
        start_location={
            "name": location.get("name"),
            "city": addr.get("addressLocality"),
            "country": addr.get("addressCountry"),
        },
        details={"event_name": inner.get("name") or None},
    )


def _food_from_jsonld(d: dict[str, Any]) -> SegmentDraft | None:
    """FoodEstablishmentReservation → activity. startTime is on the reservation;
    reservationFor is a FoodEstablishment/Restaurant."""
    start = _parse_iso(d.get("startTime", ""))
    if not start:
        return None
    end = _parse_iso(d.get("endTime", ""))
    inner = d.get("reservationFor", {}) or {}
    addr = inner.get("address", {}) or {}
    return SegmentDraft(
        type="activity",
        confirmation_number=d.get("reservationNumber"),
        start_at=start,
        start_tz=_tz_str(start) or "UTC",
        end_at=end,
        end_tz=_tz_str(end),
        start_location={
            "name": inner.get("name"),
            "city": addr.get("addressLocality"),
            "country": addr.get("addressCountry"),
        },
        details={
            "restaurant_name": inner.get("name") or None,
            "party_size": d.get("partySize"),
        },
    )


# Outer @type → handler function. ReservationPackage is handled separately
# because it recurses rather than producing a single segment.
_DISPATCH = {
    "FlightReservation": _flight_from_jsonld,
    "LodgingReservation": _lodging_from_jsonld,
    "TrainReservation": _train_from_jsonld,
    "BusReservation": _bus_from_jsonld,
    "BoatReservation": _boat_from_jsonld,
    "RentalCarReservation": _rentalcar_from_jsonld,
    "TaxiReservation": _taxi_from_jsonld,
    "EventReservation": _event_from_jsonld,
    "FoodEstablishmentReservation": _food_from_jsonld,
}


def _segments_from_item(item: dict[str, Any]) -> list[SegmentDraft]:
    """Recursive: handles ReservationPackage (collection) and leaf reservations.

    A ReservationPackage holds an array of subReservation objects, each
    potentially being any reservation type — including, recursively, another
    ReservationPackage. Leaf reservations dispatch through _DISPATCH.
    """
    t = item.get("@type")
    if t == "ReservationPackage":
        sub = item.get("subReservation") or []
        if not isinstance(sub, list):
            sub = [sub]
        out: list[SegmentDraft] = []
        for s in sub:
            if isinstance(s, dict):
                out.extend(_segments_from_item(s))
        return out
    handler = _DISPATCH.get(t) if isinstance(t, str) else None
    if handler is None:
        return []
    seg = handler(item)
    return [seg] if seg else []


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
        if isinstance(item, dict):
            segments.extend(_segments_from_item(item))

    return ParseResult(
        segments=segments,
        confidence=0.95 if segments else 0.0,
        source="json-ld",
    )
