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
