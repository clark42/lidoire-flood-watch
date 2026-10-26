"""Geographic helpers (pure, no settings import: usable from config.py)."""

import math

EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle ("as the crow flies") distance in km between two points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def display_distance_km(
    origin_lat: float, origin_lon: float, lat: float | None, lon: float | None
) -> float | None:
    """Distance shown in the UI: haversine rounded to 0.1 km, None without coordinates."""
    if lat is None or lon is None:
        return None
    return round(haversine_km(origin_lat, origin_lon, lat, lon), 1)
