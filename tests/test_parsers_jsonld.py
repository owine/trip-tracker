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
