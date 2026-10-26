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

import asyncio
import logging
import math
import re
import unicodedata
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

# Serializes token refreshes (Netatmo rotates refresh tokens)
_token_lock = asyncio.Lock()

# api.netatmo.com (token refresh + getpublicdata) skipped after a timeout/5xx
_breaker = CircuitBreaker("Netatmo")

# Request paths never call Netatmo: the scheduler job (every 15 min) runs
# get_basin_rainfall(), whose last available result is served by
# peek_basin_rainfall() for up to LATEST_MAX_AGE_S.
LATEST_MAX_AGE_S = 60 * 60
_latest = TTLCache(ttl_seconds=LATEST_MAX_AGE_S, max_entries=1)

# Cache for InfluxDB cumulative history + Open-Meteo fallback (get_basin_rainfall)
HISTORY_CACHE_MINUTES = 15
_history_cache: dict = {}

# Hourly-aligned precipitation_mm_h writes: (source, city) -> last slot written
HOURLY_WRITE_MAX_LAG = timedelta(minutes=20)
_last_hourly_write: dict[tuple[str, str], datetime] = {}

_SAFE_SOURCE_RE = re.compile(r"^[a-z0-9-]+$")

# Netatmo city spellings -> config spelling (settings.lidoire_netatmo_priority).
# Accent / case / punctuation variants of the config names ('Saint-Remy',
# 'saint rémy') are folded automatically by canonical_city(); list here only
# spellings that folding cannot recognise.
NETATMO_CITY_ALIASES: dict[str, str] = {}


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _utc_from_epoch(ts: int | float) -> datetime:
    """Epoch seconds -> naive UTC (datetime.fromtimestamp alone gives local time)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


def _fold_city(name: str) -> str:
    """Comparison key: no accents, lower case, letters and digits only."""
    stripped = unicodedata.normalize("NFD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", stripped.lower())


def canonical_city(city: Optional[str]) -> Optional[str]:
    """Config spelling of a Lidoire Netatmo city, or ``city`` unchanged.

    Used for the ``city`` tag of every ``netatmo-lidoire`` point (live and
    historical backfill), so one station is one InfluxDB series whatever
    spelling Netatmo returns. Readers (timeline) also accept legacy tags.
    """
    if not city:
        return city
    settings = get_settings()
    lookup = {_fold_city(c["city"]): c["city"] for c in settings.lidoire_netatmo_priority}
    for alias, canonical in NETATMO_CITY_ALIASES.items():
        lookup.setdefault(_fold_city(alias), canonical)
    return lookup.get(_fold_city(city), city)


def _legacy_city_tags(canonical: str, raw: Optional[str]) -> list[str]:
    """Tag values older points of ``canonical`` may carry (raw Netatmo name)."""
    return [raw] if raw and raw != canonical else []


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
        logger.warning("Could not persist Netatmo tokens")


def is_authorised() -> bool:
    """Cheap check (no network): client credentials and a token are configured.

    False means Netatmo cannot return any data until the OAuth flow
    (/api/auth/netatmo) has been completed.
    """
    settings = get_settings()
    if not settings.netatmo_client_id or not settings.netatmo_client_secret:
        return False
    tokens = _get_stored_tokens()
    return bool(
        tokens.get("refresh_token")
        or _valid_access_token(tokens)
        or settings.netatmo_refresh_token
    )


def _valid_access_token(tokens: dict) -> Optional[str]:
    """Return the stored access token if it is valid for at least 5 more minutes."""
    if tokens.get("access_token") and tokens.get("expires_at"):
        try:
            expiry = datetime.fromisoformat(tokens["expires_at"])
            if expiry > _utcnow() + timedelta(minutes=5):
                return tokens["access_token"]
        except (TypeError, ValueError):
            pass
    return None


async def _get_access_token() -> Optional[str]:
    """
    Get or refresh Netatmo access token.

    Tokens are stored persistently via the auth module.
    After initial authorization via /api/auth/netatmo, tokens are
    automatically refreshed as needed.

    Netatmo rotates refresh tokens: two concurrent refreshes with the same
    refresh token would invalidate one another. The refresh is therefore
    serialized by ``_token_lock`` and the stored tokens are re-read once the
    lock is held (a concurrent caller may already have refreshed them).

    Returns:
        Access token string or None if not available
    """
    settings = get_settings()

    # Check if we have credentials
    if not settings.netatmo_client_id or not settings.netatmo_client_secret:
        logger.info("Netatmo client credentials not configured")
        return None

    # Fast path: valid access token, no lock needed
    token = _valid_access_token(_get_stored_tokens())
    if token:
        return token

    async with _token_lock:
        tokens = _get_stored_tokens()
        token = _valid_access_token(tokens)
        if token:
            return token  # refreshed by a concurrent caller

        refresh_token = tokens.get("refresh_token") or settings.netatmo_refresh_token
        if not refresh_token:
            logger.warning("Netatmo: No refresh token available. Please authorize at /api/auth/netatmo")
            return None

        return await _refresh_access_token(refresh_token)


async def _refresh_access_token(refresh_token: str) -> Optional[str]:
    """Exchange a refresh token for a new access token and persist both.

    Must be called with ``_token_lock`` held.
    """
    settings = get_settings()
    if not _breaker.allow():
        return None
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
                timeout=EXTERNAL_TIMEOUT_S,
            )

            if response.status_code >= 500:
                _breaker.record_failure(f"token refresh HTTP {response.status_code}")
                return None
            if response.status_code != 200:
                error_text = response.text
                logger.warning("Netatmo refresh error: %s - %s", response.status_code, error_text)

                # If refresh token is invalid, need to re-authorize
                if "invalid" in error_text.lower() or response.status_code == 400:
                    logger.warning("Netatmo: Refresh token invalid. Please re-authorize at /api/auth/netatmo")

                return None

            data = response.json()

            access_token = data.get("access_token")
            new_refresh_token = data.get("refresh_token")
            expires_in = data.get("expires_in", 10800)  # Usually 3 hours

            if not access_token:
                logger.warning("Netatmo: No access token in response")
                return None

            # Save tokens (Netatmo rotates refresh tokens since May 2024)
            _update_stored_tokens(
                access_token,
                new_refresh_token or refresh_token,
                expires_in
            )

            logger.info("Netatmo token refreshed, expires in %ss", expires_in)
            return access_token

    except Exception as e:
        if is_upstream_failure(e):
            _breaker.record_failure(e)
        else:
            logger.warning("Error refreshing Netatmo token: %s", describe_error(e))
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

    now = _utcnow()

    # Check cache
    cache_key = f"{lat:.3f},{lon:.3f},{radius_km}"
    if (_stations_cache_expiry and _stations_cache_expiry > now
            and cache_key in _stations_cache):
        return _stations_cache[cache_key]
    if not _breaker.allow():
        return []

    token = await _get_access_token()
    if not token:
        logger.info("No Netatmo token available")
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
                timeout=EXTERNAL_TIMEOUT_S,
            )
            response.raise_for_status()
            data = response.json()
            _breaker.record_success()

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

    except Exception as e:
        if is_upstream_failure(e):
            _breaker.record_failure(e)
        elif isinstance(e, httpx.HTTPStatusError):
            logger.info(f"Netatmo API error: {e.response.status_code}")
        else:
            logger.warning("Error fetching Netatmo data: %s", describe_error(e))
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
                    "timestamp": _utc_from_epoch(rain_timeutc) if rain_timeutc else _utcnow(),
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
        logger.warning(f"Error parsing Netatmo station: {e}")
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
    A station must be within 15km of the expected location to match. Names
    are compared accent/case/punctuation-insensitively ('Saint-Remy' matches
    'Saint-Rémy').
    """
    city = _fold_city(station.get("city") or "")
    station_lat = station.get("lat", 0)
    station_lon = station.get("lon", 0)

    for priority in priority_stations:
        priority_city = _fold_city(priority["city"])

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


def consecutive_daily_coverage(
    timestamps: list[datetime],
    now: datetime,
    max_days: int = 7,
) -> int:
    """Number of consecutive 24h chunks (k = 0, 1, ...) that have a sample.

    Mirrors the sampling of influx_client.query_station_rainfall_cumulative:
    chunk k is covered when a precipitation_mm_24h point exists in
    [now - 24k h - 3h, now - 24k h + 1h]. A 72h cumulative is complete when
    coverage >= 3, a 7-day one when coverage >= 7. Counting points instead of
    comparing values avoids flagging dry periods (0 == 0) as incomplete.
    """
    covered = 0
    for k in range(max_days):
        target = now - timedelta(hours=24 * k)
        lo = target - timedelta(hours=3)
        hi = target + timedelta(hours=1)
        if any(lo <= ts <= hi for ts in timestamps):
            covered += 1
        else:
            break
    return covered


async def _query_daily_coverage(
    cities: list[str],
    source: str = "netatmo-lidoire",
    aliases: Optional[dict[str, list[str]]] = None,
) -> dict[str, int]:
    """Per-city consecutive 24h coverage of precipitation_mm_24h samples (one query).

    Cities are filtered in Python (never interpolated into Flux: they come from
    the Netatmo API). Samples tagged with an alias of a city (legacy spelling,
    see ``aliases``) count for that city.
    """
    from app.services import influx_client

    if not _SAFE_SOURCE_RE.match(source):
        raise ValueError(f"invalid rainfall source: {source!r}")

    now = _utcnow()
    start = now - timedelta(days=7, hours=4)
    stop = now + timedelta(minutes=5)
    query = f'''
    from(bucket: {influx_client._bucket()})
        |> range(start: {influx_client._flux_ts(start)}, stop: {influx_client._flux_ts(stop)})
        |> filter(fn: (r) => r._measurement == "rainfall")
        |> filter(fn: (r) => r.source == {influx_client.flux_string(source)})
        |> filter(fn: (r) => r._field == "precipitation_mm_24h")
        |> keep(columns: ["_time", "city"])
    '''
    tag_to_city = {c: c for c in cities}
    for city, names in (aliases or {}).items():
        for name in names:
            tag_to_city.setdefault(name, city)
    by_city: dict[str, list[datetime]] = {c: [] for c in cities}
    # Non-blocking: the synchronous InfluxDB client runs in a worker thread
    tables = await influx_client._run_query(query)
    for table in tables:
        for record in table.records:
            city = tag_to_city.get(record.values.get("city"))
            if city is not None:
                ts = record.get_time()
                if ts is not None:
                    by_city[city].append(_to_naive_utc(ts))
    return {c: consecutive_daily_coverage(ts_list, now) for c, ts_list in by_city.items()}


async def _get_lidoire_history(
    cities: list[str],
    aliases: Optional[dict[str, list[str]]] = None,
) -> tuple[dict, dict, Optional[dict]]:
    """InfluxDB cumulative rain, coverage and Open-Meteo fallback, cached 15 min.

    ``cities`` are canonical (config) names; ``aliases`` lists legacy tag
    values merged into each of them.

    Returns (historical_rainfall, coverage, open_meteo_fallback).
    """
    global _history_cache

    settings = get_settings()
    aliases = aliases or {}
    key = (tuple(sorted(cities)), tuple(sorted((c, tuple(a)) for c, a in aliases.items())))
    now = _utcnow()
    if (_history_cache.get("key") == key and _history_cache.get("expires")
            and _history_cache["expires"] > now):
        return _history_cache["historical"], _history_cache["coverage"], _history_cache["fallback"]

    from app.services import influx_client

    historical_rainfall: dict = {}
    try:
        historical_rainfall = await influx_client.query_all_stations_rainfall_cumulative(
            cities, source="netatmo-lidoire", aliases=aliases
        )
    except Exception as e:
        logger.warning("Error fetching historical rainfall: %s", e)

    coverage: dict[str, int] = {}
    try:
        coverage = await _query_daily_coverage(cities, source="netatmo-lidoire", aliases=aliases)
    except Exception as e:
        logger.warning("Error querying Netatmo rainfall coverage: %s", e)

    # Open-Meteo fallback for 72h/7d when InfluxDB history is incomplete
    # (Netatmo only provides 24h counters). The Open-Meteo call itself is
    # cached 15 min in the weather collector.
    open_meteo_fallback: Optional[dict] = None
    needs_fallback = any(coverage.get(c, 0) < 7 for c in cities)
    if needs_fallback:
        try:
            from app.services.collectors.weather import get_past_rainfall_sums
            open_meteo_fallback = await get_past_rainfall_sums(
                settings.moulin_lat, settings.moulin_lon
            ) or None
        except Exception as e:
            logger.warning("Error fetching Open-Meteo fallback: %s", e)

    _history_cache = {
        "key": key,
        "expires": now + timedelta(minutes=HISTORY_CACHE_MINUTES),
        "historical": historical_rainfall,
        "coverage": coverage,
        "fallback": open_meteo_fallback,
    }
    return historical_rainfall, coverage, open_meteo_fallback


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
    # Copy: the returned list is the cached object and is extended/sorted below
    stations = list(await fetch_public_stations_data(
        lat=settings.moulin_lat,
        lon=settings.moulin_lon,
        radius_km=75.0,  # Larger radius to find rain gauges
    ))

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

    # First pass: identify Lidoire stations. InfluxDB city tags use the config
    # spelling (priority["city"]); older points may carry the raw Netatmo name.
    lidoire_cities: list[str] = []
    lidoire_aliases: dict[str, list[str]] = {}
    for s in stations:
        priority = _match_priority_station(s, priority_stations)
        if priority:
            canonical = priority["city"]
            if canonical not in lidoire_cities:
                lidoire_cities.append(canonical)
            for legacy in _legacy_city_tags(canonical, s.get("city")):
                if legacy not in lidoire_aliases.setdefault(canonical, []):
                    lidoire_aliases[canonical].append(legacy)

    # Historical cumulatives (InfluxDB), coverage and Open-Meteo fallback
    historical_rainfall: dict = {}
    coverage: dict[str, int] = {}
    open_meteo_fallback: Optional[dict] = None
    if lidoire_cities:
        historical_rainfall, coverage, open_meteo_fallback = await _get_lidoire_history(
            lidoire_cities, lidoire_aliases
        )

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
            # Config spelling, used as the InfluxDB city tag
            station_data["config_city"] = priority["city"]
            # Add historical data from InfluxDB
            hist = historical_rainfall.get(priority["city"], {})
            rain_72h = hist.get("rain_72h_mm")
            rain_7d = hist.get("rain_7d_mm")

            # Use Open-Meteo fallback when InfluxDB data is incomplete.
            # Detection: number of consecutive 24h chunks with samples
            # (consecutive_daily_coverage), not value equality.
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
                city_coverage = coverage.get(priority["city"], 0)
                incomplete_72h = rain_72h is None or city_coverage < 3
                incomplete_7d = rain_7d is None or city_coverage < 7
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

    result = {
        "available": True,
        "timestamp": _utcnow(),
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
    _latest.set("basin", result)
    return result


def peek_basin_rainfall() -> dict:
    """Last available Netatmo basin rainfall, never calling the API.

    Used by request paths (realtime inputs, /api/realtime/*): the data is
    fetched by the scheduler job. Unavailable (same shape as a failed fetch)
    when no result younger than LATEST_MAX_AGE_S exists.
    """
    cached = _latest.get("basin")
    if cached is not None:
        return cached
    return {
        "available": False,
        "stations_count": 0,
        "rain_1h_avg_mm": 0.0,
        "rain_24h_avg_mm": 0.0,
        "auth_url": "/api/auth/netatmo",
    }


def hourly_slot(
    now: datetime,
    last_written: Optional[datetime],
    max_lag: timedelta = HOURLY_WRITE_MAX_LAG,
) -> Optional[datetime]:
    """Clock-hour label to write the hourly rain value at, or None to skip.

    The label is ``now`` floored to the hour (T) and the value written there is
    Netatmo's sliding ``rain_60min`` read shortly after T, i.e. rain over
    ~[T-1h, T) — the same "label = end of the hour" convention as the Open-Meteo
    series. A slot is written once (first collection within ``max_lag`` after
    the hour); later collections of the same hour are skipped.
    """
    slot = now.replace(minute=0, second=0, microsecond=0)
    if last_written is not None and last_written >= slot:
        return None
    if now - slot > max_lag:
        return None
    return slot


def _station_rain_points(
    source: str,
    city: Optional[str],
    rain_1h_mm: Optional[float],
    rain_24h_mm: Optional[float],
    now: datetime,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
) -> tuple[list, Optional[tuple[tuple[str, str], datetime]]]:
    """InfluxDB points for one Netatmo station (or the basin average).

    Rain over-count fix: Netatmo's rain_60min is a *sliding* 60 min total.
    Writing it every 15 min as precipitation_mm_h made every hour counted ~4x
    by downstream ``sum`` aggregations. Now:

    - ``precipitation_mm_h`` (the field every rain sum uses) is written at most
      once per clock hour, aligned on the hour (see ``hourly_slot``), so the
      series has non-overlapping 1h intervals and sums are correct;
    - every collection still writes a snapshot point at ``now`` WITHOUT
      precipitation_mm_h: ``precipitation_mm_24h`` (sampled at 24h boundaries
      for 72h/7d cumulatives) and ``rain_60min_mm`` (latest sliding value, for
      display only — never sum it).

    Returns the points and the ``(series key, slot)`` to mark as written once
    the points are stored (None when no hourly point is due).
    """
    from app.services import influx_client

    tags = {"source": source}
    if city:
        tags["city"] = str(city)

    fields: dict = {
        "precipitation_mm_24h": float(rain_24h_mm or 0),
        "rain_60min_mm": float(rain_1h_mm or 0),
    }
    if lat is not None:
        fields["lat"] = float(lat)
    if lon is not None:
        fields["lon"] = float(lon)
    points = [influx_client.make_point("rainfall", tags, fields, now)]

    key = (source, str(city))
    slot = hourly_slot(now, _last_hourly_write.get(key))
    pending = None
    if slot is not None:
        points.append(influx_client.make_rainfall_point(
            source=source,
            precipitation_mm_h=rain_1h_mm or 0,
            timestamp=slot,
            station_city=city,
            lat=lat,
            lon=lon,
        ))
        pending = (key, slot)

    return points, pending


async def store_rainfall_to_influx(basin_data: dict) -> int:
    """Store Netatmo rainfall data to InfluxDB in ONE batched write.

    See _station_rain_points for the point layout. ``netatmo-lidoire`` city
    tags use the config spelling (canonical_city).
    """
    from app.services import influx_client

    now = _utcnow()
    batches = [
        # Basin average
        _station_rain_points(
            source="netatmo",
            city="basin_average",
            rain_1h_mm=basin_data.get("rain_1h_avg_mm", 0),
            rain_24h_mm=basin_data.get("rain_24h_avg_mm", 0),
            now=now,
        ),
    ]

    # Lidoire stations
    for station in basin_data.get("lidoire_stations", []):
        batches.append(_station_rain_points(
            source="netatmo-lidoire",
            city=station.get("config_city") or canonical_city(station.get("city")),
            rain_1h_mm=station.get("rain_1h_mm"),
            rain_24h_mm=station.get("rain_24h_mm"),
            now=now,
            lat=station.get("lat"),
            lon=station.get("lon"),
        ))

    # Other nearby stations (top 5)
    for station in basin_data.get("stations", [])[:5]:
        batches.append(_station_rain_points(
            source="netatmo",
            city=station.get("city"),
            rain_1h_mm=station.get("rain_1h_mm"),
            rain_24h_mm=station.get("rain_24h_mm"),
            now=now,
            lat=station.get("lat"),
            lon=station.get("lon"),
        ))

    points = [p for station_points, _ in batches for p in station_points]
    stored = await influx_client.write_points_batch(points)
    if points and stored == len(points):
        for _, pending in batches:
            if pending is not None:
                key, slot = pending
                _last_hourly_write[key] = slot

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
                    "config_city": priority["city"],
                    "lat": s.get("lat"),
                    "lon": s.get("lon"),
                }

    logger.info("Netatmo: discovered %d Lidoire stations with device IDs: %s",
                len(_lidoire_device_cache), list(_lidoire_device_cache.keys()))
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
                logger.info(f"Netatmo getmeasure: station {device_id} not found or not public")
                return []

            response.raise_for_status()
            data = response.json()

            body = data.get("body", {})
            results = []

            for ts_str, values in body.items():
                ts = _utc_from_epoch(int(ts_str))
                rain_mm = values[0] if isinstance(values, list) and len(values) > 0 else 0.0
                results.append({
                    "timestamp": ts,
                    "rain_mm": rain_mm or 0.0,
                })

            results.sort(key=lambda x: x["timestamp"])
            return results

    except httpx.HTTPStatusError as e:
        logger.info(f"Netatmo getmeasure HTTP error: {e.response.status_code} for {device_id}")
        return []
    except Exception as e:
        logger.info(f"Netatmo getmeasure error for {device_id}: {e}")
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
        logger.info("Netatmo historical: no Lidoire station device IDs discovered")
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

        logger.info(f"Netatmo historical: fetching {city} ({device_id}/{module_id}) for {start} -> {end}")

        points = await fetch_netatmo_station_history(
            device_id=device_id,
            module_id=module_id,
            start=start,
            end=end,
        )

        if not points:
            logger.info(f"  No historical data for {city}")
            continue

        # Store to InfluxDB under the config spelling (same tag as live writes)
        tag_city = info.get("config_city") or canonical_city(city)
        stored = await influx_client.write_points_batch([
            influx_client.make_rainfall_point(
                source="netatmo-lidoire",
                precipitation_mm_h=p["rain_mm"],
                timestamp=p["timestamp"],
                station_city=tag_city,
                lat=info.get("lat"),
                lon=info.get("lon"),
            )
            for p in points
        ])

        logger.info(f"  Stored {stored}/{len(points)} points for {city}")
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
            timestamp=basin_data.get("timestamp", _utcnow()),
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
                timestamp=basin_data.get("timestamp", _utcnow()),
                source=f"netatmo-{station.get('city', 'unknown')}",
                precipitation_mm_h=station.get("rain_1h_mm") or 0,
                precipitation_mm_24h=station.get("rain_24h_mm") or 0,
                basin_avg=False,
            )
        )

    return observations
