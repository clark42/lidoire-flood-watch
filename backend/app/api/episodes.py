"""Flood episodes management endpoints."""

import asyncio
import inspect
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from app.models.schemas import (
    FloodEpisode,
    FloodEpisodeCreate,
    EpisodeAnalysis,
    EpisodeLabelSet,
    EpisodeOutcome,
)
from app.services.data_store import (
    get_all_episodes,
    get_episode_by_id,
    create_episode,
    update_episode,
    delete_episode,
)
from app.services.episode_analyzer import analyze_episode
from app.services.episode_analyzer_v2 import (
    analyze_episode as analyze_episode_v2,
    compare_episodes,
    get_station_max_levels,
)
from app.services.backfill import run_full_backfill
from app.services.episode_detector import run_episode_detection
from app.services.labels import load_label_set, strip_legacy_prefixes

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# In-memory backfill job tracker
_backfill_status: dict = {
    "running": False,
    "progress": None,
    "result": None,
    "error": None,
    "started_at": None,
    "finished_at": None,
}

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


@router.get("/labels", response_model=EpisodeLabelSet)
async def get_episode_labels():
    """
    Reference ground truth of the annotated episodes.

    Served from app/data/episodes_labels.json (single source of truth for
    model evaluation), keyed by peak date rather than by EP number.
    """
    try:
        return load_label_set()
    except (OSError, ValueError) as e:
        logger.error("Cannot load episode labels: %s", e)
        raise HTTPException(status_code=500, detail="Episode labels unavailable")


@router.post("", response_model=FloodEpisode)
async def create_flood_episode(episode: FloodEpisodeCreate):
    """
    Create a new flood episode record.

    This allows manual recording of historical flood events
    for later analysis and model training.
    """
    created = await create_episode(episode)
    return created


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


# ============================================================================
# V2 Endpoints - Multi-station analysis with Hub'Eau
# ============================================================================


@router.post("/analyze-v2", response_model=EpisodeAnalysis)
async def analyze_flood_episode_v2(
    date: datetime = Query(..., description="Date/time of the flood peak"),
    severity: int = Query(3, ge=1, le=5, description="Estimated severity"),
    description: str = Query("", description="Description of the event"),
    hours_before: int = Query(72, ge=24, le=168, description="Hours of context before"),
    hours_after: int = Query(24, ge=0, le=72, description="Hours of context after"),
):
    """
    Analyze a flood episode using the enhanced v2 multi-station model.

    This endpoint fetches historical data from:
    - Hub'Eau: All configured stations (Dordogne, Isle, Dronne)
    - Open-Meteo Archive: Historical rainfall
    - Tide archives: Historical tide data

    Returns a comprehensive analysis with:
    - River levels from all stations around the event
    - Rainfall cumulative and patterns
    - Tide context
    - Identified contributing factors

    Example: POST /api/episodes/analyze-v2?date=2024-01-15T14:00:00&severity=4
    """
    analysis = await analyze_episode_v2(
        episode_date=date,
        severity=severity,
        description=description,
        hours_before=hours_before,
        hours_after=hours_after,
    )
    return analysis


@router.post("/compare")
async def compare_flood_episodes(
    dates: list[datetime] = Query(..., description="List of episode dates to compare"),
):
    """
    Compare multiple flood episodes to identify common patterns.

    Provide a list of known flood dates and get a comparison showing:
    - Rainfall before each event
    - River levels at each station
    - Tide coefficients
    - Common contributing factors

    Example: POST /api/episodes/compare?dates=2024-01-15T14:00&dates=2023-12-10T08:00
    """
    if len(dates) < 2:
        raise HTTPException(
            status_code=400,
            detail="Need at least 2 dates to compare",
        )

    comparisons = await compare_episodes(dates)

    # Calculate summary statistics
    rain_24h_values = [c["rain_24h_mm"] for c in comparisons]
    rain_72h_values = [c["rain_72h_mm"] for c in comparisons]
    dordogne_values = [c["dordogne_max_m"] for c in comparisons]
    tide_values = [c["tide_coef"] for c in comparisons]

    summary = {
        "episode_count": len(comparisons),
        "rain_24h": {
            "min": min(rain_24h_values),
            "max": max(rain_24h_values),
            "avg": sum(rain_24h_values) / len(rain_24h_values),
        },
        "rain_72h": {
            "min": min(rain_72h_values),
            "max": max(rain_72h_values),
            "avg": sum(rain_72h_values) / len(rain_72h_values),
        },
        "dordogne_max": {
            "min": min(dordogne_values),
            "max": max(dordogne_values),
            "avg": sum(dordogne_values) / len(dordogne_values),
        },
        "tide_coef": {
            "min": min(tide_values),
            "max": max(tide_values),
            "avg": sum(tide_values) / len(tide_values),
        },
    }

    # Find common criteria
    all_criteria = []
    for c in comparisons:
        all_criteria.extend(c.get("criteria", []))

    # Count criteria occurrences
    criteria_counts = {}
    for criterion in all_criteria:
        # Normalize criterion (remove numbers for grouping)
        base = criterion.split(":")[0].strip()
        criteria_counts[base] = criteria_counts.get(base, 0) + 1

    common_criteria = [
        {"criterion": k, "occurrences": v, "frequency": v / len(comparisons)}
        for k, v in criteria_counts.items()
        if v >= len(comparisons) * 0.5  # Present in at least 50% of episodes
    ]
    common_criteria.sort(key=lambda x: x["occurrences"], reverse=True)

    return {
        "episodes": comparisons,
        "summary": summary,
        "common_criteria": common_criteria,
    }


@router.get("/station-extremes")
async def get_station_extremes(
    start_date: datetime = Query(..., description="Start of analysis period"),
    end_date: datetime = Query(..., description="End of analysis period"),
):
    """
    Get maximum recorded levels for each station over a period.

    Useful for:
    - Identifying extreme events
    - Calibrating threshold values
    - Understanding historical ranges

    Example: GET /api/episodes/station-extremes?start_date=2020-01-01&end_date=2024-12-31
    """
    max_levels = await get_station_max_levels(start_date, end_date)

    return {
        "period": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
        },
        "stations": max_levels,
    }


# ============================================================================
# Backfill & Detection Endpoints
# ============================================================================


async def _run_backfill_task(
    start_date: Optional[datetime],
    end_date: Optional[datetime],
    skip_rivers: bool,
    skip_rainfall: bool,
    skip_tides: bool,
    skip_soil: bool,
    skip_risk: bool,
    risk_shadow: bool = False,
):
    """Background task for backfill."""
    global _backfill_status
    try:
        results = await run_full_backfill(
            start_date=start_date,
            end_date=end_date,
            skip_rivers=skip_rivers,
            skip_rainfall=skip_rainfall,
            skip_tides=skip_tides,
            skip_soil=skip_soil,
            skip_risk=skip_risk,
            risk_shadow=risk_shadow,
        )
        _backfill_status["result"] = results
        _backfill_status["finished_at"] = _utcnow().isoformat()
    except Exception as e:
        _backfill_status["error"] = str(e)
        _backfill_status["finished_at"] = _utcnow().isoformat()
        logger.error("Backfill error: %s", e)
    finally:
        _backfill_status["running"] = False


@router.post("/backfill")
async def trigger_backfill(
    start_date: Optional[datetime] = Query(None, description="Start date (default: 2022-11-01)"),
    end_date: Optional[datetime] = Query(None, description="End date (default: now)"),
    skip_rivers: bool = Query(False, description="Skip river level backfill"),
    skip_rainfall: bool = Query(False, description="Skip rainfall backfill"),
    skip_tides: bool = Query(False, description="Skip tide backfill"),
    skip_soil: bool = Query(False, description="Skip soil data backfill"),
    skip_risk: bool = Query(False, description="Skip risk score recalculation"),
    risk_shadow: bool = Query(False, description="Also backfill the shadow model 3.3-gate"),
):
    """
    Trigger comprehensive data backfill as a background task.

    Returns immediately with status. Poll GET /api/episodes/backfill/status
    to track progress.

    This endpoint fetches ALL historical data from:
    - Hydroportail: River levels for 7 stations (sub-hourly, since 2022)
    - Open-Meteo Archive: Hourly rainfall
    - Open-Meteo Marine: Hourly tide heights
    - Open-Meteo: Soil moisture and temperature

    Then recalculates risk scores using the v3.3 model (and, with
    ``risk_shadow=true``, the shadow model 3.3-gate).

    Example: POST /api/episodes/backfill?start_date=2022-11-01T00:00:00
    """
    global _backfill_status

    if _backfill_status["running"]:
        return {
            "status": "already_running",
            "started_at": _backfill_status["started_at"],
            "message": "Un backfill est deja en cours. GET /api/episodes/backfill/status pour le suivi.",
        }

    _backfill_status = {
        "running": True,
        "progress": "starting",
        "result": None,
        "error": None,
        "started_at": _utcnow().isoformat(),
        "finished_at": None,
    }

    # Fire-and-forget background task
    asyncio.create_task(_run_backfill_task(
        start_date=start_date,
        end_date=end_date,
        skip_rivers=skip_rivers,
        skip_rainfall=skip_rainfall,
        skip_tides=skip_tides,
        skip_soil=skip_soil,
        skip_risk=skip_risk,
        risk_shadow=risk_shadow,
    ))

    return {
        "status": "started",
        "started_at": _backfill_status["started_at"],
        "message": "Backfill lance en arriere-plan. GET /api/episodes/backfill/status pour le suivi.",
    }


@router.get("/backfill/status")
async def backfill_status():
    """
    Get current backfill job status.

    Returns running state, progress, result or error.
    """
    return _backfill_status


@router.post("/detect")
async def detect_episodes_endpoint(
    start_date: Optional[datetime] = Query(None, description="Start date (default: 2022-11-01)"),
    end_date: Optional[datetime] = Query(None, description="End date (default: now)"),
    threshold: Optional[float] = Query(
        None,
        ge=0,
        le=100,
        description="Risk score threshold (only if the detector supports it; else 400)",
    ),
):
    """
    Detect potential flood episodes from backfilled data.

    Analyzes stored risk scores and river levels to find periods that
    match flood criteria:
    - Risk score >= 55 (RED) for at least 2 hours
    - Risk score >= 35 (ORANGE) for at least 6 hours
    - River level exceeds station red threshold

    Returns a list of detected episodes for user validation.
    You can then confirm/reject each episode to refine the risk model.

    Any range can be requested at once: data is read in 30-day windows
    (bounded memory) with the same result as a single pass.

    Example: POST /api/episodes/detect?start_date=2022-11-01
    """
    kwargs: dict = {"start_date": start_date, "end_date": end_date}
    if threshold is not None:
        # Forward only if the detector accepts it: never silently ignore it
        if "threshold" not in inspect.signature(run_episode_detection).parameters:
            raise HTTPException(
                status_code=400,
                detail="threshold is not supported by the episode detector yet",
            )
        kwargs["threshold"] = threshold

    results = await run_episode_detection(**kwargs)

    return results


# ============================================================================
# Routes with a path parameter. Keep them LAST: FastAPI matches in
# declaration order and "/{episode_id}" would otherwise shadow static GET
# routes such as /patterns or /station-extremes (returning 422).
# ============================================================================


@router.get("/analyze/{episode_id}", response_model=EpisodeAnalysis)
async def analyze_existing_episode(episode_id: int):
    """Analyze an existing recorded episode using v2 multi-station model."""
    episode = await get_episode_by_id(episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="Episode not found")

    # Use peak_time if available, otherwise start_time
    episode_date = episode.peak_time or episode.start_time

    analysis = await analyze_episode_v2(
        episode_date=episode_date,
        severity=episode.severity,
        description=episode.description or "",
        hours_before=72,
        hours_after=24,
    )
    return analysis


@router.post("/confirm/{episode_id}")
async def confirm_detected_episode(
    episode_id: int,
    outcome: Optional[EpisodeOutcome] = Query(
        None, description="Ground-truth outcome (flooded, false_positive, near_miss, unknown)"
    ),
    was_flooded: Optional[bool] = Query(
        None, description="Legacy: true=flooded, false=false_positive (ignored if outcome is set)"
    ),
    actual_severity: Optional[int] = Query(None, ge=1, le=5, description="Actual flood severity"),
    notes: Optional[str] = Query(None, description="Notes (replace existing notes when provided)"),
    observed_at: Optional[datetime] = Query(None, description="Observed peak time at the mill"),
    water_height_cm: Optional[float] = Query(None, ge=0, description="Observed water height (cm)"),
    source: str = Query("user", max_length=100, description="Who/what labelled the episode"),
):
    """
    Set the ground-truth outcome of a detected/existing episode.

    The outcome is stored in a structured field: confirming again REPLACES the
    previous outcome (e.g. flooded -> false_positive), it never stacks text
    prefixes in notes. Legacy ``[CONFIRMED: ...]`` prefixes are stripped.

    Fields not provided keep their current value, except water_height_cm which
    is cleared when the outcome becomes false_positive.
    """
    if outcome is None:
        if was_flooded is None:
            raise HTTPException(status_code=422, detail="Provide outcome or was_flooded")
        outcome = EpisodeOutcome.FLOODED if was_flooded else EpisodeOutcome.FALSE_POSITIVE

    episode = await get_episode_by_id(episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="Episode not found")

    if water_height_cm is None and outcome != EpisodeOutcome.FALSE_POSITIVE:
        water_height_cm = episode.water_height_cm

    update_data = FloodEpisodeCreate(
        start_time=episode.start_time,
        peak_time=episode.peak_time,
        end_time=episode.end_time,
        severity=actual_severity if actual_severity else episode.severity,
        description=episode.description,
        notes=strip_legacy_prefixes(notes if notes is not None else episode.notes),
        outcome=outcome,
        observed_at=observed_at if observed_at is not None else episode.observed_at,
        water_height_cm=water_height_cm,
        outcome_source=source,
    )

    updated = await update_episode(episode_id, update_data)

    if outcome == EpisodeOutcome.FLOODED:
        status = "confirmed"
    elif outcome == EpisodeOutcome.FALSE_POSITIVE:
        status = "rejected"
    else:
        status = outcome.value

    return {
        "status": status,
        "outcome": outcome.value,
        "episode": updated,
    }


@router.get("/{episode_id}", response_model=FloodEpisode)
async def get_episode(episode_id: int):
    """Get a specific flood episode by ID."""
    episode = await get_episode_by_id(episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="Episode not found")
    return episode


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
