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


# ─── enrichment tests ──────────────────────────────────────────────────────


def test_pricing_extracted_from_food_reservation() -> None:
    """totalPrice + priceCurrency lift into details.{total_price, price_currency}.

    Coerces string price '240.00' → float 240.0 because schema.org allows
    either; downstream expense-tracking expects numeric."""
    result = parse_jsonld(_msg("jsonld_food.eml"))
    seg = result.segments[0]
    assert (seg.details or {}).get("total_price") == 240.0
    assert (seg.details or {}).get("price_currency") == "USD"


def test_passengers_extracted_from_food_reservation() -> None:
    """underName as a list of Person → details.passengers as flat name list."""
    result = parse_jsonld(_msg("jsonld_food.eml"))
    seg = result.segments[0]
    assert (seg.details or {}).get("passengers") == ["Oliver Wine", "Elise Wine"]


def test_geo_coordinates_extracted_from_train_stations() -> None:
    """Place.geo (GeoCoordinates) → lat/lng on location dicts. Mixed
    string/number latitude/longitude both coerce to float so the map feature
    sees one consistent shape."""
    result = parse_jsonld(_msg("jsonld_train.eml"))
    seg = result.segments[0]
    assert (seg.start_location or {}).get("lat") == 44.8264
    assert (seg.start_location or {}).get("lng") == -0.5563
    # arrival uses STRING coords in the fixture — must coerce to float too
    assert (seg.end_location or {}).get("lat") == 48.8403
    assert (seg.end_location or {}).get("lng") == 2.3209


def test_provider_extracted_from_train_organization() -> None:
    """reservationFor.provider as Organization → SegmentDraft.provider."""
    result = parse_jsonld(_msg("jsonld_train.eml"))
    seg = result.segments[0]
    assert seg.provider == "SNCF Voyageurs"


def test_booking_time_extracted_from_train() -> None:
    """bookingTime is preserved as ISO string in details so downstream can
    compute 'booked X days in advance' without re-parsing."""
    result = parse_jsonld(_msg("jsonld_train.eml"))
    seg = result.segments[0]
    assert (seg.details or {}).get("booking_time", "").startswith("2026-04-15T10:23:00")


def test_program_membership_extracted_from_flight() -> None:
    """programMembershipUsed → details.{program_name, membership_number}."""
    result = parse_jsonld(_msg("jsonld_flight.eml"))
    seg = result.segments[0]
    assert (seg.details or {}).get("program_name") == "AirExample Elite"
    assert (seg.details or {}).get("membership_number") == "AE-998877"


def test_potential_actions_extracted_from_flight() -> None:
    """potentialAction with both string-target and EntryPoint-target lands
    flat in details.actions keyed by view_url / cancel_url / modify_url."""
    result = parse_jsonld(_msg("jsonld_flight.eml"))
    seg = result.segments[0]
    actions = (seg.details or {}).get("actions") or {}
    assert actions.get("view_url") == "https://airexample.com/booking/ABC123"
    assert actions.get("cancel_url") == "https://airexample.com/cancel/ABC123"


def test_provider_from_airline_organization() -> None:
    """For flights, inner.airline.name fills SegmentDraft.provider when no
    top-level reservation provider is present."""
    result = parse_jsonld(_msg("jsonld_flight.eml"))
    seg = result.segments[0]
    assert seg.provider == "AirExample"


def test_cancelled_reservation_status() -> None:
    """ReservationCancelled URL → SegmentDraft.status='cancelled'.

    The segment is still emitted (not silently dropped) so /inbox shows the
    cancellation and the user can decide whether to delete or keep for
    record-keeping."""
    result = parse_jsonld(_msg("jsonld_cancelled.eml"))
    assert len(result.segments) == 1
    seg = result.segments[0]
    assert seg.status == "cancelled"
    assert seg.confirmation_number == "CANCELLED-1"


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
