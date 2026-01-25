"""Data collectors for external APIs."""

from app.services.collectors.hubeau import (
    fetch_realtime_observations,
    fetch_historical_data,
    fetch_all_stations_realtime,
    get_multi_station_data,
    fetch_episode_context,
)
from app.services.collectors.netatmo import (
    fetch_public_stations_data,
    get_basin_rainfall,
    get_rainfall_observations,
)

__all__ = [
    # Hub'Eau
    "fetch_realtime_observations",
    "fetch_historical_data",
    "fetch_all_stations_realtime",
    "get_multi_station_data",
    "fetch_episode_context",
    # Netatmo
    "fetch_public_stations_data",
    "get_basin_rainfall",
    "get_rainfall_observations",
]
