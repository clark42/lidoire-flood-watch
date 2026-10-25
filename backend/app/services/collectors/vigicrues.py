"""Vigicrues data collector.

Fetches river level data from the French Vigicrues service.
https://www.vigicrues.gouv.fr/

Endpoint notes (verified 2026-10):
- ``services/observations.json`` answers with a 302 to
  ``services/observations.json/index.php``; we call the final URL directly and
  still enable ``follow_redirects`` in case it moves again.
- ``ResObsHydro`` is already expressed in metres (e.g. 1.24 for Libourne),
  unlike Hub'Eau which returns millimetres.
- ``services/previsions.json/index.php`` returns ``{"Simul": {"Prevs": [...]}}``
  with epoch-millisecond ``DtPrev`` and min/mean/max levels in metres.

No mock data is ever returned: when Vigicrues cannot be reached the collector
returns an explicit "unavailable" result (see ``get_current_river_level``).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app.config import get_settings
from app.models.schemas import RiverLevel, RiverLevelForecast, Tendency

logger = logging.getLogger(__name__)

# Vigicrues API base URL
VIGICRUES_API_BASE = "https://www.vigicrues.gouv.fr/services"
VIGICRUES_OBSERVATIONS_URL = f"{VIGICRUES_API_BASE}/observations.json/index.php"
VIGICRUES_FORECAST_URL = f"{VIGICRUES_API_BASE}/previsions.json/index.php"

# Source labels exposed on RiverLevelForecast.source
SOURCE_LIVE = "vigicrues"
SOURCE_STALE = "vigicrues-stale"
SOURCE_UNAVAILABLE = "unavailable"

CACHE_DURATION = timedelta(minutes=15)
# How long a previous successful fetch may be served when Vigicrues is down
STALE_MAX_AGE = timedelta(hours=6)

# Cache for river data
_river_cache: dict = {}
_cache_expiry: Optional[datetime] = None


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _to_naive_utc(dt: datetime) -> datetime:
    """Convert a datetime to naive UTC (tz-aware values are converted, not stripped)."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _station_info(station_id: str) -> dict:
    """Return display name and river for a Vigicrues station code."""
    settings = get_settings()
    for station in settings.vigicrues_stations:
        if station.get("id") == station_id:
            return {"name": station.get("name", "Unknown"), "river": station.get("river", "Unknown")}
    return {"name": "Unknown", "river": "Unknown"}


def parse_observations(data: dict, station_id: str) -> list[RiverLevel]:
    """Parse a Vigicrues observations.json payload into RiverLevel (metres, naive UTC).

    Returned levels are sorted by timestamp. Invalid entries are skipped.
    """
    info = _station_info(station_id)
    levels: list[RiverLevel] = []

    observations = (data or {}).get("Serie", {}).get("ObssHydro", []) or []
    for obs in observations:
        try:
            value = obs["ResObsHydro"]
            if value is None:
                continue
            ts = datetime.fromisoformat(str(obs["DtObsHydro"]).replace("Z", "+00:00"))
            levels.append(
                RiverLevel(
                    timestamp=_to_naive_utc(ts),
                    station_id=station_id,
                    station_name=info["name"],
                    river=info["river"],
                    height_m=float(value),  # already in metres
                    flow_m3s=None,
                )
            )
        except (KeyError, ValueError, TypeError) as e:
            logger.debug("Vigicrues: skipping invalid observation %r: %s", obs, e)
            continue

    levels.sort(key=lambda lv: lv.timestamp)
    return levels


def parse_forecast(data: dict) -> list[dict]:
    """Parse a Vigicrues previsions.json payload.

    Returns a list of ``{"timestamp": ISO8601 UTC, "min_m", "mean_m", "max_m"}``
    sorted by time (empty when Vigicrues publishes no forecast for the station).
    """
    prevs = (data or {}).get("Simul", {}).get("Prevs", []) or []
    result: list[dict] = []
    for prev in prevs:
        try:
            raw_ts = prev["DtPrev"]
            if isinstance(raw_ts, (int, float)):
                ts = datetime.fromtimestamp(raw_ts / 1000, tz=timezone.utc)
            else:
                ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
            result.append({
                "timestamp": _to_naive_utc(ts).isoformat() + "Z",
                "min_m": prev.get("ResMinPrev"),
                "mean_m": prev.get("ResMoyPrev"),
                "max_m": prev.get("ResMaxPrev"),
            })
        except (KeyError, ValueError, TypeError) as e:
            logger.debug("Vigicrues: skipping invalid forecast %r: %s", prev, e)
            continue
    result.sort(key=lambda p: p["timestamp"])
    return result


async def fetch_station_data(station_id: str) -> list[RiverLevel]:
    """
    Fetch river level data from Vigicrues for a specific station.

    Station IDs use the 10-character Hydro code shared with Hub'Eau
    (e.g. P577001001 for Libourne). Returns [] on any error.
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(
                VIGICRUES_OBSERVATIONS_URL,
                params={
                    "CdStationHydro": station_id,
                    "GrdSerie": "H",  # Hauteur (height)
                    "FormatDate": "iso",
                },
                timeout=30.0,
            )
            response.raise_for_status()
            return parse_observations(response.json(), station_id)

    except Exception as e:
        logger.warning("Error fetching Vigicrues data for station %s: %s", station_id, e)
        return []


async def fetch_station_forecast(station_id: str) -> list[dict]:
    """Fetch forecast levels from Vigicrues (see parse_forecast). Returns [] on error."""
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(
                VIGICRUES_FORECAST_URL,
                params={"CdStationHydro": station_id},
                timeout=30.0,
            )
            response.raise_for_status()
            return parse_forecast(response.json())

    except Exception as e:
        logger.warning("Error fetching Vigicrues forecast for station %s: %s", station_id, e)
        return []


def _calculate_tendency(levels: list[RiverLevel], window_hours: float = 1.0) -> Tendency:
    """Tendency from the level change over the last ``window_hours`` of data."""
    if len(levels) < 2:
        return Tendency.STABLE

    last = levels[-1]
    cutoff = last.timestamp - timedelta(hours=window_hours)
    recent = [lv for lv in levels if lv.timestamp >= cutoff]
    if len(recent) < 2:
        return Tendency.STABLE

    diff = recent[-1].height_m - recent[0].height_m

    if diff > 0.05:
        return Tendency.RISING
    elif diff < -0.05:
        return Tendency.FALLING
    else:
        return Tendency.STABLE


def _unavailable_forecast(station_id: str, now: datetime) -> RiverLevelForecast:
    """Placeholder returned when no real Vigicrues data is available.

    ``current.height_m`` is 0.0 and MUST NOT be interpreted as a measurement:
    callers have to check ``available`` (False) / ``source`` ("unavailable").
    """
    info = _station_info(station_id)
    return RiverLevelForecast(
        current=RiverLevel(
            timestamp=now,
            station_id=station_id,
            station_name=info["name"],
            river=info["river"],
            height_m=0.0,
            tendency=Tendency.STABLE,
        ),
        tendency=Tendency.STABLE,
        forecast_scenarios=[],
        available=False,
        source=SOURCE_UNAVAILABLE,
    )


async def get_current_river_level() -> RiverLevelForecast:
    """Get current river level (Libourne) with Vigicrues forecast.

    Contract for callers:
    - ``available=True, source="vigicrues"``: fresh data (cached 15 min).
    - ``available=True, source="vigicrues-stale"``: Vigicrues is down, the last
      successful fetch (< 6 h old) is served; ``current.timestamp`` tells its age.
    - ``available=False, source="unavailable"``: no real data at all;
      ``current.height_m`` is a 0.0 placeholder and must be ignored.
    """
    global _river_cache, _cache_expiry

    settings = get_settings()
    station_id = settings.vigicrues_station_libourne
    now = _utcnow()

    source = SOURCE_LIVE
    if _cache_expiry and _cache_expiry > now and _river_cache.get("levels"):
        levels = _river_cache["levels"]
        scenarios = _river_cache.get("scenarios", [])
    else:
        levels = await fetch_station_data(station_id)
        if levels:
            scenarios = await fetch_station_forecast(station_id)
            _river_cache = {"levels": levels, "scenarios": scenarios, "fetched_at": now}
            _cache_expiry = now + CACHE_DURATION
        else:
            fetched_at = _river_cache.get("fetched_at")
            if _river_cache.get("levels") and fetched_at and now - fetched_at <= STALE_MAX_AGE:
                logger.warning("Vigicrues unavailable, serving stale data from %s", fetched_at)
                levels = _river_cache["levels"]
                scenarios = _river_cache.get("scenarios", [])
                source = SOURCE_STALE
            else:
                logger.warning("Vigicrues unavailable and no recent data: river level unavailable")
                return _unavailable_forecast(station_id, now)

    tendency = _calculate_tendency(levels)
    current = levels[-1].model_copy(update={"tendency": tendency})

    return RiverLevelForecast(
        current=current,
        tendency=tendency,
        forecast_scenarios=scenarios,
        available=True,
        source=source,
    )


async def get_historical_levels(
    station_id: str,
    start: datetime,
    end: datetime,
) -> list[RiverLevel]:
    """Get river levels for a time range from Vigicrues.

    Vigicrues only serves about the last month of observations: older ranges
    return []. Use Hub'Eau / Hydroportail for real history.
    """
    start_n = _to_naive_utc(start)
    end_n = _to_naive_utc(end)
    levels = await fetch_station_data(station_id)
    return [lv for lv in levels if start_n <= lv.timestamp <= end_n]


async def collect_and_store_river_levels():
    """Collect river level data (storage is handled by the Hub'Eau pipeline)."""
    settings = get_settings()

    total_fetched = 0
    for station in settings.vigicrues_stations:
        levels = await fetch_station_data(station["id"])
        total_fetched += len(levels)

    return total_fetched
