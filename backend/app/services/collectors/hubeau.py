"""Hub'Eau hydrometry data collector.

Fetches river level data from the French Hub'Eau API.
https://hubeau.eaufrance.fr/page/api-hydrometrie

Supports:
- Real-time observations (last 30 days)
- Historical elaborated data (since 1900 for some stations)
- Multiple stations across Dordogne, Isle, and Dronne rivers
"""

import httpx
from datetime import datetime, timedelta
from typing import Optional

from app.config import get_settings
from app.models.schemas import RiverLevel, Tendency, MultiStationData, StationLevel


# Hub'Eau API base URLs
HUBEAU_API_BASE = "https://hubeau.eaufrance.fr/api/v2/hydrometrie"

# Cache for river data
_stations_cache: dict[str, list[RiverLevel]] = {}
_cache_expiry: Optional[datetime] = None
CACHE_DURATION_MINUTES = 5


async def fetch_realtime_observations(
    station_id: str,
    hours_back: int = 48,
) -> list[RiverLevel]:
    """
    Fetch real-time observations from Hub'Eau for a specific station.

    Args:
        station_id: Hub'Eau station code (e.g., P577001001)
        hours_back: Number of hours of history to fetch (max ~720 = 30 days)

    Returns:
        List of RiverLevel observations sorted by timestamp
    """
    settings = get_settings()
    station_config = _get_station_config(station_id)

    url = f"{HUBEAU_API_BASE}/observations_tr"

    # Calculate date range
    end_date = datetime.utcnow()
    start_date = end_date - timedelta(hours=hours_back)

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                params={
                    "code_entite": station_id,
                    "grandeur_hydro": "H",  # Height
                    "date_debut_obs": start_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "date_fin_obs": end_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "size": 2000,
                    "sort": "asc",
                },
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()

            levels = []
            for obs in data.get("data", []):
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
                    print(f"Error parsing Hub'Eau observation: {e}")
                    continue

            return sorted(levels, key=lambda x: x.timestamp)

    except httpx.HTTPStatusError as e:
        print(f"Hub'Eau API error for station {station_id}: {e.response.status_code}")
        return []
    except Exception as e:
        print(f"Error fetching Hub'Eau data for station {station_id}: {e}")
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
        granularity: Type of data:
            - "daily_max": Maximum daily height (HXI)
            - "daily_mean": Mean daily flow (QmJ) - if available
            - "monthly_max": Maximum monthly height

    Returns:
        List of RiverLevel observations
    """
    station_config = _get_station_config(station_id)

    url = f"{HUBEAU_API_BASE}/obs_elab"

    # Map granularity to Hub'Eau parameter
    grandeur_map = {
        "daily_max": "HXI",  # Hauteur instantanée maximale journalière
        "monthly_max": "HXIM",  # Hauteur instantanée maximale mensuelle
    }
    grandeur = grandeur_map.get(granularity, "HXI")

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
                    print(f"Error parsing Hub'Eau historical data: {e}")
                    continue

            return sorted(levels, key=lambda x: x.timestamp)

    except httpx.HTTPStatusError as e:
        print(f"Hub'Eau API error for historical data {station_id}: {e.response.status_code}")
        return []
    except Exception as e:
        print(f"Error fetching Hub'Eau historical data for station {station_id}: {e}")
        return []


async def fetch_all_stations_realtime(hours_back: int = 48) -> dict[str, list[RiverLevel]]:
    """
    Fetch real-time data for all configured stations.

    Returns:
        Dictionary mapping station_id to list of RiverLevel
    """
    global _stations_cache, _cache_expiry

    now = datetime.utcnow()

    # Check cache
    if _cache_expiry and _cache_expiry > now and _stations_cache:
        return _stations_cache

    settings = get_settings()
    results = {}

    for station in settings.hubeau_stations:
        station_id = station["id"]
        levels = await fetch_realtime_observations(station_id, hours_back)
        if levels:
            results[station_id] = levels

    # Update cache
    _stations_cache = results
    _cache_expiry = now + timedelta(minutes=CACHE_DURATION_MINUTES)

    return results


async def get_multi_station_data() -> MultiStationData:
    """
    Get current data for all stations with tendency calculation.

    Returns:
        MultiStationData with all stations' current levels and tendencies
    """
    settings = get_settings()
    all_data = await fetch_all_stations_realtime(hours_back=24)

    stations = []
    for station_config in settings.hubeau_stations:
        station_id = station_config["id"]
        levels = all_data.get(station_id, [])

        if not levels:
            continue

        current = levels[-1]
        tendency = _calculate_tendency(levels)
        rate = _calculate_rate_of_change(levels)

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
        timestamp=datetime.utcnow(),
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

    results = {}

    # First try real-time data (if within last 30 days)
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)

    for station in settings.hubeau_stations:
        station_id = station["id"]

        if start_date > thirty_days_ago:
            # Use real-time API
            levels = await fetch_realtime_observations(
                station_id,
                hours_back=int((datetime.utcnow() - start_date).total_seconds() / 3600),
            )
            # Filter to our date range
            levels = [
                lv for lv in levels
                if start_date <= lv.timestamp <= end_date
            ]
        else:
            # Use historical API
            levels = await fetch_historical_data(
                station_id,
                start_date,
                end_date,
                granularity="daily_max",
            )

        if levels:
            results[station_id] = levels

    return results


def _get_station_config(station_id: str) -> dict:
    """Get station configuration by ID."""
    settings = get_settings()
    for station in settings.hubeau_stations:
        if station["id"] == station_id:
            return station
    return {"name": "Unknown", "river": "Unknown"}


def _calculate_tendency(levels: list[RiverLevel], window_hours: int = 3) -> Tendency:
    """
    Calculate the tendency from recent measurements.

    Args:
        levels: List of RiverLevel sorted by timestamp
        window_hours: Number of hours to consider for tendency

    Returns:
        Tendency enum value
    """
    if len(levels) < 2:
        return Tendency.STABLE

    now = levels[-1].timestamp
    cutoff = now - timedelta(hours=window_hours)

    recent = [lv for lv in levels if lv.timestamp >= cutoff]
    if len(recent) < 2:
        recent = levels[-5:] if len(levels) >= 5 else levels

    if len(recent) < 2:
        return Tendency.STABLE

    first_height = recent[0].height_m
    last_height = recent[-1].height_m
    diff = last_height - first_height

    # More than 5cm change is significant
    if diff > 0.05:
        return Tendency.RISING
    elif diff < -0.05:
        return Tendency.FALLING
    else:
        return Tendency.STABLE


def _calculate_rate_of_change(levels: list[RiverLevel], window_hours: int = 1) -> float:
    """
    Calculate rate of water level change in cm/hour.

    Args:
        levels: List of RiverLevel sorted by timestamp
        window_hours: Number of hours to consider

    Returns:
        Rate of change in cm/hour (positive = rising)
    """
    if len(levels) < 2:
        return 0.0

    now = levels[-1].timestamp
    cutoff = now - timedelta(hours=window_hours)

    recent = [lv for lv in levels if lv.timestamp >= cutoff]
    if len(recent) < 2:
        return 0.0

    first = recent[0]
    last = recent[-1]

    time_diff_hours = (last.timestamp - first.timestamp).total_seconds() / 3600
    if time_diff_hours < 0.1:  # Less than 6 minutes
        return 0.0

    height_diff_cm = (last.height_m - first.height_m) * 100
    return round(height_diff_cm / time_diff_hours, 1)


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
