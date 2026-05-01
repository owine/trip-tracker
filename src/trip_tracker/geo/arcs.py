"""Great-circle interpolation via spherical linear interpolation (slerp).

Used to render flight arcs that look correctly curved on a 2D map (Web
Mercator, Equirectangular, etc.) instead of straight Mercator lines that
slice through the wrong hemisphere on long-haul routes.
"""

from __future__ import annotations

import math


def _to_xyz(lat: float, lon: float) -> tuple[float, float, float]:
    """Lat/lon (degrees) → unit-sphere xyz."""
    lat_r = math.radians(lat)
    lon_r = math.radians(lon)
    return (
        math.cos(lat_r) * math.cos(lon_r),
        math.cos(lat_r) * math.sin(lon_r),
        math.sin(lat_r),
    )


def _from_xyz(x: float, y: float, z: float) -> tuple[float, float]:
    """Unit-sphere xyz → lat/lon (degrees)."""
    lat = math.degrees(math.asin(z))
    lon = math.degrees(math.atan2(y, x))
    return lat, lon


def great_circle_points(
    start: tuple[float, float],
    end: tuple[float, float],
    n_points: int = 50,
) -> list[tuple[float, float]]:
    """Interpolate `n_points` along the great-circle arc between two lat/lon pairs.

    Returns a list of (lat, lon) tuples suitable for Leaflet's L.polyline.
    Uses spherical linear interpolation (slerp) on the unit sphere.
    """
    if n_points < 2:
        raise ValueError("n_points must be >= 2")

    p1 = _to_xyz(*start)
    p2 = _to_xyz(*end)
    # Angle between the two vectors; clamp dot product for numerical stability.
    dot = max(-1.0, min(1.0, p1[0] * p2[0] + p1[1] * p2[1] + p1[2] * p2[2]))
    omega = math.acos(dot)

    if omega < 1e-9:
        # Coincident points: return n_points copies of start
        return [start for _ in range(n_points)]

    sin_omega = math.sin(omega)
    points: list[tuple[float, float]] = []
    for i in range(n_points):
        t = i / (n_points - 1)
        a = math.sin((1 - t) * omega) / sin_omega
        b = math.sin(t * omega) / sin_omega
        x = a * p1[0] + b * p2[0]
        y = a * p1[1] + b * p2[1]
        z = a * p1[2] + b * p2[2]
        points.append(_from_xyz(x, y, z))
    return points
