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
import unicodedata
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

# Cache for discovered Lidoire station device/module IDs (persists across requests)
# Maps city_name -> {"device_id": str, "rain_module_id": str}
_lidoire_device_cache: dict[str, dict] = {}


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

        rain_module_id = None
        for module_id, module_data in measures.items():
            # Rain modules have a different structure - data is directly in the module
            # Format: {"rain_60min": 0, "rain_24h": 20.6, "rain_live": 0, "rain_timeutc": 123456}
            if "rain_60min" in module_data or "rain_24h" in module_data:
                rain_module_id = module_id
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
            "rain_module_id": rain_module_id,
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

    Note: Netatmo API has a limit of ~1100 stations per request.
    We make multiple searches to cover both the main area and the
    upstream Lidoire basin which might otherwise be cut off.
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

    # Secondary search for upstream Lidoire basin (Saint-Rémy area)
    # This catches stations that might be cut off due to API limits
    upstream_stations = await fetch_public_stations_data(
        lat=44.95,  # Center on upstream Lidoire
        lon=0.18,
        radius_km=25.0,
    )

    # Merge stations, avoiding duplicates
    existing_ids = {s.get("station_id") for s in stations}
    for s in upstream_stations:
        if s.get("station_id") not in existing_ids:
            stations.append(s)
            existing_ids.add(s.get("station_id"))

    # Re-sort by distance from moulin
    stations.sort(key=lambda s: s.get("distance_km", 999))

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

    # First pass: identify Lidoire stations
    lidoire_cities = []
    for s in stations:
        priority = _match_priority_station(s, priority_stations)
        if priority:
            lidoire_cities.append(s.get("city"))

    # Query historical rainfall for Lidoire stations from InfluxDB
    historical_rainfall = {}
    if lidoire_cities:
        try:
            from app.services import influx_client
            historical_rainfall = await influx_client.query_all_stations_rainfall_cumulative(
                lidoire_cities, source="netatmo-lidoire"
            )
        except Exception as e:
            print(f"Error fetching historical rainfall: {e}")

    # Fallback: always fetch Open-Meteo past rainfall to supplement Netatmo
    # 72h/7d data. Netatmo only provides 24h counters; InfluxDB often has
    # insufficient history, causing 72h == 24h. Open-Meteo observed data
    # (past_days=7) fills the gap reliably.
    open_meteo_fallback = None
    if lidoire_cities:
        needs_fallback = True
        if historical_rainfall:
            sample = next(iter(historical_rainfall.values()), {})
            r24 = sample.get("rain_24h_mm")
            r72 = sample.get("rain_72h_mm")
            r7d = sample.get("rain_7d_mm")
            # Data looks complete if 72h and 7d differ from 24h
            if r24 is not None and r72 is not None and r72 != r24 and r7d is not None and r7d != r72:
                needs_fallback = False
        if needs_fallback:
            try:
                from app.services.collectors.weather import get_past_rainfall_sums
                open_meteo_fallback = await get_past_rainfall_sums(
                    settings.moulin_lat, settings.moulin_lon
                )
                if open_meteo_fallback:
                    print(f"Netatmo: using Open-Meteo fallback for 72h/7d "
                          f"(72h={open_meteo_fallback.get('rain_72h_mm')}mm, "
                          f"7d={open_meteo_fallback.get('rain_7d_mm')}mm)")
            except Exception as e:
                print(f"Error fetching Open-Meteo fallback: {e}")

    for s in stations:
        city = s.get("city")
        station_data = {
            "city": city,
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
            # Add historical data from InfluxDB
            hist = historical_rainfall.get(city, {})
            influx_24h = hist.get("rain_24h_mm")
            rain_72h = hist.get("rain_72h_mm")
            rain_7d = hist.get("rain_7d_mm")

            # Use Open-Meteo fallback when InfluxDB data is incomplete.
            # Detection: value equals influx_24h means only one 24h chunk was found.
            # Strategy: keep Netatmo 24h (real sensor), add Open-Meteo delta for older periods.
            # 72h = netatmo_24h + open_meteo_(24h..72h delta)
            # 7d  = netatmo_24h + open_meteo_(24h..7d delta)
            if open_meteo_fallback:
                station_24h = station_data.get("rain_24h_mm") or 0
                om_24h = open_meteo_fallback.get("rain_24h_mm", 0)
                om_72h = open_meteo_fallback.get("rain_72h_mm", 0)
                om_7d = open_meteo_fallback.get("rain_7d_mm", 0)
                # Delta = rain that fell between 24h ago and 72h/7d ago
                delta_72h = max(0, om_72h - om_24h)
                delta_7d = max(0, om_7d - om_24h)
                incomplete_72h = rain_72h is None or rain_72h == influx_24h
                incomplete_7d = rain_7d is None or rain_7d == influx_24h
                if incomplete_72h:
                    rain_72h = round(station_24h + delta_72h, 1)
                if incomplete_7d:
                    rain_7d = round(station_24h + delta_7d, 1)

            station_data["rain_72h_mm"] = rain_72h
            station_data["rain_7d_mm"] = rain_7d
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


async def store_rainfall_to_influx(basin_data: dict) -> int:
    """Store Netatmo rainfall data to InfluxDB."""
    from app.services import influx_client

    stored = 0
    timestamp = basin_data.get("timestamp", datetime.utcnow())

    # Store basin average
    success = await influx_client.write_rainfall(
        source="netatmo",
        precipitation_mm_h=basin_data.get("rain_1h_avg_mm", 0),
        timestamp=timestamp,
        precipitation_mm_24h=basin_data.get("rain_24h_avg_mm", 0),
        station_city="basin_average",
    )
    if success:
        stored += 1

    # Store Lidoire stations
    for station in basin_data.get("lidoire_stations", []):
        success = await influx_client.write_rainfall(
            source="netatmo-lidoire",
            precipitation_mm_h=station.get("rain_1h_mm") or 0,
            timestamp=timestamp,
            precipitation_mm_24h=station.get("rain_24h_mm") or 0,
            station_city=station.get("city"),
            lat=station.get("lat"),
            lon=station.get("lon"),
        )
        if success:
            stored += 1

    # Store other nearby stations (top 5)
    for station in basin_data.get("stations", [])[:5]:
        success = await influx_client.write_rainfall(
            source="netatmo",
            precipitation_mm_h=station.get("rain_1h_mm") or 0,
            timestamp=timestamp,
            precipitation_mm_24h=station.get("rain_24h_mm") or 0,
            station_city=station.get("city"),
            lat=station.get("lat"),
            lon=station.get("lon"),
        )
        if success:
            stored += 1

    return stored


async def _discover_lidoire_device_ids() -> dict[str, dict]:
    """Discover and cache device/module IDs for Lidoire priority stations.

    Calls getpublicdata to find stations, then matches against priority
    Lidoire stations to get their device_id and rain_module_id.

    Returns:
        Dict mapping city_name -> {"device_id": str, "rain_module_id": str, ...}
    """
    global _lidoire_device_cache

    if _lidoire_device_cache:
        return _lidoire_device_cache

    settings = get_settings()
    priority_stations = getattr(settings, 'lidoire_netatmo_priority', [])
    if not priority_stations:
        return {}

    # Fetch current stations to discover device IDs
    stations = await fetch_public_stations_data(
        lat=settings.moulin_lat,
        lon=settings.moulin_lon,
        radius_km=75.0,
    )

    # Also search upstream area
    upstream = await fetch_public_stations_data(
        lat=44.95, lon=0.18, radius_km=25.0,
    )
    existing_ids = {s.get("station_id") for s in stations}
    for s in upstream:
        if s.get("station_id") not in existing_ids:
            stations.append(s)

    # Match against priority stations and store device IDs
    for s in stations:
        priority = _match_priority_station(s, priority_stations)
        if priority:
            city = s.get("city", "Unknown")
            device_id = s.get("station_id", "")
            rain_module_id = s.get("rain_module_id")
            if device_id and rain_module_id:
                _lidoire_device_cache[city] = {
                    "device_id": device_id,
                    "rain_module_id": rain_module_id,
                    "city": city,
                    "lat": s.get("lat"),
                    "lon": s.get("lon"),
                }

    print(f"Netatmo: discovered {len(_lidoire_device_cache)} Lidoire stations with device IDs: "
          f"{list(_lidoire_device_cache.keys())}")
    return _lidoire_device_cache


async def fetch_netatmo_station_history(
    device_id: str,
    module_id: str,
    start: datetime,
    end: datetime,
    scale: str = "1hour",
) -> list[dict]:
    """Fetch historical rainfall data for a specific Netatmo station via getmeasure.

    Args:
        device_id: MAC address of the base station
        module_id: MAC address of the rain module
        start: Start datetime
        end: End datetime
        scale: Resolution (30min, 1hour, 3hours, 1day)

    Returns:
        List of {"timestamp": datetime, "rain_mm": float}
    """
    token = await _get_access_token()
    if not token:
        return []

    url = f"{NETATMO_API_BASE}/getmeasure"

    date_begin = int(start.timestamp())
    date_end = int(end.timestamp())

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                params={
                    "device_id": device_id,
                    "module_id": module_id,
                    "scale": scale,
                    "type": "sum_rain",
                    "date_begin": date_begin,
                    "date_end": date_end,
                    "optimize": "false",
                },
                headers={"Authorization": f"Bearer {token}"},
                timeout=30.0,
            )

            if response.status_code == 404:
                print(f"Netatmo getmeasure: station {device_id} not found or not public")
                return []

            response.raise_for_status()
            data = response.json()

            body = data.get("body", {})
            results = []

            for ts_str, values in body.items():
                ts = datetime.fromtimestamp(int(ts_str))
                rain_mm = values[0] if isinstance(values, list) and len(values) > 0 else 0.0
                results.append({
                    "timestamp": ts,
                    "rain_mm": rain_mm or 0.0,
                })

            results.sort(key=lambda x: x["timestamp"])
            return results

    except httpx.HTTPStatusError as e:
        print(f"Netatmo getmeasure HTTP error: {e.response.status_code} for {device_id}")
        return []
    except Exception as e:
        print(f"Netatmo getmeasure error for {device_id}: {e}")
        return []


async def fetch_and_store_netatmo_historical_range(
    start: datetime,
    end: datetime,
    cities: list[str] | None = None,
) -> int:
    """Fetch historical rainfall from Netatmo getmeasure and store in InfluxDB.

    Cache-through pattern: discover station device IDs, then fetch historical
    data for each Lidoire station via getmeasure API.

    Args:
        start: Start datetime
        end: End datetime
        cities: Optional list of cities to filter (fetch all Lidoire if None)

    Returns:
        Number of data points stored
    """
    from app.services import influx_client

    device_cache = await _discover_lidoire_device_ids()
    if not device_cache:
        print("Netatmo historical: no Lidoire station device IDs discovered")
        return 0

    total_stored = 0

    def _normalize(s: str) -> str:
        """Strip accents for comparison (Saint-Rémy == Saint-Remy)."""
        return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode()

    for city, info in device_cache.items():
        # Filter by requested cities if specified (accent-insensitive)
        if cities and _normalize(city) not in [_normalize(c) for c in cities]:
            continue

        device_id = info["device_id"]
        module_id = info["rain_module_id"]

        print(f"Netatmo historical: fetching {city} ({device_id}/{module_id}) for {start} -> {end}")

        points = await fetch_netatmo_station_history(
            device_id=device_id,
            module_id=module_id,
            start=start,
            end=end,
        )

        if not points:
            print(f"  No historical data for {city}")
            continue

        # Store to InfluxDB — normalize city name (strip accents) for consistent tags
        normalized_city = _normalize(city)
        stored = 0
        for p in points:
            success = await influx_client.write_rainfall(
                source="netatmo-lidoire",
                precipitation_mm_h=p["rain_mm"],
                timestamp=p["timestamp"],
                station_city=normalized_city,
                lat=info.get("lat"),
                lon=info.get("lon"),
            )
            if success:
                stored += 1

        print(f"  Stored {stored}/{len(points)} points for {city}")
        total_stored += stored

    return total_stored


async def get_rainfall_observations() -> list[RainfallObservation]:
    """
    Get rainfall observations from Netatmo formatted as RainfallObservation.

    Returns list of observations for use in risk calculation.
    """
    basin_data = await get_basin_rainfall()

    if not basin_data.get("available"):
        return []

    # Store to InfluxDB
    await store_rainfall_to_influx(basin_data)

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
