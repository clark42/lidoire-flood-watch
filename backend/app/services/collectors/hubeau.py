"""Hub'Eau hydrometry data collector.

Fetches river level data from the French Hub'Eau API.
https://hubeau.eaufrance.fr/page/api-hydrometrie

Supports:
- Real-time observations (last 30 days) via Hub'Eau observations_tr
- Historical sub-hourly data (unlimited) via Hydroportail AJAX API
- Historical elaborated data (daily/monthly aggregates) via Hub'Eau obs_elab
- Multiple stations across Dordogne, Isle, and Dronne rivers

Data source priority for historical periods (> 30 days):
1. Hydroportail (hydro.eaufrance.fr) — sub-hourly, full history
2. Hub'Eau obs_elab HIXnJ — daily max, full history
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app.config import get_settings
from app.models.schemas import RiverLevel, Tendency, MultiStationData, StationLevel
# Single (pure) implementation of the regression rate, shared with the risk
# model backfill/replay/forecast. Re-exported here for existing callers.
from app.services.risk_core import rate_cm_per_hour, to_utc_naive

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Hub'Eau API base URLs
HUBEAU_API_BASE = "https://hubeau.eaufrance.fr/api/v2/hydrometrie"

# Hydroportail AJAX API (sub-hourly historical data, no time limit)
HYDROPORTAIL_BASE = "https://www.hydro.eaufrance.fr/stationhydro/ajax"

# Cache for river data: hours_back -> (expiry, {station_id: levels})
_stations_cache_by_hours: dict[int, tuple[datetime, dict[str, list[RiverLevel]]]] = {}
_stations_lock = asyncio.Lock()
CACHE_DURATION_MINUTES = 5
# Bounded concurrency towards Hub'Eau
MAX_CONCURRENT_REQUESTS = 4

# Hub'Eau obs_elab height grandeurs. Hub'Eau has no daily *minimum height*
# (QINnJ is a minimum *discharge*), so no "daily_min" granularity is offered.
HEIGHT_GRANDEURS = {
    "daily_max": "HIXnJ",   # Hauteur Instantanée maXimale n-Jours (daily max)
    "monthly_max": "HIXM",  # Hauteur Instantanée maXimale Mensuelle
}


def parse_observations_tr(data: dict, station_id: str, station_config: Optional[dict] = None) -> list[RiverLevel]:
    """Parse a Hub'Eau observations_tr payload (H in millimetres) into RiverLevel (metres).

    Timestamps are kept tz-aware (UTC) as returned by Hub'Eau. Sorted by time.
    """
    station_config = station_config or _get_station_config(station_id)
    levels = []
    for obs in (data or {}).get("data", []) or []:
        try:
            # Hub'Eau returns height in mm, convert to m
            height_mm = obs.get("resultat_obs")
            if height_mm is None:
                continue

            timestamp_str = obs.get("date_obs")
            if not timestamp_str:
                continue

            levels.append(
                RiverLevel(
                    timestamp=datetime.fromisoformat(timestamp_str.replace("Z", "+00:00")),
                    station_id=station_id,
                    station_name=station_config.get("name", "Unknown"),
                    river=station_config.get("river", "Unknown"),
                    height_m=float(height_mm) / 1000,
                    flow_m3s=None,
                )
            )
        except (KeyError, ValueError, TypeError) as e:
            logger.debug("Error parsing Hub'Eau observation: %s", e)
            continue

    return sorted(levels, key=lambda x: x.timestamp)


async def fetch_realtime_observations(
    station_id: str,
    hours_back: int = 48,
    client: Optional[httpx.AsyncClient] = None,
) -> list[RiverLevel]:
    """
    Fetch real-time observations from Hub'Eau for a specific station.

    Args:
        station_id: Hub'Eau station code (e.g., P577001001)
        hours_back: Number of hours of history to fetch (max ~720 = 30 days)
        client: Optional shared httpx client (a temporary one is created otherwise)

    Returns:
        List of RiverLevel observations sorted by timestamp
    """
    settings = get_settings()
    station_config = _get_station_config(station_id)

    url = f"{HUBEAU_API_BASE}/observations_tr"

    # Calculate date range
    end_date = _utcnow()
    start_date = end_date - timedelta(hours=hours_back)

    params = {
        "code_entite": station_id,
        "grandeur_hydro": "H",  # Height
        "date_debut_obs": start_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date_fin_obs": end_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "size": 10000,  # ~35 days at 5-min intervals
        "sort": "asc",
    }

    try:
        if client is not None:
            response = await client.get(url, params=params, timeout=30.0)
        else:
            async with httpx.AsyncClient() as own_client:
                response = await own_client.get(url, params=params, timeout=30.0)
        response.raise_for_status()
        return parse_observations_tr(response.json(), station_id, station_config)

    except httpx.HTTPStatusError as e:
        logger.warning(f"Hub'Eau API error for station {station_id}: {e.response.status_code}")
        return []
    except Exception as e:
        logger.warning(f"Error fetching Hub'Eau data for station {station_id}: {e}")
        return []


async def fetch_historical_data(
    station_id: str,
    start_date: datetime,
    end_date: datetime,
    granularity: str = "daily_max",
) -> list[RiverLevel]:
    """
    Fetch historical elaborated data from Hub'Eau.

    Args:
        station_id: Hub'Eau station code
        start_date: Start of the period
        end_date: End of the period
        granularity: Type of data (heights only):
            - "daily_max": Maximum daily height (HIXnJ)
            - "monthly_max": Maximum monthly height (HIXM)
            Any other value (e.g. the former "daily_min", which was mapped to
            QINnJ, a *discharge*) is rejected and returns [].

    Returns:
        List of RiverLevel observations
    """
    station_config = _get_station_config(station_id)

    url = f"{HUBEAU_API_BASE}/obs_elab"

    # Map granularity to Hub'Eau parameter
    # Valid values: QmnJ, QmM, HIXnJ, HIXM, QIXnJ, QIXM, QINnJ, QINM
    # Only H* grandeurs are heights; Q* are discharges (l/s) and must not be
    # parsed as height_m.
    grandeur = HEIGHT_GRANDEURS.get(granularity)
    if grandeur is None:
        logger.warning("Hub'Eau: unsupported height granularity %r (use %s)",
                       granularity, list(HEIGHT_GRANDEURS))
        return []

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                params={
                    "code_entite": station_id,
                    "grandeur_hydro_elab": grandeur,
                    "date_debut_obs_elab": start_date.strftime("%Y-%m-%d"),
                    "date_fin_obs_elab": end_date.strftime("%Y-%m-%d"),
                    "size": 10000,
                    "sort": "asc",
                },
                timeout=60.0,
            )
            response.raise_for_status()
            data = response.json()

            levels = []
            for obs in data.get("data", []):
                try:
                    height_mm = obs.get("resultat_obs_elab")
                    if height_mm is None:
                        continue

                    date_str = obs.get("date_obs_elab")
                    if not date_str:
                        continue

                    # Parse date (format: YYYY-MM-DD)
                    timestamp = datetime.strptime(date_str, "%Y-%m-%d")

                    levels.append(
                        RiverLevel(
                            timestamp=timestamp,
                            station_id=station_id,
                            station_name=station_config.get("name", "Unknown"),
                            river=station_config.get("river", "Unknown"),
                            height_m=float(height_mm) / 1000,
                            flow_m3s=None,
                        )
                    )
                except (KeyError, ValueError, TypeError) as e:
                    logger.warning(f"Error parsing Hub'Eau historical data: {e}")
                    continue

            return sorted(levels, key=lambda x: x.timestamp)

    except httpx.HTTPStatusError as e:
        logger.warning(f"Hub'Eau API error for historical data {station_id}: {e.response.status_code}")
        return []
    except Exception as e:
        logger.warning(f"Error fetching Hub'Eau historical data for station {station_id}: {e}")
        return []


async def fetch_hydroportail_data(
    station_id: str,
    start_date: datetime,
    end_date: datetime,
) -> list[RiverLevel]:
    """
    Fetch sub-hourly historical data from the Hydroportail (hydro.eaufrance.fr).

    The Hydroportail serves the full Banque Hydro national database with no time
    limit, unlike Hub'Eau observations_tr which only keeps 30 days.

    Returns data at ~5-15 min intervals (same granularity as realtime).

    Args:
        station_id: Hub'Eau station code (e.g., P577001001)
        start_date: Start of the period
        end_date: End of the period

    Returns:
        List of RiverLevel observations sorted by timestamp
    """
    station_config = _get_station_config(station_id)

    url = f"{HYDROPORTAIL_BASE}/{station_id}/series"

    # Hydroportail expects DD/MM/YYYY date format
    params = {
        "hydro_series[startAt]": start_date.strftime("%d/%m/%Y"),
        "hydro_series[endAt]": end_date.strftime("%d/%m/%Y"),
        "hydro_series[variableType]": "simple_and_interpolated_and_hourly_variable",
        "hydro_series[simpleAndInterpolatedAndHourlyVariable]": "H",
        "hydro_series[statusData]": "most_valid",
    }

    # Hydroportail requires browser-like headers to avoid 403
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://www.hydro.eaufrance.fr/stationhydro/{station_id}/series",
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, headers=headers, timeout=60.0)
            response.raise_for_status()
            data = response.json()

            series_data = data.get("series", {}).get("data", [])
            if not series_data:
                return []

            levels = []
            for obs in series_data:
                try:
                    height_mm = obs.get("v")
                    if height_mm is None:
                        continue

                    timestamp_str = obs.get("t")
                    if not timestamp_str:
                        continue

                    levels.append(
                        RiverLevel(
                            timestamp=datetime.fromisoformat(
                                timestamp_str.replace("Z", "+00:00")
                            ),
                            station_id=station_id,
                            station_name=station_config.get("name", "Unknown"),
                            river=station_config.get("river", "Unknown"),
                            height_m=float(height_mm) / 1000,
                            flow_m3s=None,
                        )
                    )
                except (KeyError, ValueError, TypeError) as e:
                    logger.warning(f"Error parsing Hydroportail observation: {e}")
                    continue

            logger.info(f"  Hydroportail: {len(levels)} sub-hourly points for {station_config.get('name', station_id)}")
            return sorted(levels, key=lambda x: x.timestamp)

    except httpx.HTTPStatusError as e:
        logger.warning(f"Hydroportail API error for {station_id}: {e.response.status_code}")
        return []
    except Exception as e:
        logger.warning(f"Error fetching Hydroportail data for {station_id}: {e}")
        return []


async def fetch_all_stations_realtime(hours_back: int = 48) -> dict[str, list[RiverLevel]]:
    """
    Fetch real-time data for all configured stations.

    Stations are fetched concurrently (at most MAX_CONCURRENT_REQUESTS at a
    time) over a single HTTP client. Results are cached per ``hours_back`` for
    CACHE_DURATION_MINUTES; a lock prevents concurrent callers from issuing
    duplicate requests.

    Returns:
        Dictionary mapping station_id to list of RiverLevel
    """
    now = _utcnow()
    cached = _stations_cache_by_hours.get(hours_back)
    if cached and cached[0] > now:
        return cached[1]

    async with _stations_lock:
        now = _utcnow()
        cached = _stations_cache_by_hours.get(hours_back)
        if cached and cached[0] > now:
            return cached[1]

        settings = get_settings()
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

        async with httpx.AsyncClient() as client:
            async def _one(station_id: str) -> tuple[str, list[RiverLevel]]:
                async with semaphore:
                    return station_id, await fetch_realtime_observations(
                        station_id, hours_back, client=client
                    )

            fetched = await asyncio.gather(
                *[_one(station["id"]) for station in settings.hubeau_stations]
            )

        results = {station_id: levels for station_id, levels in fetched if levels}

        # Do not cache a total failure: retry on next call
        if results:
            _stations_cache_by_hours[hours_back] = (
                now + timedelta(minutes=CACHE_DURATION_MINUTES), results
            )

        return results


async def get_multi_station_data() -> MultiStationData:
    """
    Get current data for all stations with tendency calculation.

    Returns:
        MultiStationData with all stations' current levels and tendencies
    """
    settings = get_settings()
    all_data = await fetch_all_stations_realtime(hours_back=24)
    now = _utcnow()

    stations = []
    for station_config in settings.hubeau_stations:
        station_id = station_config["id"]
        levels = all_data.get(station_id, [])

        if not levels:
            continue

        current = levels[-1]
        tendency = _calculate_tendency(levels, t=now)
        rate = _calculate_rate_of_change(levels, t=now)

        # Get thresholds for this station
        thresholds = station_config.get("thresholds", {})

        stations.append(
            StationLevel(
                station_id=station_id,
                station_name=station_config["name"],
                river=station_config["river"],
                current_level=current,
                tendency=tendency,
                rate_cm_per_hour=rate,
                distance_km=station_config.get("distance_km", 0),
                delay_hours=station_config.get("delay_hours", 0),
                weight=station_config.get("weight", 0.1),
                threshold_yellow=thresholds.get("yellow", 3.0),
                threshold_orange=thresholds.get("orange", 4.0),
                threshold_red=thresholds.get("red", 5.0),
            )
        )

    # Sort by distance (closest first)
    stations.sort(key=lambda s: s.distance_km)

    return MultiStationData(
        timestamp=_utcnow(),
        stations=stations,
        primary_station_id=settings.hubeau_primary_station,
    )


async def fetch_episode_context(
    episode_date: datetime,
    hours_before: int = 72,
    hours_after: int = 24,
) -> dict[str, list[RiverLevel]]:
    """
    Fetch historical data for all stations around a flood episode date.

    This is used to analyze past flood events and calibrate the risk model.
    Stations are fetched concurrently (at most MAX_CONCURRENT_REQUESTS at a
    time); the result keeps the configured station order.

    Args:
        episode_date: Date/time of the flood event (peak or start)
        hours_before: Hours of data before the event
        hours_after: Hours of data after the event

    Returns:
        Dictionary mapping station_id to list of RiverLevel
    """
    settings = get_settings()

    start_date = episode_date - timedelta(hours=hours_before)
    end_date = episode_date + timedelta(hours=hours_after)
    start_naive = to_utc_naive(start_date)
    end_naive = to_utc_naive(end_date)

    # Real-time API only keeps ~30 days
    now = _utcnow()
    use_realtime = start_naive > now - timedelta(days=30)
    hours_back = int((now - start_naive).total_seconds() / 3600)

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async def _one(station_id: str) -> tuple[str, list[RiverLevel]]:
        async with semaphore:
            if use_realtime:
                levels = await fetch_realtime_observations(station_id, hours_back=hours_back)
                return station_id, [
                    lv for lv in levels
                    if start_naive <= to_utc_naive(lv.timestamp) <= end_naive
                ]
            # Hydroportail for sub-hourly historical data, then Hub'Eau
            # obs_elab (daily max) if Hydroportail fails
            levels = await fetch_hydroportail_data(station_id, start_date, end_date)
            if not levels:
                levels = await fetch_historical_data(
                    station_id, start_date, end_date, granularity="daily_max",
                )
            return station_id, levels

    fetched = await asyncio.gather(
        *[_one(station["id"]) for station in settings.hubeau_stations]
    )
    return {station_id: levels for station_id, levels in fetched if levels}


def _get_station_config(station_id: str) -> dict:
    """Get station configuration by ID."""
    settings = get_settings()
    for station in settings.hubeau_stations:
        if station["id"] == station_id:
            return station
    return {"name": "Unknown", "river": "Unknown"}


def _calculate_tendency(
    levels: list[RiverLevel],
    window_hours: float = 3,
    t: Optional[datetime] = None,
) -> Tendency:
    """
    Calculate the tendency from the regression rate over the last window.

    RISING/FALLING when the regressed change over the window exceeds 5 cm.

    Args:
        levels: List of RiverLevel sorted by timestamp
        window_hours: Number of hours to consider for tendency
        t: Evaluation instant (default: now, UTC); later points are ignored

    Returns:
        Tendency enum value
    """
    rate = rate_cm_per_hour(levels, t or _utcnow(), window_h=window_hours)
    change_cm = rate * window_hours

    # More than 5cm change is significant
    if change_cm > 5:
        return Tendency.RISING
    elif change_cm < -5:
        return Tendency.FALLING
    else:
        return Tendency.STABLE


def _calculate_rate_of_change(
    levels: list[RiverLevel],
    window_hours: float = 3,
    t: Optional[datetime] = None,
) -> float:
    """
    Calculate rate of water level change in cm/hour (see rate_cm_per_hour).

    Args:
        levels: List of RiverLevel sorted by timestamp
        window_hours: Number of hours to consider (default 3h regression)
        t: Evaluation instant (default: now, UTC); later points are ignored

    Returns:
        Rate of change in cm/hour (positive = rising), rounded to 0.1
    """
    return round(rate_cm_per_hour(levels, t or _utcnow(), window_h=window_hours), 1)


async def collect_and_store_all_stations():
    """Collect data from all stations and store in database."""
    settings = get_settings()
    total_stored = 0

    for station in settings.hubeau_stations:
        levels = await fetch_realtime_observations(station["id"], hours_back=6)
        if levels:
            # TODO: Store in InfluxDB
            total_stored += len(levels)

    return total_stored
