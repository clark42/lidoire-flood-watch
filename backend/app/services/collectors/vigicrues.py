"""Vigicrues data collector.

Fetches river level data from the French Vigicrues service.
https://www.vigicrues.gouv.fr/
"""

import httpx
from datetime import datetime, timedelta
from typing import Optional

from app.config import get_settings
from app.models.schemas import RiverLevel, RiverLevelForecast, Tendency

# Vigicrues API base URL
VIGICRUES_API_BASE = "https://www.vigicrues.gouv.fr/services"

# Cache for river data
_river_cache: dict = {}
_cache_expiry: Optional[datetime] = None


async def fetch_station_data(station_id: str) -> list[RiverLevel]:
    """
    Fetch river level data from Vigicrues for a specific station.

    Station IDs for the area:
    - O9720010: Libourne (Dordogne)
    - O9700010: Bergerac (Dordogne)
    """
    # Vigicrues observations endpoint
    url = f"{VIGICRUES_API_BASE}/observations.json"

    settings = get_settings()
    station_info = {
        "O9720010": {"name": "Libourne", "river": "Dordogne"},
        "O9700010": {"name": "Bergerac", "river": "Dordogne"},
    }

    info = station_info.get(station_id, {"name": "Unknown", "river": "Unknown"})

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                params={
                    "CdStationHydro": station_id,
                    "GrdSerie": "H",  # Hauteur (height)
                    "FormatDate": "iso",
                },
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()

            levels = []
            observations = data.get("Serie", {}).get("ObssHydro", [])

            for obs in observations:
                try:
                    levels.append(
                        RiverLevel(
                            timestamp=datetime.fromisoformat(obs["DtObsHydro"].replace("Z", "+00:00")),
                            station_id=station_id,
                            station_name=info["name"],
                            river=info["river"],
                            height_m=float(obs["ResObsHydro"]) / 1000,  # Convert mm to m
                            flow_m3s=None,  # Would need separate query
                        )
                    )
                except (KeyError, ValueError) as e:
                    print(f"Error parsing observation: {e}")
                    continue

            return levels

    except Exception as e:
        print(f"Error fetching Vigicrues data for station {station_id}: {e}")
        return []


async def fetch_station_forecast(station_id: str) -> list[dict]:
    """Fetch forecast scenarios from Vigicrues."""
    url = f"{VIGICRUES_API_BASE}/previsions.json"

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                params={
                    "CdStationHydro": station_id,
                },
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()

            scenarios = data.get("Scenarios", [])
            return scenarios

    except Exception as e:
        print(f"Error fetching Vigicrues forecast for station {station_id}: {e}")
        return []


def _generate_mock_river_data(station_id: str) -> list[RiverLevel]:
    """Generate mock river level data for development."""
    now = datetime.utcnow()
    levels = []

    station_info = {
        "O9720010": {"name": "Libourne", "river": "Dordogne"},
    }
    info = station_info.get(station_id, {"name": "Unknown", "river": "Unknown"})

    # Generate hourly data for the last 48 hours
    base_level = 2.5  # meters

    for i in range(48):
        timestamp = now - timedelta(hours=48 - i)
        # Add some variation based on time (simulating tidal influence)
        hour_factor = 0.3 * (1 + 0.5 * ((i % 12) - 6) / 6)
        level = base_level + hour_factor + 0.1 * (i % 5 - 2)

        levels.append(
            RiverLevel(
                timestamp=timestamp,
                station_id=station_id,
                station_name=info["name"],
                river=info["river"],
                height_m=round(level, 2),
                flow_m3s=None,
            )
        )

    return levels


def _calculate_tendency(levels: list[RiverLevel]) -> Tendency:
    """Calculate the tendency from recent measurements."""
    if len(levels) < 3:
        return Tendency.STABLE

    recent = levels[-3:]
    heights = [l.height_m for l in recent]

    diff = heights[-1] - heights[0]

    if diff > 0.05:
        return Tendency.RISING
    elif diff < -0.05:
        return Tendency.FALLING
    else:
        return Tendency.STABLE


async def get_current_river_level() -> RiverLevelForecast:
    """Get current river level with forecast."""
    global _river_cache, _cache_expiry

    settings = get_settings()
    station_id = settings.vigicrues_station_libourne
    now = datetime.utcnow()

    # Check cache
    if _cache_expiry and _cache_expiry > now and _river_cache:
        levels = _river_cache.get("levels", [])
    else:
        # Fetch real data
        levels = await fetch_station_data(station_id)

        if not levels:
            # Fall back to mock data
            levels = _generate_mock_river_data(station_id)

        # Update cache (15 minutes)
        _river_cache = {"levels": levels}
        _cache_expiry = now + timedelta(minutes=15)

    # Get current level (most recent)
    current = levels[-1] if levels else RiverLevel(
        timestamp=now,
        station_id=station_id,
        station_name="Libourne",
        river="Dordogne",
        height_m=2.5,
    )

    # Calculate tendency
    tendency = _calculate_tendency(levels)
    current.tendency = tendency

    # Fetch forecast scenarios
    scenarios = await fetch_station_forecast(station_id)

    return RiverLevelForecast(
        current=current,
        tendency=tendency,
        forecast_scenarios=scenarios,
    )


async def get_historical_levels(
    station_id: str,
    start: datetime,
    end: datetime,
) -> list[RiverLevel]:
    """Get historical river levels for a time range."""
    # For historical data, we would query our stored data
    # For now, return mock data
    levels = []

    current = start
    while current <= end:
        base_level = 2.5 + 0.5 * ((current.hour % 12) - 6) / 6
        levels.append(
            RiverLevel(
                timestamp=current,
                station_id=station_id,
                station_name="Libourne",
                river="Dordogne",
                height_m=base_level,
            )
        )
        current += timedelta(hours=1)

    return levels


async def collect_and_store_river_levels():
    """Collect river level data and store in database."""
    settings = get_settings()

    total_stored = 0
    for station in settings.vigicrues_stations:
        levels = await fetch_station_data(station["id"])
        if levels:
            # Store in InfluxDB
            # TODO: Implement storage
            total_stored += len(levels)

    return total_stored
