"""Data storage service.

Handles storage and retrieval of time-series and relational data.
Uses InfluxDB for time-series and PostgreSQL for relational data.
"""

from datetime import datetime, timedelta
from typing import Optional

from app.models.schemas import (
    FloodEpisode,
    FloodEpisodeCreate,
    TimelinePoint,
    TideData,
    RiverLevel,
    RainfallObservation,
)

# In-memory storage for development
# TODO: Replace with actual database connections
_episodes_store: list[FloodEpisode] = []
_timeline_store: list[TimelinePoint] = []
_episode_id_counter = 1


async def get_all_episodes(
    limit: int = 50,
    offset: int = 0,
    min_severity: Optional[int] = None,
) -> list[FloodEpisode]:
    """Get all recorded flood episodes."""
    episodes = _episodes_store.copy()

    if min_severity:
        episodes = [e for e in episodes if e.severity >= min_severity]

    # Sort by start_time descending
    episodes.sort(key=lambda e: e.start_time, reverse=True)

    return episodes[offset:offset + limit]


async def get_episode_by_id(episode_id: int) -> Optional[FloodEpisode]:
    """Get a specific episode by ID."""
    for episode in _episodes_store:
        if episode.id == episode_id:
            return episode
    return None


async def create_episode(episode: FloodEpisodeCreate) -> FloodEpisode:
    """Create a new flood episode."""
    global _episode_id_counter

    new_episode = FloodEpisode(
        id=_episode_id_counter,
        start_time=episode.start_time,
        peak_time=episode.peak_time,
        end_time=episode.end_time,
        severity=episode.severity,
        description=episode.description,
        notes=episode.notes,
        created_at=datetime.utcnow(),
    )

    _episodes_store.append(new_episode)
    _episode_id_counter += 1

    return new_episode


async def update_episode(episode_id: int, episode: FloodEpisodeCreate) -> FloodEpisode:
    """Update an existing episode."""
    for i, ep in enumerate(_episodes_store):
        if ep.id == episode_id:
            updated = FloodEpisode(
                id=episode_id,
                start_time=episode.start_time,
                peak_time=episode.peak_time,
                end_time=episode.end_time,
                severity=episode.severity,
                description=episode.description,
                notes=episode.notes,
                created_at=ep.created_at,
            )
            _episodes_store[i] = updated
            return updated

    raise ValueError(f"Episode {episode_id} not found")


async def delete_episode(episode_id: int) -> None:
    """Delete an episode."""
    global _episodes_store
    _episodes_store = [e for e in _episodes_store if e.id != episode_id]


async def get_episodes_in_range(start: datetime, end: datetime) -> list[FloodEpisode]:
    """Get episodes that occurred within a time range."""
    return [
        e for e in _episodes_store
        if start <= e.start_time <= end
    ]


async def get_timeline_data(
    start: datetime,
    end: datetime,
    resolution: str = "1h",
) -> list[TimelinePoint]:
    """
    Get timeline data for a time range.

    Resolution options: 15m, 1h, 6h, 1d
    """
    # Resolution to timedelta
    resolution_map = {
        "15m": timedelta(minutes=15),
        "1h": timedelta(hours=1),
        "6h": timedelta(hours=6),
        "1d": timedelta(days=1),
    }
    delta = resolution_map.get(resolution, timedelta(hours=1))

    points = []
    current = start

    # Generate mock data points
    # In production, this would query InfluxDB
    import random
    random.seed(42)  # Reproducible for demo

    base_river = 2.5
    base_tide = 4.0

    while current <= end:
        # Simulate some variation
        hour = current.hour
        day_factor = 0.3 * (1 + 0.5 * ((hour % 12) - 6) / 6)

        points.append(
            TimelinePoint(
                timestamp=current,
                tide_height_m=base_tide + day_factor + random.uniform(-0.2, 0.2),
                river_level_m=base_river + day_factor * 0.5 + random.uniform(-0.1, 0.1),
                rainfall_mm_h=max(0, random.gauss(0.5, 1.5)),
                risk_score=20 + random.uniform(-5, 15) + day_factor * 10,
                video_level=None,
            )
        )
        current += delta

    return points


async def store_tide_data(tides: list[TideData]) -> int:
    """Store tide data in time-series database."""
    # TODO: Implement InfluxDB storage
    return len(tides)


async def store_river_levels(levels: list[RiverLevel]) -> int:
    """Store river level data."""
    # TODO: Implement InfluxDB storage
    return len(levels)


async def store_rainfall(observations: list[RainfallObservation]) -> int:
    """Store rainfall observations."""
    # TODO: Implement InfluxDB storage
    return len(observations)


async def get_historical_tides(start: datetime, end: datetime) -> list[TideData]:
    """Get historical tide data."""
    # TODO: Query InfluxDB
    return []


async def get_historical_river_levels(
    station_id: str,
    start: datetime,
    end: datetime,
) -> list[RiverLevel]:
    """Get historical river level data."""
    # TODO: Query InfluxDB
    from app.services.collectors.vigicrues import get_historical_levels
    return await get_historical_levels(station_id, start, end)


async def get_historical_rainfall(start: datetime, end: datetime) -> list[RainfallObservation]:
    """Get historical rainfall data."""
    # TODO: Query InfluxDB
    from app.services.collectors.weather import get_historical_rainfall as fetch_historical
    return await fetch_historical(start, end)
