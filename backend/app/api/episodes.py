"""Flood episodes management endpoints."""

from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Query

from app.models.schemas import (
    FloodEpisode,
    FloodEpisodeCreate,
    EpisodeAnalysis,
    EpisodeContext,
)
from app.services.data_store import (
    get_all_episodes,
    get_episode_by_id,
    create_episode,
    update_episode,
    delete_episode,
)
from app.services.episode_analyzer import analyze_episode

router = APIRouter()


@router.get("", response_model=list[FloodEpisode])
async def list_episodes(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    min_severity: Optional[int] = Query(None, ge=1, le=5),
):
    """List all recorded flood episodes."""
    episodes = await get_all_episodes(limit=limit, offset=offset, min_severity=min_severity)
    return episodes


@router.get("/{episode_id}", response_model=FloodEpisode)
async def get_episode(episode_id: int):
    """Get a specific flood episode by ID."""
    episode = await get_episode_by_id(episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="Episode not found")
    return episode


@router.post("", response_model=FloodEpisode)
async def create_flood_episode(episode: FloodEpisodeCreate):
    """
    Create a new flood episode record.

    This allows manual recording of historical flood events
    for later analysis and model training.
    """
    created = await create_episode(episode)
    return created


@router.put("/{episode_id}", response_model=FloodEpisode)
async def update_flood_episode(episode_id: int, episode: FloodEpisodeCreate):
    """Update an existing flood episode."""
    existing = await get_episode_by_id(episode_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Episode not found")

    updated = await update_episode(episode_id, episode)
    return updated


@router.delete("/{episode_id}")
async def delete_flood_episode(episode_id: int):
    """Delete a flood episode."""
    existing = await get_episode_by_id(episode_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Episode not found")

    await delete_episode(episode_id)
    return {"status": "deleted", "id": episode_id}


@router.post("/analyze", response_model=EpisodeAnalysis)
async def analyze_flood_episode(
    date: datetime = Query(..., description="Date of the flood episode to analyze"),
    window_before_days: int = Query(3, ge=1, le=7, description="Days before to include"),
    window_after_days: int = Query(2, ge=0, le=5, description="Days after to include"),
):
    """
    Analyze a flood episode by reconstructing the hydro-meteorological context.

    This endpoint:
    1. Fetches historical data around the specified date
    2. Calculates aggregate metrics (cumulative rainfall, max levels, etc.)
    3. Identifies matching risk criteria
    4. Returns a comprehensive analysis

    Useful for understanding past events and extracting patterns.
    """
    analysis = await analyze_episode(
        date=date,
        window_before=timedelta(days=window_before_days),
        window_after=timedelta(days=window_after_days),
    )
    return analysis


@router.get("/analyze/{episode_id}", response_model=EpisodeAnalysis)
async def analyze_existing_episode(episode_id: int):
    """Analyze an existing recorded episode."""
    episode = await get_episode_by_id(episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="Episode not found")

    # Use the episode's start time for analysis
    analysis = await analyze_episode(
        date=episode.start_time,
        window_before=timedelta(days=3),
        window_after=timedelta(days=2),
        episode=episode,
    )
    return analysis


@router.get("/patterns")
async def extract_patterns(min_episodes: int = Query(3, ge=2, le=20)):
    """
    Extract common patterns from recorded flood episodes.

    Analyzes all recorded episodes to find:
    - Common rainfall thresholds
    - Typical river levels before flooding
    - Tide coefficient patterns
    - Time delays between rain and flood
    """
    episodes = await get_all_episodes(limit=100)

    if len(episodes) < min_episodes:
        return {
            "status": "insufficient_data",
            "message": f"Need at least {min_episodes} episodes, found {len(episodes)}",
            "patterns": None,
        }

    # Analyze each episode and extract patterns
    analyses = []
    for ep in episodes:
        try:
            analysis = await analyze_episode(
                date=ep.start_time,
                window_before=timedelta(days=3),
                window_after=timedelta(days=1),
                episode=ep,
            )
            analyses.append(analysis)
        except Exception:
            continue

    if not analyses:
        return {
            "status": "analysis_failed",
            "message": "Could not analyze any episodes",
            "patterns": None,
        }

    # Extract patterns from analyses
    rain_24h = [a.context.rain_24h_before_mm for a in analyses]
    rain_72h = [a.context.rain_72h_before_mm for a in analyses]
    dordogne_max = [a.context.max_dordogne_level_m for a in analyses]
    tide_coef = [a.context.max_tide_coefficient for a in analyses]

    patterns = {
        "rainfall_24h": {
            "min": min(rain_24h),
            "max": max(rain_24h),
            "avg": sum(rain_24h) / len(rain_24h),
            "threshold_suggestion": sorted(rain_24h)[len(rain_24h) // 4],  # 25th percentile
        },
        "rainfall_72h": {
            "min": min(rain_72h),
            "max": max(rain_72h),
            "avg": sum(rain_72h) / len(rain_72h),
            "threshold_suggestion": sorted(rain_72h)[len(rain_72h) // 4],
        },
        "dordogne_level": {
            "min": min(dordogne_max),
            "max": max(dordogne_max),
            "avg": sum(dordogne_max) / len(dordogne_max),
            "threshold_suggestion": sorted(dordogne_max)[len(dordogne_max) // 4],
        },
        "tide_coefficient": {
            "min": min(tide_coef),
            "max": max(tide_coef),
            "avg": sum(tide_coef) / len(tide_coef),
            "high_tide_correlation": sum(1 for c in tide_coef if c > 90) / len(tide_coef),
        },
        "episode_count": len(analyses),
    }

    return {
        "status": "success",
        "patterns": patterns,
    }
