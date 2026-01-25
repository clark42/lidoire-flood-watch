"""Netatmo weather data collector.

Fetches rainfall and weather data from Netatmo public stations near the moulin.
https://dev.netatmo.com/apidocumentation/weather

Netatmo provides access to data from citizen weather stations, giving
hyper-local rainfall data that is crucial for small basin flood prediction.

Authentication:
- Requires Netatmo API credentials (client_id, client_secret)
- Uses OAuth2 Authorization Code flow
- Tokens are obtained via /api/auth/netatmo endpoint
- Tokens are automatically refreshed and persisted
"""

import httpx
from datetime import datetime, timedelta
from typing import Optional
import math

from app.config import get_settings
from app.models.schemas import RainfallObservation


# Netatmo API endpoints
NETATMO_AUTH_URL = "https://api.netatmo.com/oauth2/token"
NETATMO_API_BASE = "https://api.netatmo.com/api"

# Cache for station data
_stations_cache: dict = {}
_stations_cache_expiry: Optional[datetime] = None
CACHE_DURATION_MINUTES = 10


def _get_stored_tokens() -> dict:
    """Get tokens from persistent storage."""
    try:
        from app.api.auth import get_netatmo_tokens
        return get_netatmo_tokens()
    except ImportError:
        return {}


def _update_stored_tokens(access_token: str, refresh_token: str, expires_in: int) -> None:
    """Update tokens in persistent storage."""
    try:
        from app.api.auth import update_netatmo_tokens
        update_netatmo_tokens(access_token, refresh_token, expires_in)
    except ImportError:
        print("Warning: Could not persist Netatmo tokens")


async def _get_access_token() -> Optional[str]:
    """
    Get or refresh Netatmo access token.

    Tokens are stored persistently via the auth module.
    After initial authorization via /api/auth/netatmo, tokens are
    automatically refreshed as needed.

    Returns:
        Access token string or None if not available
    """
    settings = get_settings()

    # Check if we have credentials
    if not settings.netatmo_client_id or not settings.netatmo_client_secret:
        print("Netatmo client credentials not configured")
        return None

    # Load stored tokens
    tokens = _get_stored_tokens()

    # Check if we have a valid access token
    if tokens.get("access_token") and tokens.get("expires_at"):
        try:
            expiry = datetime.fromisoformat(tokens["expires_at"])
            if expiry > datetime.utcnow() + timedelta(minutes=5):
                # Token still valid (with 5 min buffer)
                return tokens["access_token"]
        except Exception:
            pass

    # Need to refresh - check for refresh token
    refresh_token = tokens.get("refresh_token")

    # Fallback to env var if no stored token
    if not refresh_token:
        refresh_token = settings.netatmo_refresh_token

    if not refresh_token:
        print("Netatmo: No refresh token available. Please authorize at /api/auth/netatmo")
        return None

    # Refresh the token
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                NETATMO_AUTH_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": settings.netatmo_client_id,
                    "client_secret": settings.netatmo_client_secret,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=30.0,
            )

            if response.status_code != 200:
                error_text = response.text
                print(f"Netatmo refresh error: {response.status_code} - {error_text}")

                # If refresh token is invalid, need to re-authorize
                if "invalid" in error_text.lower() or response.status_code == 400:
                    print("Netatmo: Refresh token invalid. Please re-authorize at /api/auth/netatmo")

                return None

            data = response.json()

            access_token = data.get("access_token")
            new_refresh_token = data.get("refresh_token")
            expires_in = data.get("expires_in", 10800)  # Usually 3 hours

            if not access_token:
                print("Netatmo: No access token in response")
                return None

            # Save tokens (Netatmo rotates refresh tokens since May 2024)
            _update_stored_tokens(
                access_token,
                new_refresh_token or refresh_token,
                expires_in
            )

            print(f"Netatmo token refreshed, expires in {expires_in}s")
            return access_token

    except Exception as e:
        print(f"Error refreshing Netatmo token: {e}")
        return None


async def fetch_public_stations_data(
    lat: float,
    lon: float,
    radius_km: float = 75.0,
) -> list[dict]:
    """
    Fetch data from public Netatmo stations in an area.

    Args:
        lat: Center latitude
        lon: Center longitude
        radius_km: Search radius in kilometers

    Returns:
        List of station data with measurements
    """
    global _stations_cache, _stations_cache_expiry

    now = datetime.utcnow()

    # Check cache
    cache_key = f"{lat:.3f},{lon:.3f},{radius_km}"
    if (_stations_cache_expiry and _stations_cache_expiry > now
            and cache_key in _stations_cache):
        return _stations_cache[cache_key]

    token = await _get_access_token()
    if not token:
        print("No Netatmo token available")
        return []

    # Calculate bounding box from center + radius
    # Approximate: 1 degree latitude = 111km
    lat_delta = radius_km / 111.0
    lon_delta = radius_km / (111.0 * math.cos(math.radians(lat)))

    url = f"{NETATMO_API_BASE}/getpublicdata"

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                params={
                    "lat_ne": lat + lat_delta,
                    "lon_ne": lon + lon_delta,
                    "lat_sw": lat - lat_delta,
                    "lon_sw": lon - lon_delta,
                    "required_data": "rain",  # Only stations with rain gauges
                    "filter": True,
                },
                headers={"Authorization": f"Bearer {token}"},
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()

            stations = []
            for station in data.get("body", []):
                station_info = _parse_station_data(station, lat, lon)
                if station_info:
                    stations.append(station_info)

            # Sort by distance
            stations.sort(key=lambda s: s.get("distance_km", 999))

            # Update cache
            _stations_cache[cache_key] = stations
            _stations_cache_expiry = now + timedelta(minutes=CACHE_DURATION_MINUTES)

            return stations

    except httpx.HTTPStatusError as e:
        print(f"Netatmo API error: {e.response.status_code}")
        return []
    except Exception as e:
        print(f"Error fetching Netatmo data: {e}")
        return []


def _parse_station_data(station: dict, ref_lat: float, ref_lon: float) -> Optional[dict]:
    """Parse a single station's data from Netatmo response."""
    try:
        location = station.get("place", {}).get("location", [])
        if len(location) < 2:
            return None

        station_lon, station_lat = location[0], location[1]

        # Calculate distance from reference point
        distance = _haversine_distance(ref_lat, ref_lon, station_lat, station_lon)

        # Get module data
        measures = station.get("measures", {})
        rain_data = None
        outdoor_data = None

        for module_id, module_data in measures.items():
            # Rain modules have a different structure - data is directly in the module
            # Format: {"rain_60min": 0, "rain_24h": 20.6, "rain_live": 0, "rain_timeutc": 123456}
            if "rain_60min" in module_data or "rain_24h" in module_data:
                rain_timeutc = module_data.get("rain_timeutc", 0)
                rain_data = {
                    "rain_60min_mm": module_data.get("rain_60min", 0) or 0,
                    "rain_24h_mm": module_data.get("rain_24h", 0) or 0,
                    "rain_live_mm": module_data.get("rain_live", 0) or 0,
                    "timestamp": datetime.fromtimestamp(rain_timeutc) if rain_timeutc else datetime.utcnow(),
                }

            # Temperature/humidity modules have res structure
            types = module_data.get("type", []) or []
            res = module_data.get("res", {})

            if res and "temperature" in types:
                latest_ts = max(res.keys())
                values = res[latest_ts]
                temp_idx = types.index("temperature")
                outdoor_data = {
                    "temperature_c": values[temp_idx] if temp_idx < len(values) else None,
                }
                if "humidity" in types:
                    hum_idx = types.index("humidity")
                    outdoor_data["humidity_percent"] = values[hum_idx] if hum_idx < len(values) else None

        if not rain_data:
            return None

        return {
            "station_id": station.get("_id", ""),
            "lat": station_lat,
            "lon": station_lon,
            "distance_km": round(distance, 1),
            "city": station.get("place", {}).get("city", "Unknown"),
            "altitude": station.get("place", {}).get("altitude", 0),
            "rain": rain_data,
            "outdoor": outdoor_data,
        }

    except Exception as e:
        print(f"Error parsing Netatmo station: {e}")
        return None


def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two points in km using Haversine formula."""
    R = 6371  # Earth's radius in km

    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)

    a = (math.sin(delta_lat / 2) ** 2 +
         math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return R * c


def _match_priority_station(station: dict, priority_stations: list[dict]) -> dict | None:
    """Check if a station matches a priority Lidoire station.

    Uses both name matching AND distance validation to avoid false positives.
    A station must be within 15km of the expected location to match.
    """
    city = station.get("city", "").lower()
    station_lat = station.get("lat", 0)
    station_lon = station.get("lon", 0)

    for priority in priority_stations:
        priority_city = priority["city"].lower()

        # Name matching - must be a good match
        name_match = False
        if priority_city == city:
            # Exact match
            name_match = True
        elif len(priority_city) >= 6 and priority_city in city:
            # Priority name is contained in station city (e.g., "Castillon" in "Castillon-la-Bataille")
            name_match = True
        elif len(city) >= 6 and city in priority_city:
            # Station city is contained in priority (e.g., "Flaujagues" in "Flaujagues")
            name_match = True

        if not name_match:
            continue

        # Distance validation - must be within 15km of expected location
        distance = _haversine_distance(
            priority["lat"], priority["lon"],
            station_lat, station_lon
        )
        if distance <= 15.0:
            return priority

    return None


async def get_basin_rainfall() -> dict:
    """
    Get rainfall data from Netatmo stations covering the Lidoire basin.

    Returns aggregated rainfall data from multiple stations.
    Prioritizes stations on the Lidoire river path.
    """
    settings = get_settings()
    priority_stations = getattr(settings, 'lidoire_netatmo_priority', [])

    # Fetch stations near the moulin
    # Note: Rain gauges are rare, need larger radius (closest ~50km from moulin)
    stations = await fetch_public_stations_data(
        lat=settings.moulin_lat,
        lon=settings.moulin_lon,
        radius_km=75.0,  # Larger radius to find rain gauges
    )

    if not stations:
        return {
            "available": False,
            "stations_count": 0,
            "rain_1h_avg_mm": 0.0,
            "rain_24h_avg_mm": 0.0,
            "auth_url": "/api/auth/netatmo",
        }

    # Calculate averages (weighted by inverse distance)
    total_weight = 0.0
    weighted_rain_1h = 0.0
    weighted_rain_24h = 0.0

    for station in stations:
        rain = station.get("rain", {})
        distance = station.get("distance_km", 10)

        # Inverse distance weighting (closer stations matter more)
        weight = 1.0 / (distance + 1)  # +1 to avoid division by zero

        rain_1h = rain.get("rain_60min_mm", 0) or 0
        rain_24h = rain.get("rain_24h_mm", 0) or 0

        weighted_rain_1h += rain_1h * weight
        weighted_rain_24h += rain_24h * weight
        total_weight += weight

    avg_rain_1h = weighted_rain_1h / total_weight if total_weight > 0 else 0.0
    avg_rain_24h = weighted_rain_24h / total_weight if total_weight > 0 else 0.0

    # Find closest station for display
    closest = stations[0] if stations else None

    # Tag priority Lidoire stations
    lidoire_stations = []
    other_stations = []

    for s in stations:
        station_data = {
            "city": s.get("city"),
            "distance_km": s.get("distance_km"),
            "rain_1h_mm": s.get("rain", {}).get("rain_60min_mm"),
            "rain_24h_mm": s.get("rain", {}).get("rain_24h_mm"),
            "lat": s.get("lat"),
            "lon": s.get("lon"),
        }

        priority = _match_priority_station(s, priority_stations)
        if priority:
            station_data["is_lidoire"] = True
            station_data["position"] = priority.get("position", "unknown")
            lidoire_stations.append(station_data)
        else:
            station_data["is_lidoire"] = False
            other_stations.append(station_data)

    # Sort Lidoire stations by position (amont -> aval)
    position_order = {"amont": 0, "milieu": 1, "aval": 2, "unknown": 3}
    lidoire_stations.sort(key=lambda x: position_order.get(x.get("position", "unknown"), 3))

    return {
        "available": True,
        "timestamp": datetime.utcnow(),
        "stations_count": len(stations),
        "rain_1h_avg_mm": round(avg_rain_1h, 1),
        "rain_24h_avg_mm": round(avg_rain_24h, 1),
        "closest_station": {
            "city": closest.get("city") if closest else None,
            "distance_km": closest.get("distance_km") if closest else None,
            "rain_1h_mm": closest.get("rain", {}).get("rain_60min_mm") if closest else None,
            "rain_24h_mm": closest.get("rain", {}).get("rain_24h_mm") if closest else None,
        } if closest else None,
        "lidoire_stations": lidoire_stations,  # Priority stations on Lidoire basin
        "stations": other_stations[:10],  # Other closest stations (limit 10)
    }


async def get_rainfall_observations() -> list[RainfallObservation]:
    """
    Get rainfall observations from Netatmo formatted as RainfallObservation.

    Returns list of observations for use in risk calculation.
    """
    basin_data = await get_basin_rainfall()

    if not basin_data.get("available"):
        return []

    observations = []

    # Create an observation from the basin average
    observations.append(
        RainfallObservation(
            timestamp=basin_data.get("timestamp", datetime.utcnow()),
            source="netatmo",
            precipitation_mm_h=basin_data.get("rain_1h_avg_mm", 0),
            precipitation_mm_24h=basin_data.get("rain_24h_avg_mm", 0),
            basin_avg=True,
        )
    )

    # Also add individual station observations
    for station in basin_data.get("stations", []):
        observations.append(
            RainfallObservation(
                timestamp=basin_data.get("timestamp", datetime.utcnow()),
                source=f"netatmo-{station.get('city', 'unknown')}",
                precipitation_mm_h=station.get("rain_1h_mm") or 0,
                precipitation_mm_24h=station.get("rain_24h_mm") or 0,
                basin_avg=False,
            )
        )

    return observations
