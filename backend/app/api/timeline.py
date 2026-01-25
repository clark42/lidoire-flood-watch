"""Timeline data endpoints."""

from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Query

from app.models.schemas import TimelineData, TimelinePoint, FloodEpisode
from app.services.data_store import get_timeline_data, get_episodes_in_range

router = APIRouter()


@router.get("", response_model=TimelineData)
async def get_timeline(
    start: Optional[datetime] = Query(None, description="Start datetime (default: 7 days ago)"),
    end: Optional[datetime] = Query(None, description="End datetime (default: now)"),
    resolution: str = Query("1h", description="Data resolution: 15m, 1h, 6h, 1d"),
):
    """
    Get timeline data for visualization.

    Returns time-series data for:
    - Tide height
    - River level (Dordogne at Libourne)
    - Rainfall (hourly)
    - Risk score
    - Video water level index

    Also includes flood episodes that occurred in the time range.
    """
    now = datetime.utcnow()

    if end is None:
        end = now
    if start is None:
        start = end - timedelta(days=7)

    # Validate resolution
    valid_resolutions = ["15m", "1h", "6h", "1d"]
    if resolution not in valid_resolutions:
        resolution = "1h"

    # Fetch timeline data from storage
    points = await get_timeline_data(start, end, resolution)

    # Fetch episodes in range
    episodes = await get_episodes_in_range(start, end)

    return TimelineData(
        start=start,
        end=end,
        resolution=resolution,
        points=points,
        episodes=episodes,
    )


@router.get("/stations")
async def get_multi_station_timeline(
    start: Optional[datetime] = Query(None, description="Start datetime (default: 48h ago)"),
    end: Optional[datetime] = Query(None, description="End datetime (default: now)"),
    station_ids: Optional[str] = Query(None, description="Comma-separated station IDs (default: all)"),
):
    """
    Get timeline data for multiple hydrometric stations.

    Returns time-series height data for each requested station.
    Useful for comparing water levels across the Dordogne, Isle, and Dronne rivers.
    """
    from app.services.collectors.hubeau import fetch_realtime_observations
    from app.config import get_settings

    settings = get_settings()
    now = datetime.utcnow()

    if end is None:
        end = now
    if start is None:
        start = end - timedelta(hours=48)

    # Make datetimes naive for comparison
    if start.tzinfo is not None:
        start = start.replace(tzinfo=None)
    if end.tzinfo is not None:
        end = end.replace(tzinfo=None)

    # Calculate hours back from now
    hours_back = max(1, int((now - start).total_seconds() / 3600))

    # Determine which stations to fetch
    if station_ids:
        requested_ids = [s.strip() for s in station_ids.split(",")]
    else:
        requested_ids = [s["id"] for s in settings.hubeau_stations]

    # Build station config lookup
    station_configs = {s["id"]: s for s in settings.hubeau_stations}

    result = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "stations": {},
    }

    for station_id in requested_ids:
        if station_id not in station_configs:
            continue

        config = station_configs[station_id]
        levels = await fetch_realtime_observations(station_id, hours_back)

        # Filter to our date range (handle timezone-aware timestamps)
        levels_in_range = []
        for lv in levels:
            ts = lv.timestamp
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
            if start <= ts <= end:
                levels_in_range.append(lv)

        if levels_in_range:
            result["stations"][station_id] = {
                "name": config["name"],
                "river": config["river"],
                "distance_km": config.get("distance_km", 0),
                "thresholds": config.get("thresholds", {}),
                "data": [
                    {
                        "timestamp": lv.timestamp.isoformat(),
                        "height_m": lv.height_m,
                    }
                    for lv in levels_in_range
                ],
            }

    return result


@router.get("/compare")
async def compare_periods(
    period1_start: datetime = Query(..., description="First period start"),
    period1_end: datetime = Query(..., description="First period end"),
    period2_start: datetime = Query(..., description="Second period start"),
    period2_end: datetime = Query(..., description="Second period end"),
):
    """Compare two time periods for analysis."""
    # Fetch data for both periods
    data1 = await get_timeline_data(period1_start, period1_end, "1h")
    data2 = await get_timeline_data(period2_start, period2_end, "1h")

    # Calculate summary statistics for comparison
    def summarize(points: list[TimelinePoint]) -> dict:
        if not points:
            return {}
        river_levels = [p.river_level_m for p in points if p.river_level_m is not None]
        rainfall = [p.rainfall_mm_h for p in points if p.rainfall_mm_h is not None]
        risk_scores = [p.risk_score for p in points if p.risk_score is not None]

        return {
            "river_level_max": max(river_levels) if river_levels else None,
            "river_level_avg": sum(river_levels) / len(river_levels) if river_levels else None,
            "rainfall_total": sum(rainfall) if rainfall else 0,
            "risk_score_max": max(risk_scores) if risk_scores else None,
            "risk_score_avg": sum(risk_scores) / len(risk_scores) if risk_scores else None,
        }

    return {
        "period1": {
            "start": period1_start,
            "end": period1_end,
            "summary": summarize(data1),
        },
        "period2": {
            "start": period2_start,
            "end": period2_end,
            "summary": summarize(data2),
        },
    }
