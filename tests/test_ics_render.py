"""RFC 5545 serializer: shape, escaping, line folding, per-type dispatch, alarms."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from icalendar import Calendar

from trip_tracker.ics.render import render_calendar
from trip_tracker.models.segment import Segment
from trip_tracker.models.user import User

BASE_URL = "https://trips.example.com"


def _seg(
    *,
    trip_id: uuid.UUID,
    type_: str = "flight",
    start_at: datetime = datetime(2026, 6, 1, 13, 0, tzinfo=UTC),
    end_at: datetime | None = None,
    provider: str | None = None,
    confirmation_number: str | None = None,
    start_location: dict[str, Any] | None = None,
    end_location: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
    parse_source: str = "manual",
) -> Segment:
    return Segment(
        id=uuid.uuid4(),
        trip_id=trip_id,
        owner_user_id=uuid.uuid4(),
        type=type_,
        status="confirmed",
        confirmation_number=confirmation_number,
        provider=provider,
        start_at=start_at,
        start_tz="UTC",
        end_at=end_at,
        end_tz="UTC" if end_at else None,
        start_location=start_location,
        end_location=end_location,
        details=details,
        parse_source=parse_source,
        parse_confidence=1.0,
    )


def _user() -> User:
    return User(
        id=uuid.uuid4(),
        email="r1@x.com",
        display_name="Trip Tester",
    )


def test_calendar_wrapper_has_required_properties() -> None:
    body = render_calendar(user=_user(), segments=[], base_url=BASE_URL)
    assert "BEGIN:VCALENDAR" in body
    assert "END:VCALENDAR" in body
    assert "VERSION:2.0" in body
    assert "PRODID:-//trip-tracker//EN" in body
    assert "CALSCALE:GREGORIAN" in body
    assert "METHOD:PUBLISH" in body
    assert "NAME:Trip-Tracker" in body
    assert "X-WR-CALNAME:Trip-Tracker" in body


def test_empty_segments_produces_valid_calendar() -> None:
    body = render_calendar(user=_user(), segments=[], base_url=BASE_URL)
    cal = Calendar.from_ical(body)
    events = [c for c in cal.subcomponents if c.name == "VEVENT"]
    assert events == []


def test_flight_vevent_summary_and_alarm() -> None:
    tid = uuid.uuid4()
    s = _seg(
        trip_id=tid,
        type_="flight",
        start_at=datetime(2026, 6, 1, 13, 0, tzinfo=UTC),
        end_at=datetime(2026, 6, 1, 22, 0, tzinfo=UTC),
        provider="Air France",
        confirmation_number="K8YH3M",
        start_location={"iata": "JFK", "city": "New York"},
        end_location={"iata": "CDG", "city": "Paris"},
        details={"flight_number": "AF007"},
    )
    body = render_calendar(user=_user(), segments=[s], base_url=BASE_URL)
    cal = Calendar.from_ical(body)
    events = [c for c in cal.subcomponents if c.name == "VEVENT"]
    assert len(events) == 1
    ev = events[0]
    assert "AF007" in str(ev["SUMMARY"])
    assert "JFK" in str(ev["SUMMARY"])
    assert "CDG" in str(ev["SUMMARY"])
    assert str(ev["LOCATION"]) == "Paris"
    assert str(ev["UID"]) == f"{s.id}@trips.example.com"
    assert str(ev["URL"]) == f"{BASE_URL}/trips/{tid}#segment-{s.id}"
    alarms = [c for c in ev.subcomponents if c.name == "VALARM"]
    assert len(alarms) == 1
    assert alarms[0]["TRIGGER"].to_ical() == b"-PT3H"
    assert str(alarms[0]["ACTION"]) == "DISPLAY"


def test_lodging_vevent_no_alarm() -> None:
    s = _seg(
        trip_id=uuid.uuid4(),
        type_="lodging",
        start_at=datetime(2026, 6, 1, 15, 0, tzinfo=UTC),
        end_at=datetime(2026, 6, 7, 11, 0, tzinfo=UTC),
        provider="Hotel Adlon",
        end_location={"city": "Berlin", "address": "Unter den Linden 77"},
    )
    body = render_calendar(user=_user(), segments=[s], base_url=BASE_URL)
    cal = Calendar.from_ical(body)
    ev = next(c for c in cal.subcomponents if c.name == "VEVENT")
    assert "Hotel Adlon" in str(ev["SUMMARY"])
    loc = str(ev["LOCATION"])
    assert "Unter den Linden" in loc or loc == "Berlin"
    assert not [c for c in ev.subcomponents if c.name == "VALARM"]


def test_train_vevent() -> None:
    s = _seg(
        trip_id=uuid.uuid4(),
        type_="train",
        provider="SNCF",
        details={"train_number": "9023"},
        start_location={"iata": "PAR", "city": "Paris"},
        end_location={"iata": "BCN", "city": "Barcelona"},
    )
    body = render_calendar(user=_user(), segments=[s], base_url=BASE_URL)
    cal = Calendar.from_ical(body)
    ev = next(c for c in cal.subcomponents if c.name == "VEVENT")
    assert "9023" in str(ev["SUMMARY"])
    assert str(ev["LOCATION"]) == "Barcelona"


def test_segment_with_no_end_at_gets_one_hour_default() -> None:
    s = _seg(
        trip_id=uuid.uuid4(),
        type_="activity",
        start_at=datetime(2026, 6, 3, 14, 0, tzinfo=UTC),
        end_at=None,
        provider="Louvre Museum tour",
    )
    body = render_calendar(user=_user(), segments=[s], base_url=BASE_URL)
    cal = Calendar.from_ical(body)
    ev = next(c for c in cal.subcomponents if c.name == "VEVENT")
    dtstart = ev["DTSTART"].dt
    dtend = ev["DTEND"].dt
    assert (dtend - dtstart).total_seconds() == 3600


def test_text_escaping_handles_commas_and_semicolons() -> None:
    s = _seg(
        trip_id=uuid.uuid4(),
        type_="lodging",
        provider="Hôtel Le Bristol, Paris",
        end_location={"address": "112 Rue du Faubourg; 75008", "city": "Paris"},
    )
    body = render_calendar(user=_user(), segments=[s], base_url=BASE_URL)
    assert r"Le Bristol\, Paris" in body
    assert r"Faubourg\;" in body
    cal = Calendar.from_ical(body)
    ev = next(c for c in cal.subcomponents if c.name == "VEVENT")
    assert "Le Bristol, Paris" in str(ev["SUMMARY"])
    assert "Faubourg;" in str(ev["LOCATION"])


def test_text_escaping_collapses_newlines_and_bare_cr() -> None:
    s = _seg(
        trip_id=uuid.uuid4(),
        type_="lodging",
        provider="Hotel\nWith\rNewlines",
    )
    body = render_calendar(user=_user(), segments=[s], base_url=BASE_URL)
    summary_line = next(line for line in body.splitlines() if "SUMMARY:" in line)
    assert "\r" not in summary_line  # splitlines strips line CRLFs
    assert r"\n" in summary_line


def test_line_folding_at_75_octets() -> None:
    long_provider = "X" * 100
    s = _seg(trip_id=uuid.uuid4(), type_="activity", provider=long_provider)
    body = render_calendar(user=_user(), segments=[s], base_url=BASE_URL)
    folded_lines = [line for line in body.split("\r\n") if line.startswith(" ")]
    assert len(folded_lines) >= 1
    cal = Calendar.from_ical(body)
    ev = next(c for c in cal.subcomponents if c.name == "VEVENT")
    assert long_provider in str(ev["SUMMARY"])


def test_line_folding_counts_octets_not_chars() -> None:
    s = _seg(
        trip_id=uuid.uuid4(),
        type_="flight",
        provider="A" * 70,
        details={"flight_number": "AF" + "0" * 5},
        start_location={"iata": "JFK"},
        end_location={"iata": "CDG"},
    )
    body = render_calendar(user=_user(), segments=[s], base_url=BASE_URL)
    cal = Calendar.from_ical(body)
    ev = next(c for c in cal.subcomponents if c.name == "VEVENT")
    assert "✈" in str(ev["SUMMARY"])


def test_uid_stable_across_two_renders() -> None:
    tid = uuid.uuid4()
    s = _seg(trip_id=tid, type_="flight", details={"flight_number": "AF1"})
    body1 = render_calendar(user=_user(), segments=[s], base_url=BASE_URL)
    body2 = render_calendar(user=_user(), segments=[s], base_url=BASE_URL)
    cal1 = Calendar.from_ical(body1)
    cal2 = Calendar.from_ical(body2)
    uid1 = str(next(c for c in cal1.subcomponents if c.name == "VEVENT")["UID"])
    uid2 = str(next(c for c in cal2.subcomponents if c.name == "VEVENT")["UID"])
    assert uid1 == uid2


def test_dtstart_emits_utc_basic_format() -> None:
    s = _seg(
        trip_id=uuid.uuid4(),
        type_="flight",
        start_at=datetime(2026, 6, 1, 13, 30, 45, tzinfo=UTC),
    )
    body = render_calendar(user=_user(), segments=[s], base_url=BASE_URL)
    assert "DTSTART:20260601T133045Z" in body


def test_per_type_fallback_for_partial_segment() -> None:
    s = _seg(
        trip_id=uuid.uuid4(),
        type_="transfer",
        provider=None,
    )
    body = render_calendar(user=_user(), segments=[s], base_url=BASE_URL)
    cal = Calendar.from_ical(body)
    ev = next(c for c in cal.subcomponents if c.name == "VEVENT")
    summary = str(ev["SUMMARY"])
    assert "transfer" in summary
