"""resolve_point: iata → city → None priority chain."""

from __future__ import annotations

from trip_tracker.geo.resolve import resolve_point


def test_resolve_with_iata_returns_airport_coords() -> None:
    point = resolve_point({"iata": "JFK", "city": "New York"})
    assert point is not None
    lat, lon = point
    # JFK is at ~40.64°N, 73.78°W
    assert 40.5 < lat < 40.7
    assert -74 < lon < -73


def test_resolve_with_unknown_iata_falls_back_to_city() -> None:
    point = resolve_point({"iata": "XYZ", "city": "Berlin", "country": "DE"})
    assert point is not None
    lat, lon = point
    # Berlin ~52.52°N, 13.40°E
    assert 52 < lat < 53
    assert 13 < lon < 14


def test_resolve_with_only_city() -> None:
    point = resolve_point({"city": "Tokyo", "country": "JP"})
    assert point is not None
    lat, lon = point
    # Tokyo ~35.7°N, 139.7°E
    assert 35 < lat < 36
    assert 139 < lon < 140


def test_resolve_with_no_location_data_returns_none() -> None:
    assert resolve_point(None) is None
    assert resolve_point({}) is None


def test_resolve_unknown_city_returns_none() -> None:
    assert resolve_point({"city": "Xyzzyglop"}) is None
