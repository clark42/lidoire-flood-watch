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

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app.config import get_settings
from app.models.schemas import RainfallObservation
from app.services.cache import (
    EXTERNAL_TIMEOUT_S,
    CircuitBreaker,
    TTLCache,
    describe_error,
    is_upstream_failure,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


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

# Request paths never call Infoclimat: the scheduler job (every 15 min) runs
# get_basin_rainfall_infoclimat(), whose last available result is served by
# peek_basin_rainfall_infoclimat() for up to LATEST_MAX_AGE_S.
LATEST_MAX_AGE_S = 60 * 60
_latest = TTLCache(ttl_seconds=LATEST_MAX_AGE_S, max_entries=1)

# One breaker for the whole host (stations list + observations)
_breaker = CircuitBreaker("Infoclimat")


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
    if not _breaker.allow():
        return []

    # Direct Infoclimat GeoJSON endpoint
    url = "https://www.infoclimat.fr/opendata/stations_xhr.php?format=geojson"

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(url, timeout=EXTERNAL_TIMEOUT_S)
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
            _breaker.record_success()
            logger.info(f"Infoclimat: loaded {len(stations)} stations")
            return stations

    except Exception as e:
        if is_upstream_failure(e):
            _breaker.record_failure(e)
        else:
            logger.warning("Error fetching Infoclimat stations list: %s", describe_error(e))
        return []


async def find_stations_near(
    lat: float, lon: float, radius_km: float = 30, fetch: bool = True,
) -> list[dict]:
    """
    Find Infoclimat stations within a radius of a point.

    Args:
        lat: Center latitude
        lon: Center longitude
        radius_km: Search radius in km
        fetch: False on request paths: only the already loaded list is used

    Returns:
        List of stations with distance, sorted by proximity
    """
    stations = await fetch_stations_list() if fetch else (_stations_cache or [])

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
        logger.info("Infoclimat API key not configured")
        return {}
    if not _breaker.allow():
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
        # Repeated stations[] key; httpx drops a query string already present in the URL
        "stations[]": station_ids,
    }

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(
                INFOCLIMAT_API_URL,
                params=params,
                timeout=EXTERNAL_TIMEOUT_S,
            )

            if response.status_code == 403:
                logger.warning(f"Infoclimat API: Access denied (IP not authorized)")
                return {}

            response.raise_for_status()
            data = response.json()

            _breaker.record_success()
            return data

    except Exception as e:
        if is_upstream_failure(e):
            _breaker.record_failure(e)
        else:
            # The body names the cause, e.g. {"errors":["Wrong IP address"]} for an IP-bound token
            logger.warning("Error fetching Infoclimat observations: %s", describe_error(e))
        return {}


async def get_basin_rainfall_infoclimat() -> dict:
    """
    Get rainfall data from Infoclimat stations in the Lidoire basin.

    Returns:
        Dict with rainfall data and station info
    """
    global _observations_cache, _cache_expiry

    settings = get_settings()
    now = _utcnow()

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
    # Infoclimat API v2 returns data in format:
    #   {"metadata": {...}, "hourly": {"STATION_ID": {"timestamp": {...}, ...}, ...}}
    # OR (legacy/alternative):
    #   {"STATION_ID": {"timestamp": {...}, ...}, ...}
    # We need to handle both formats.
    station_ids_set = set(station_ids)
    rainfall_data = []
    total_rain_24h = 0.0

    # Determine where station data lives
    station_data_source = observations
    if "hourly" in observations and isinstance(observations["hourly"], dict):
        station_data_source = observations["hourly"]
    elif "daily" in observations and isinstance(observations["daily"], dict):
        station_data_source = observations["daily"]


    for station_id, station_obs in station_data_source.items():
        # Skip non-station keys
        if station_id not in station_ids_set:
            continue

        # Station data can be a list of observations or a dict keyed by timestamp
        # Sorted chronologically: the "most recent pluie_24h" scan below relies on it
        # (API ordering is not guaranteed). ISO / "YYYY-MM-DD HH:MM:SS" UTC strings
        # sort lexicographically.
        observations_list: list[dict] = []
        if isinstance(station_obs, list):
            observations_list = sorted(
                (o for o in station_obs if isinstance(o, dict)),
                key=lambda o: str(o.get("dh_utc") or ""),
            )
        elif isinstance(station_obs, dict):
            observations_list = [
                o for _, o in sorted(station_obs.items(), key=lambda kv: str(kv[0]))
                if isinstance(o, dict)
            ]
        else:
            continue

        station_info = next((s for s in stations if s["id"] == station_id), {})
        station_rain = 0.0

        def _to_float(val) -> float:
            """Convert API value to float (handles str, int, float, None)."""
            if val is None:
                return 0.0
            try:
                return float(val)
            except (ValueError, TypeError):
                return 0.0

        # Use pluie_24h from the most recent observation (rolling 24h counter)
        # This avoids double-counting from summing overlapping pluie_1h values.
        # Infoclimat API v2 uses French field names; values may be strings.
        last_pluie_24h = None
        if observations_list:
            for obs in reversed(observations_list):
                val = obs.get("pluie_24h")
                if val is not None:
                    parsed = _to_float(val)
                    if parsed >= 0:
                        last_pluie_24h = parsed
                        break

        # Fallback: sum pluie_1h if pluie_24h unavailable
        if last_pluie_24h is None:
            for obs in observations_list:
                rain_1h = _to_float(obs.get("pluie_1h") or obs.get("RR1") or 0)
                if rain_1h > 0:
                    station_rain += rain_1h

        rain_24h_value = last_pluie_24h if last_pluie_24h is not None else station_rain

        rainfall_data.append({
            "station_id": station_id,
            "station_name": station_info.get("name", "Unknown"),
            "distance_km": station_info.get("distance_km"),
            "rain_24h_mm": round(rain_24h_value, 1),
        })
        total_rain_24h += rain_24h_value

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
    _latest.set("basin", result)

    return result


def peek_basin_rainfall_infoclimat() -> dict:
    """Last available Infoclimat basin rainfall, never calling the API.

    Used by request paths (realtime inputs, /api/realtime/*): the data is
    fetched by the scheduler job. Unavailable (same shape as a failed fetch)
    when no result younger than LATEST_MAX_AGE_S exists.
    """
    cached = _latest.get("basin")
    if cached is not None:
        return cached
    return {
        "available": False,
        "reason": "No recent Infoclimat data (fetched in the background)",
        "stations": [],
    }


async def store_rainfall_to_influx(data: dict) -> int:
    """Store Infoclimat rainfall data to InfluxDB.

    Only the 24h counter is known here, so the snapshot is written WITHOUT a
    precipitation_mm_h field (it used to be a fake 0.0 that diluted the hourly
    "infoclimat" series filled by the pluie_1h backfill).
    """
    from app.services import influx_client

    timestamp = _utcnow()

    rows = [("basin_average", data.get("rain_24h_avg_mm", 0))]
    rows += [(station.get("station_name"), station.get("rain_24h_mm", 0))
             for station in data.get("stations", [])]

    points = []
    for city, rain_24h in rows:
        tags = {"source": "infoclimat"}
        if city:
            tags["city"] = str(city)
        points.append(influx_client.make_point(
            measurement="rainfall",
            tags=tags,
            fields={"precipitation_mm_24h": float(rain_24h or 0)},
            timestamp=timestamp,
        ))

    return await influx_client.write_points_batch(points)


async def get_rainfall_observations_infoclimat() -> list[RainfallObservation]:
    """
    Get rainfall observations from Infoclimat as RainfallObservation objects.
    """
    data = await get_basin_rainfall_infoclimat()

    if not data.get("available"):
        return []

    # Store to InfluxDB
    await store_rainfall_to_influx(data)

    observations = []

    # Add basin average
    observations.append(
        RainfallObservation(
            timestamp=_utcnow(),
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
                timestamp=_utcnow(),
                source=f"infoclimat-{station.get('station_name', 'unknown')}",
                precipitation_mm_h=0,
                precipitation_mm_24h=station.get("rain_24h_mm", 0),
                basin_avg=False,
            )
        )

    return observations


async def fetch_and_store_infoclimat_historical_range(
    start: datetime,
    end: datetime,
) -> int:
    """Fetch historical hourly rainfall from Infoclimat and store in InfluxDB.

    Chunks requests into 7-day windows (API limit).
    Discovers nearby stations automatically.

    Args:
        start: Start datetime
        end: End datetime

    Returns:
        Number of data points stored
    """
    from app.services import influx_client

    settings = get_settings()
    if not settings.infoclimat_api_key:
        logger.info("Infoclimat: API key not configured, skipping historical backfill")
        return 0

    # Find nearby stations
    stations = await find_stations_near(
        lat=settings.moulin_lat,
        lon=settings.moulin_lon,
        radius_km=40,
    )
    if not stations:
        logger.info("Infoclimat: no stations found nearby")
        return 0

    station_ids = [s["id"] for s in stations[:5] if s.get("id")]
    if not station_ids:
        return 0

    station_lookup = {s["id"]: s for s in stations}
    total_stored = 0

    # Chunk into 7-day windows
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=7), end)

        logger.info(f"Infoclimat: fetching {chunk_start.date()} -> {chunk_end.date()} "
              f"for {len(station_ids)} stations...")

        data = await fetch_station_observations(station_ids, chunk_start, chunk_end)

        if not data:
            logger.info(f"  No data returned (API may be IP-restricted)")
            break  # Don't retry other chunks if IP-restricted

        # Parse hourly observations from each station
        station_data_source = data
        if "hourly" in data and isinstance(data["hourly"], dict):
            station_data_source = data["hourly"]

        for station_id, station_obs in station_data_source.items():
            if station_id not in set(station_ids):
                continue

            info = station_lookup.get(station_id, {})
            station_name = info.get("name", station_id)

            observations_list: list[dict] = []
            if isinstance(station_obs, dict):
                observations_list = [
                    {"timestamp_str": ts, **obs}
                    for ts, obs in station_obs.items()
                    if isinstance(obs, dict)
                ]
            elif isinstance(station_obs, list):
                observations_list = [o for o in station_obs if isinstance(o, dict)]

            points = []
            for obs in observations_list:
                try:
                    # Parse timestamp
                    ts_str = obs.get("timestamp_str") or obs.get("dh_utc") or ""
                    if not ts_str:
                        continue
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")) if "T" in ts_str else datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S")

                    # Parse rain (pluie_1h = hourly rainfall in mm)
                    rain_1h = obs.get("pluie_1h")
                    if rain_1h is None:
                        continue
                    rain_val = float(rain_1h) if rain_1h else 0.0

                    points.append(influx_client.make_rainfall_point(
                        source="infoclimat",
                        precipitation_mm_h=rain_val,
                        timestamp=ts,
                        station_city=station_name,
                    ))
                except (ValueError, TypeError, KeyError):
                    continue

            stored = await influx_client.write_points_batch(points) if points else 0
            if stored:
                logger.info(f"  {station_name}: {stored} hourly points")
            total_stored += stored

        chunk_start = chunk_end

    return total_stored
