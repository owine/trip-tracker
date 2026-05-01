"""Great-circle arc interpolation: spherical linear interpolation (slerp)."""

from __future__ import annotations

import pytest

from trip_tracker.geo.arcs import great_circle_points


def test_endpoints_match_input() -> None:
    points = great_circle_points((40.64, -73.78), (49.01, 2.55), n_points=10)
    assert points[0] == pytest.approx((40.64, -73.78), abs=1e-3)
    assert points[-1] == pytest.approx((49.01, 2.55), abs=1e-3)


def test_n_points_count() -> None:
    points = great_circle_points((0, 0), (0, 90), n_points=50)
    assert len(points) == 50


def test_jfk_to_cdg_arches_north() -> None:
    """A great-circle JFK → CDG passes north of the straight Mercator line."""
    start = (40.64, -73.78)  # JFK
    end = (49.01, 2.55)  # CDG
    midpoint_idx = 50 // 2
    points = great_circle_points(start, end, n_points=50)
    mid_lat, _mid_lon = points[midpoint_idx]
    # Straight Mercator midpoint of JFK→CDG is at lat ~44.8.
    # Great-circle midpoint is at ~52° (curving north over Greenland).
    straight_mid_lat = (start[0] + end[0]) / 2
    assert mid_lat > straight_mid_lat + 4  # at least 4° farther north


def test_short_distance_arc_is_nearly_linear() -> None:
    """JFK → BOS is short (~300 km); arc should be nearly straight."""
    start = (40.64, -73.78)  # JFK
    end = (42.36, -71.01)  # BOS
    points = great_circle_points(start, end, n_points=20)
    mid_lat, mid_lon = points[10]
    straight_lat = (start[0] + end[0]) / 2
    straight_lon = (start[1] + end[1]) / 2
    assert abs(mid_lat - straight_lat) < 0.1
    assert abs(mid_lon - straight_lon) < 0.1


def test_antipodal_points_no_crash() -> None:
    """Edge case: nearly-antipodal points (great-circle is degenerate)."""
    points = great_circle_points((0, 0), (0, 179.99), n_points=10)
    assert len(points) == 10
    assert points[0] == pytest.approx((0, 0), abs=1e-3)
    assert points[-1] == pytest.approx((0, 179.99), abs=1e-3)


def test_default_n_points_is_50() -> None:
    points = great_circle_points((0, 0), (0, 90))
    assert len(points) == 50


def test_n_points_below_2_raises() -> None:
    with pytest.raises(ValueError):
        great_circle_points((0, 0), (0, 1), n_points=1)


def test_coincident_endpoints_returns_n_copies() -> None:
    """Start == end: degenerate slerp; return n_points copies of start."""
    points = great_circle_points((10.0, 20.0), (10.0, 20.0), n_points=5)
    assert len(points) == 5
    assert all(p == (10.0, 20.0) for p in points)
