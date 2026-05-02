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

Each handler runs through _enrich() to lift cross-cutting schema.org fields
(reservationStatus, totalPrice, underName, programMembershipUsed,
potentialAction, bookingTime, provider/airline/operator) into the segment.
GeoCoordinates from Place.geo land directly on start_location / end_location
dicts via _apply_geo() so the map feature can read lat/lng without a
geocoder round-trip.

Returns ParseResult with confidence 0.95 on hit.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Literal

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


# ─── enrichment helpers ────────────────────────────────────────────────────


def _apply_geo(location: dict[str, Any] | None, place: dict[str, Any]) -> dict[str, Any] | None:
    """Append lat/lng to a location dict from a Place's `geo` field.

    schema.org GeoCoordinates: `latitude` and `longitude` can be string OR
    number in the wild. Coerce to float; drop both on coercion failure rather
    than emit a half-coordinate.
    """
    if location is None:
        return None
    geo = place.get("geo")
    if not isinstance(geo, dict):
        return location
    lat, lng = geo.get("latitude"), geo.get("longitude")
    if lat is None or lng is None:
        return location
    with contextlib.suppress(TypeError, ValueError):
        location["lat"] = float(lat)
        location["lng"] = float(lng)
    return location


def _extract_price(d: dict[str, Any]) -> dict[str, Any]:
    """Lift totalPrice + priceCurrency from a reservation.

    `totalPrice` can be a number, a numeric string, or a PriceSpecification
    object ({"price": ..., "priceCurrency": ...}). Coerce to float; on
    coercion failure, drop price (still keep currency if it was unambiguous).

    `priceCurrency` is normalized to uppercase ISO 4217. Real-world emails
    are inconsistent (some emit `"usd"`, some `"USD"`); Frankfurter and our
    Expense.currency column both expect uppercase. Fix once at extraction so
    every downstream consumer (expense auto-import, FX, totals, etc.) sees a
    single canonical shape.
    """
    out: dict[str, Any] = {}
    raw = d.get("totalPrice")
    currency = d.get("priceCurrency")
    if isinstance(raw, dict):
        currency = currency or raw.get("priceCurrency")
        raw = raw.get("price")
    if raw is not None:
        with contextlib.suppress(TypeError, ValueError):
            out["total_price"] = float(raw)
    if isinstance(currency, str) and currency:
        out["price_currency"] = currency.upper()
    return out


def _extract_status(d: dict[str, Any]) -> Literal["confirmed", "cancelled"]:
    """reservationStatus URL → SegmentDraft.status.

    Schema.org statuses end the URL with ReservationConfirmed/Cancelled/Hold/
    Pending. We only branch on Cancelled today; everything else is treated as
    confirmed (the segment author can manually mark tentative if desired).
    Return type is the SegmentDraft.status Literal so mypy enforces the
    valid-values invariant at the assignment site.
    """
    status = d.get("reservationStatus", "")
    if isinstance(status, str) and status.endswith("ReservationCancelled"):
        return "cancelled"
    return "confirmed"


def _extract_passengers(d: dict[str, Any]) -> list[str]:
    """underName → list of names. Schema.org allows a single Person or a list."""
    under = d.get("underName")
    if not under:
        return []
    if isinstance(under, dict):
        under = [under]
    if not isinstance(under, list):
        return []
    out: list[str] = []
    for p in under:
        if isinstance(p, dict) and (name := p.get("name")):
            out.append(name)
    return out


def _extract_program_membership(d: dict[str, Any]) -> dict[str, Any]:
    """programMembershipUsed → {program_name, membership_number}.

    schema.org ProgramMembership has more fields but these two are the only
    ones reliably populated by airline/hotel confirmations in the wild.
    """
    pm = d.get("programMembershipUsed") or {}
    if not isinstance(pm, dict):
        return {}
    out: dict[str, Any] = {}
    if name := pm.get("programName"):
        out["program_name"] = name
    if num := pm.get("membershipNumber"):
        out["membership_number"] = num
    return out


def _extract_actions(d: dict[str, Any]) -> dict[str, str]:
    """potentialAction → {modify_url, cancel_url, view_url}.

    schema.org Action has a `target` that is either a URL string OR an
    EntryPoint object with `urlTemplate`/`url`. EditAction and ReserveAction
    both map to modify_url since users perceive them identically.
    """
    actions = d.get("potentialAction") or []
    if isinstance(actions, dict):
        actions = [actions]
    if not isinstance(actions, list):
        return {}
    type_to_key = {
        "ReserveAction": "modify_url",
        "EditAction": "modify_url",
        "CancelAction": "cancel_url",
        "ViewAction": "view_url",
    }
    out: dict[str, str] = {}
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = action.get("@type")
        if not isinstance(action_type, str):
            continue
        key = type_to_key.get(action_type)
        if not key:
            continue
        target = action.get("target")
        url: str | None = None
        if isinstance(target, str):
            url = target
        elif isinstance(target, dict):
            raw = target.get("urlTemplate") or target.get("url")
            if isinstance(raw, str):
                url = raw
        if url:
            out[key] = url
    return out


def _extract_provider(d: dict[str, Any], inner: dict[str, Any]) -> str | None:
    """Pick the best provider name from reservation + inner trip object.

    Priority: reservation `provider` > inner `airline` > inner `provider` >
    inner `busOperator` > inner `boatOperator`. Each can be an Organization
    (dict with `name`) or a bare string.
    """
    for candidate in (
        d.get("provider"),
        inner.get("airline"),
        inner.get("provider"),
        inner.get("busOperator"),
        inner.get("boatOperator"),
    ):
        if isinstance(candidate, dict):
            if name := candidate.get("name"):
                return str(name)
        elif isinstance(candidate, str) and candidate:
            return candidate
    return None


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


def _enrich(d: dict[str, Any], seg: SegmentDraft) -> SegmentDraft:
    """Apply cross-cutting schema.org enrichments to a partially-built segment.

    Mutates `seg.status`, `seg.provider` (only if unset by handler), and
    `seg.details` with: price/currency, passengers, program membership,
    action URLs, booking time. Geo coords are NOT applied here — each handler
    calls _apply_geo per Place, since the locations are handler-specific.
    """
    inner = d.get("reservationFor", {}) or {}
    seg.status = _extract_status(d)
    if not seg.provider:
        seg.provider = _extract_provider(d, inner)
    enriched = dict(seg.details)
    enriched.update(_extract_price(d))
    if pax := _extract_passengers(d):
        enriched["passengers"] = pax
    enriched.update(_extract_program_membership(d))
    if actions := _extract_actions(d):
        enriched["actions"] = actions
    if booking_time := _parse_iso(d.get("bookingTime", "")):
        enriched["booking_time"] = booking_time.isoformat()
    seg.details = enriched
    return seg


# ─── reservation handlers ──────────────────────────────────────────────────


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
    seg = SegmentDraft(
        type="flight",
        confirmation_number=d.get("reservationNumber"),
        start_at=start,
        start_tz=_tz_str(start) or "UTC",
        end_at=end,
        end_tz=_tz_str(end),
        start_location=_apply_geo({"iata": dep.get("iataCode"), "name": dep.get("name")}, dep),
        end_location=_apply_geo({"iata": arr.get("iataCode"), "name": arr.get("name")}, arr),
        details=details,
    )
    return _enrich(d, seg)


def _lodging_from_jsonld(d: dict[str, Any]) -> SegmentDraft | None:
    start = _parse_iso(d.get("checkinTime", ""))
    end = _parse_iso(d.get("checkoutTime", ""))
    if not start:
        return None
    inner = d.get("reservationFor", {}) or {}
    addr = inner.get("address", {}) or {}
    seg = SegmentDraft(
        type="lodging",
        confirmation_number=d.get("reservationNumber"),
        start_at=start,
        start_tz=_tz_str(start) or "UTC",
        end_at=end,
        end_tz=_tz_str(end),
        start_location=_apply_geo(
            {
                "name": inner.get("name"),
                "city": addr.get("addressLocality"),
                "country": addr.get("addressCountry"),
            },
            inner,
        ),
    )
    return _enrich(d, seg)


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
    seg = SegmentDraft(
        type="train",
        confirmation_number=d.get("reservationNumber"),
        start_at=start,
        start_tz=_tz_str(start) or "UTC",
        end_at=end,
        end_tz=_tz_str(end),
        start_location=_apply_geo({"name": dep.get("name")}, dep),
        end_location=_apply_geo({"name": arr.get("name")}, arr),
        details=details,
    )
    return _enrich(d, seg)


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
    seg = SegmentDraft(
        type="train",  # ← coerced (no `bus` SegmentType yet)
        confirmation_number=d.get("reservationNumber"),
        start_at=start,
        start_tz=_tz_str(start) or "UTC",
        end_at=end,
        end_tz=_tz_str(end),
        start_location=_apply_geo({"name": dep.get("name")}, dep),
        end_location=_apply_geo({"name": arr.get("name")}, arr),
        details=details,
    )
    return _enrich(d, seg)


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
    seg = SegmentDraft(
        type="transfer",  # ← coerced (no `ferry` SegmentType yet)
        confirmation_number=d.get("reservationNumber"),
        start_at=start,
        start_tz=_tz_str(start) or "UTC",
        end_at=end,
        end_tz=_tz_str(end),
        start_location=_apply_geo({"name": dep.get("name")}, dep),
        end_location=_apply_geo({"name": arr.get("name")}, arr),
        details=details,
    )
    return _enrich(d, seg)


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
    seg = SegmentDraft(
        type="car",
        confirmation_number=d.get("reservationNumber"),
        start_at=start,
        start_tz=_tz_str(start) or "UTC",
        end_at=end,
        end_tz=_tz_str(end),
        start_location=_apply_geo(
            {"name": pickup.get("name"), "city": pickup_addr.get("addressLocality")},
            pickup,
        ),
        end_location=_apply_geo(
            {"name": dropoff.get("name"), "city": dropoff_addr.get("addressLocality")},
            dropoff,
        ),
        details={"vehicle": vehicle.get("name") or None},
    )
    return _enrich(d, seg)


def _taxi_from_jsonld(d: dict[str, Any]) -> SegmentDraft | None:
    """TaxiReservation: pickup time/location are top-level. reservationFor is
    a Taxi (the service)."""
    start = _parse_iso(d.get("pickupTime", ""))
    if not start:
        return None
    pickup = d.get("pickupLocation", {}) or {}
    seg = SegmentDraft(
        type="transfer",
        confirmation_number=d.get("reservationNumber"),
        start_at=start,
        start_tz=_tz_str(start) or "UTC",
        start_location=_apply_geo({"name": pickup.get("name")}, pickup),
        details={"party_size": d.get("partySize")},
    )
    return _enrich(d, seg)


def _event_from_jsonld(d: dict[str, Any]) -> SegmentDraft | None:
    """EventReservation → activity. reservationFor is an Event with startDate."""
    inner = d.get("reservationFor", {}) or {}
    start = _parse_iso(inner.get("startDate", ""))
    if not start:
        return None
    end = _parse_iso(inner.get("endDate", ""))
    location = inner.get("location", {}) or {}
    addr = location.get("address", {}) or {}
    seg = SegmentDraft(
        type="activity",
        confirmation_number=d.get("reservationNumber"),
        start_at=start,
        start_tz=_tz_str(start) or "UTC",
        end_at=end,
        end_tz=_tz_str(end),
        start_location=_apply_geo(
            {
                "name": location.get("name"),
                "city": addr.get("addressLocality"),
                "country": addr.get("addressCountry"),
            },
            location,
        ),
        details={"event_name": inner.get("name") or None},
    )
    return _enrich(d, seg)


def _food_from_jsonld(d: dict[str, Any]) -> SegmentDraft | None:
    """FoodEstablishmentReservation → activity. startTime is on the reservation;
    reservationFor is a FoodEstablishment/Restaurant."""
    start = _parse_iso(d.get("startTime", ""))
    if not start:
        return None
    end = _parse_iso(d.get("endTime", ""))
    inner = d.get("reservationFor", {}) or {}
    addr = inner.get("address", {}) or {}
    seg = SegmentDraft(
        type="activity",
        confirmation_number=d.get("reservationNumber"),
        start_at=start,
        start_tz=_tz_str(start) or "UTC",
        end_at=end,
        end_tz=_tz_str(end),
        start_location=_apply_geo(
            {
                "name": inner.get("name"),
                "city": addr.get("addressLocality"),
                "country": addr.get("addressCountry"),
            },
            inner,
        ),
        details={
            "restaurant_name": inner.get("name") or None,
            "party_size": d.get("partySize"),
        },
    )
    return _enrich(d, seg)


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
