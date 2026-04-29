"""Airport IATA → tz + lat/lon enrichment."""

from __future__ import annotations

from trip_tracker.parsers.enrich import enrich_airport, get_airport, haversine_km


def test_get_airport_jfk() -> None:
    a = get_airport("JFK")
    assert a is not None
    assert a.tz == "America/New_York"
    assert a.city.lower() == "new york"
    assert -74.5 < a.lon < -73.5
    assert 40.0 < a.lat < 41.0


def test_get_airport_unknown() -> None:
    assert get_airport("XXX") is None


def test_get_airport_case_insensitive() -> None:
    assert get_airport("jfk") is not None


def test_haversine_known_pair() -> None:
    """JFK → CDG is ~5837 km."""
    jfk = get_airport("JFK")
    cdg = get_airport("CDG")
    assert jfk is not None
    assert cdg is not None
    d = haversine_km((jfk.lat, jfk.lon), (cdg.lat, cdg.lon))
    assert 5800 < d < 5900


def test_haversine_zero() -> None:
    assert haversine_km((0.0, 0.0), (0.0, 0.0)) == 0.0


def test_enrich_airport_fills_tz_and_coords() -> None:
    loc = {"iata": "CDG", "city": "Paris"}
    enriched = enrich_airport(loc)
    assert enriched["tz"] == "Europe/Paris"
    assert "lat" in enriched
    assert "lon" in enriched


def test_enrich_airport_unknown_returns_input() -> None:
    loc = {"iata": "XXX"}
    enriched = enrich_airport(loc)
    assert enriched == loc  # no enrichment, no error
