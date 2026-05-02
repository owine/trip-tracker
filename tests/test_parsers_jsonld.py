"""extruct-based JSON-LD strategy."""

from __future__ import annotations

from email import message_from_bytes
from email.policy import default as email_policy_default
from pathlib import Path

from trip_tracker.parsers.jsonld import parse_jsonld

_FIXTURES = Path(__file__).parent / "fixtures" / "parsers"


def _msg(name: str):
    raw = (_FIXTURES / name).read_bytes()
    return message_from_bytes(raw, policy=email_policy_default)


def test_flight_reservation_extracted() -> None:
    result = parse_jsonld(_msg("jsonld_flight.eml"))
    assert result.confidence >= 0.9
    assert result.source == "json-ld"
    assert len(result.segments) == 1
    seg = result.segments[0]
    assert seg.type == "flight"
    assert seg.confirmation_number == "ABC123"
    assert (seg.start_location or {}).get("iata") == "JFK"
    assert (seg.end_location or {}).get("iata") == "CDG"


def test_lodging_reservation_extracted() -> None:
    result = parse_jsonld(_msg("jsonld_lodging.eml"))
    assert len(result.segments) == 1
    seg = result.segments[0]
    assert seg.type == "lodging"
    assert seg.confirmation_number == "HOT9"
    assert (seg.start_location or {}).get("city") == "Paris"


def test_train_reservation_extracted() -> None:
    """schema.org TrainReservation → train segment with stations + datetime.

    Real-world Trainline emails leave trainName/trainNumber empty in JSON-LD;
    we preserve as None rather than empty strings so vendor pack enrichment
    or the UI can detect missing carrier data and act accordingly.
    """
    result = parse_jsonld(_msg("jsonld_train.eml"))
    assert result.confidence >= 0.9
    assert result.source == "json-ld"
    assert len(result.segments) == 1
    seg = result.segments[0]
    assert seg.type == "train"
    assert seg.confirmation_number == "86FAEY"
    assert (seg.start_location or {}).get("name") == "Bordeaux St-Jean"
    assert (seg.end_location or {}).get("name") == "Paris Montparnasse"
    # Empty strings in JSON-LD must collapse to None for downstream cleanliness.
    assert (seg.details or {}).get("train_name") is None
    assert (seg.details or {}).get("train_number") is None


def test_event_reservation_extracted() -> None:
    """EventReservation → activity segment carrying event name + venue."""
    result = parse_jsonld(_msg("jsonld_event.eml"))
    assert len(result.segments) == 1
    seg = result.segments[0]
    assert seg.type == "activity"
    assert seg.confirmation_number == "EVT-9921"
    assert (seg.start_location or {}).get("name") == "Symphony Hall"
    assert (seg.start_location or {}).get("city") == "Boston"
    assert (seg.details or {}).get("event_name") == "Symphony in C Major"


def test_food_establishment_reservation_extracted() -> None:
    """FoodEstablishmentReservation → activity with restaurant + party size."""
    result = parse_jsonld(_msg("jsonld_food.eml"))
    assert len(result.segments) == 1
    seg = result.segments[0]
    assert seg.type == "activity"
    assert seg.confirmation_number == "OPT-77713"
    assert (seg.start_location or {}).get("name") == "Le Bistro"
    assert (seg.details or {}).get("party_size") == 4


def test_rentalcar_reservation_extracted() -> None:
    """RentalCarReservation: pickup/dropoff are top-level on the reservation;
    one-way rentals have distinct pickup vs dropoff locations."""
    result = parse_jsonld(_msg("jsonld_rentalcar.eml"))
    assert len(result.segments) == 1
    seg = result.segments[0]
    assert seg.type == "car"
    assert seg.confirmation_number == "HRT-44210"
    assert (seg.start_location or {}).get("city") == "Los Angeles"
    assert (seg.end_location or {}).get("city") == "San Francisco"
    assert (seg.details or {}).get("vehicle") == "Toyota Camry or similar"


def test_taxi_reservation_extracted() -> None:
    """TaxiReservation → transfer with pickup-only location (no dropoff in
    schema.org Taxi shape)."""
    result = parse_jsonld(_msg("jsonld_taxi.eml"))
    assert len(result.segments) == 1
    seg = result.segments[0]
    assert seg.type == "transfer"
    assert seg.confirmation_number == "TAX-558811"
    assert (seg.start_location or {}).get("name") == "Hotel Lobby"
    assert seg.end_location is None
    assert (seg.details or {}).get("party_size") == 2


def test_bus_reservation_extracted() -> None:
    """BusReservation coerces to type='train' (closest existing SegmentType)
    and preserves schema_type='bus' in details. Ticket enrichment lifts seat
    number into details.seat_number."""
    result = parse_jsonld(_msg("jsonld_bus.eml"))
    assert len(result.segments) == 1
    seg = result.segments[0]
    assert seg.type == "train"
    assert seg.confirmation_number == "FLX-22918"
    assert (seg.start_location or {}).get("name") == "Berlin ZOB"
    assert (seg.end_location or {}).get("name") == "Hamburg ZOB"
    assert (seg.details or {}).get("schema_type") == "bus"
    assert (seg.details or {}).get("ticket_number") == "FLX-T-981"
    assert (seg.details or {}).get("seat_number") == "12A"


def test_boat_reservation_extracted() -> None:
    """BoatReservation coerces to type='transfer' and preserves
    schema_type='boat' in details so downstream can distinguish ferries."""
    result = parse_jsonld(_msg("jsonld_boat.eml"))
    assert len(result.segments) == 1
    seg = result.segments[0]
    assert seg.type == "transfer"
    assert seg.confirmation_number == "STL-66501"
    assert (seg.start_location or {}).get("name") == "Holyhead"
    assert (seg.end_location or {}).get("name") == "Dublin Port"
    assert (seg.details or {}).get("schema_type") == "boat"


def test_reservation_package_recurses_into_subreservation() -> None:
    """ReservationPackage holds an array of subReservation objects; each is
    parsed independently and emitted as its own segment. Order preserved."""
    result = parse_jsonld(_msg("jsonld_package.eml"))
    assert len(result.segments) == 2
    assert result.segments[0].type == "flight"
    assert result.segments[0].confirmation_number == "FL-A1"
    assert result.segments[1].type == "lodging"
    assert result.segments[1].confirmation_number == "LDG-B2"


def test_no_jsonld_returns_empty() -> None:
    """A plain-text email with no JSON-LD returns segments=[] confidence=0."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = "Plain"
    msg["From"] = "x@y.com"
    msg.set_content("No structured data here.")
    result = parse_jsonld(msg)
    assert result.segments == []
    assert result.confidence == 0.0
