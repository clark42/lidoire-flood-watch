"""Infoclimat weather data collector.

Fetches rainfall and weather data from Infoclimat StatIC network.
https://www.infoclimat.fr/opendata/

Infoclimat is a French amateur weather station network with high-quality
data, complementary to Netatmo for local rainfall measurements.

API Documentation:
- URL: https://www.infoclimat.fr/opendata/?version=2&method=get&format={format}&stations[]={id}&start={date}&end={date}&token={key}
- Max 7 consecutive days per request
- IP restriction on API key
"""

import httpx
from datetime import datetime, timedelta
from typing import Optional
import math

from app.config import get_settings
from app.models.schemas import RainfallObservation


# Infoclimat API base URL
INFOCLIMAT_API_URL = "https://www.infoclimat.fr/opendata/"

# Known stations near Libourne/Lidoire area
# Can be discovered from: https://www.data.gouv.fr/fr/datasets/r/8354febf-c1e4-49ae-bf0e-d20b6891c28a
STATIONS_NEAR_LIDOIRE = [
    # Format: {"id": "station_id", "name": "name", "lat": lat, "lon": lon}
    # These will be populated dynamically or from config
]

# Cache
_stations_cache: Optional[list[dict]] = None
_observations_cache: dict = {}
_cache_expiry: Optional[datetime] = None
CACHE_DURATION_MINUTES = 15


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two points in km."""
    R = 6371
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    a = (math.sin(delta_lat / 2) ** 2 +
         math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


async def fetch_stations_list() -> list[dict]:
    """
    Fetch list of Infoclimat stations.

    Returns GeoJSON with all available stations.
    Source: https://www.data.gouv.fr/datasets/liste-des-stations-en-open-data-du-reseau-meteorologique-infoclimat-static-et-meteo-france-synop
    """
    global _stations_cache

    if _stations_cache:
        return _stations_cache

    # Direct Infoclimat GeoJSON endpoint
    url = "https://www.infoclimat.fr/opendata/stations_xhr.php?format=geojson"

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(url, timeout=30.0)
            response.raise_for_status()
            data = response.json()

            stations = []
            for feature in data.get("features", []):
                props = feature.get("properties", {})
                coords = feature.get("geometry", {}).get("coordinates", [])

                if len(coords) >= 2:
                    license_info = props.get("license", {})
                    stations.append({
                        "id": props.get("id"),
                        "name": props.get("name"),
                        "commune": props.get("name"),  # Use name as commune
                        "altitude": props.get("elevation"),
                        "lon": coords[0],
                        "lat": coords[1],
                        "departement": props.get("departement"),
                        "source": license_info.get("source", "Infoclimat"),
                        "last_activity": props.get("last_activity"),
                    })

            _stations_cache = stations
            print(f"Infoclimat: loaded {len(stations)} stations")
            return stations

    except Exception as e:
        print(f"Error fetching Infoclimat stations list: {e}")
        return []


async def find_stations_near(lat: float, lon: float, radius_km: float = 30) -> list[dict]:
    """
    Find Infoclimat stations within a radius of a point.

    Args:
        lat: Center latitude
        lon: Center longitude
        radius_km: Search radius in km

    Returns:
        List of stations with distance, sorted by proximity
    """
    stations = await fetch_stations_list()

    nearby = []
    for station in stations:
        if station.get("lat") and station.get("lon"):
            distance = _haversine(lat, lon, station["lat"], station["lon"])
            if distance <= radius_km:
                station_copy = station.copy()
                station_copy["distance_km"] = round(distance, 1)
                nearby.append(station_copy)

    nearby.sort(key=lambda s: s["distance_km"])
    return nearby


async def fetch_station_observations(
    station_ids: list[str],
    start_date: datetime,
    end_date: datetime,
) -> dict:
    """
    Fetch observations from Infoclimat stations.

    Args:
        station_ids: List of station IDs to query
        start_date: Start of period
        end_date: End of period (max 7 days from start)

    Returns:
        Dict with observations per station
    """
    settings = get_settings()
    api_key = settings.infoclimat_api_key

    if not api_key:
        print("Infoclimat API key not configured")
        return {}

    # Build request URL
    # Format: ?version=2&method=get&format=json&stations[]={id}&start={date}&end={date}&token={key}

    params = {
        "version": "2",
        "method": "get",
        "format": "json",
        "start": start_date.strftime("%Y-%m-%d"),
        "end": end_date.strftime("%Y-%m-%d"),
        "token": api_key,
    }

    # Add stations (multiple stations[] params)
    stations_params = "&".join([f"stations[]={sid}" for sid in station_ids])

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            # Build full URL with stations array
            full_url = f"{INFOCLIMAT_API_URL}?{stations_params}"

            response = await client.get(
                full_url,
                params=params,
                timeout=30.0,
            )

            if response.status_code == 403:
                print(f"Infoclimat API: Access denied (IP not authorized)")
                return {}

            response.raise_for_status()
            data = response.json()

            return data

    except httpx.HTTPStatusError as e:
        print(f"Infoclimat API error: {e.response.status_code}")
        return {}
    except Exception as e:
        print(f"Error fetching Infoclimat observations: {e}")
        return {}


async def get_basin_rainfall_infoclimat() -> dict:
    """
    Get rainfall data from Infoclimat stations in the Lidoire basin.

    Returns:
        Dict with rainfall data and station info
    """
    global _observations_cache, _cache_expiry

    settings = get_settings()
    now = datetime.utcnow()

    # Check cache
    if _cache_expiry and _cache_expiry > now and _observations_cache:
        return _observations_cache

    # Find stations near the moulin
    stations = await find_stations_near(
        lat=settings.moulin_lat,
        lon=settings.moulin_lon,
        radius_km=40,  # Wider radius for Infoclimat (fewer stations)
    )

    if not stations:
        return {
            "available": False,
            "reason": "No Infoclimat stations found nearby",
            "stations": [],
        }

    # Check API key
    if not settings.infoclimat_api_key:
        return {
            "available": False,
            "reason": "Infoclimat API key not configured",
            "stations_found": len(stations),
            "nearest_stations": stations[:5],
        }

    # Fetch last 24h of observations
    station_ids = [s["id"] for s in stations[:5] if s.get("id")]

    if not station_ids:
        return {
            "available": False,
            "reason": "No valid station IDs found",
            "stations": stations[:5],
        }

    end_date = now
    start_date = now - timedelta(days=1)

    observations = await fetch_station_observations(station_ids, start_date, end_date)

    if not observations:
        return {
            "available": False,
            "reason": "No observations returned (check API key/IP)",
            "stations_found": len(stations),
            "station_ids_queried": station_ids,
        }

    # Parse rainfall from observations
    # Infoclimat returns data in format: {"station_id": {"timestamp": {...data...}, ...}, ...}
    rainfall_data = []
    total_rain_24h = 0.0

    for station_id, station_obs in observations.items():
        if not isinstance(station_obs, dict):
            continue

        station_info = next((s for s in stations if s["id"] == station_id), {})
        station_rain = 0.0

        for timestamp_str, obs in station_obs.items():
            if not isinstance(obs, dict):
                continue

            # Look for precipitation field (RR1 = rain in last hour, RR24 = rain 24h)
            rain_1h = obs.get("RR1") or obs.get("rr1") or obs.get("precipitation", 0)
            if rain_1h and isinstance(rain_1h, (int, float)):
                station_rain += float(rain_1h)

        rainfall_data.append({
            "station_id": station_id,
            "station_name": station_info.get("name", "Unknown"),
            "distance_km": station_info.get("distance_km"),
            "rain_24h_mm": round(station_rain, 1),
        })
        total_rain_24h += station_rain

    # Calculate weighted average
    if rainfall_data:
        total_weight = 0.0
        weighted_rain = 0.0
        for rd in rainfall_data:
            dist = rd.get("distance_km") or 10  # Handle None values
            weight = 1.0 / (dist + 1)
            weighted_rain += rd["rain_24h_mm"] * weight
            total_weight += weight

        avg_rain = weighted_rain / total_weight if total_weight > 0 else 0.0
    else:
        avg_rain = 0.0

    result = {
        "available": True,
        "timestamp": now.isoformat(),
        "stations_count": len(rainfall_data),
        "rain_24h_avg_mm": round(avg_rain, 1),
        "stations": rainfall_data,
    }

    # Update cache
    _observations_cache = result
    _cache_expiry = now + timedelta(minutes=CACHE_DURATION_MINUTES)

    return result


async def get_rainfall_observations_infoclimat() -> list[RainfallObservation]:
    """
    Get rainfall observations from Infoclimat as RainfallObservation objects.
    """
    data = await get_basin_rainfall_infoclimat()

    if not data.get("available"):
        return []

    observations = []

    # Add basin average
    observations.append(
        RainfallObservation(
            timestamp=datetime.utcnow(),
            source="infoclimat",
            precipitation_mm_h=0,  # Hourly not available in summary
            precipitation_mm_24h=data.get("rain_24h_avg_mm", 0),
            basin_avg=True,
        )
    )

    # Add per-station data
    for station in data.get("stations", []):
        observations.append(
            RainfallObservation(
                timestamp=datetime.utcnow(),
                source=f"infoclimat-{station.get('station_name', 'unknown')}",
                precipitation_mm_h=0,
                precipitation_mm_24h=station.get("rain_24h_mm", 0),
                basin_avg=False,
            )
        )

    return observations
