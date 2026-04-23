"""Geometry helpers shared across tools.

Centralises the great-circle distance computation that several tool modules
were each duplicating. Two flavours are exported for ergonomics:

- `haversine_m(...)` returns meters
- `haversine_km(...)` returns kilometers

Both take `(lat1, lng1, lat2, lng2)` in decimal degrees.
"""

from __future__ import annotations

import math

_EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two WGS84 points, in meters."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two WGS84 points, in kilometers."""
    return haversine_m(lat1, lng1, lat2, lng2) / 1000.0
