"""Episode analysis service.

Reconstructs the hydro-meteorological context around flood episodes
for pattern extraction and model training.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from app.models.schemas import (
    FloodEpisode,
    EpisodeAnalysis,
    EpisodeContext,
    TideData,
    RiverLevel,
    RainfallObservation,
    VideoAnalysis,
    TideType,
)
from app.services.data_store import (
    get_historical_tides,
    get_historical_river_levels,
    get_historical_rainfall,
)
from app.config import get_settings

logger = logging.getLogger(__name__)


async def analyze_episode(
    date: datetime,
    window_before: timedelta = timedelta(days=3),
    window_after: timedelta = timedelta(days=2),
    episode: Optional[FloodEpisode] = None,
) -> EpisodeAnalysis:
    """
    Analyze a flood episode by reconstructing its context.

    Fetches and aggregates data from:
    - Tides (coefficients, heights, phases)
    - River levels (Dordogne at Pessac, primary non-tidal station)
    - Rainfall (observed, on Lidoire basin)
    - Video analysis (if available)

    Returns comprehensive analysis including:
    - Aggregated metrics (max levels, cumulative rain, etc.)
    - Time series for visualization
    - Matched risk criteria
    """
    settings = get_settings()

    start = date - window_before
    end = date + window_after

    # Fetch historical data
    tides = await get_historical_tides(start, end)
    river_levels = await get_historical_river_levels(
        settings.hubeau_primary_station,  # Pessac: Libourne is tidal
        start,
        end,
    )
    rainfall = await get_historical_rainfall(start, end)

    # Calculate context metrics
    context = _calculate_context(
        date=date,
        tides=tides,
        river_levels=river_levels,
        rainfall=rainfall,
    )

    # Identify matched criteria
    matched_criteria = _identify_matched_criteria(context, settings)

    # Create episode if not provided
    if episode is None:
        episode = FloodEpisode(
            id=None,
            start_time=date,
            peak_time=date,
            severity=3,  # Default medium severity
            description="Analyse d'épisode",
        )

    return EpisodeAnalysis(
        episode=episode,
        context=context,
        tides=tides,
        river_levels=river_levels,
        rainfall=rainfall,
        video_data=[],  # TODO: Add video data when available
        matched_criteria=matched_criteria,
    )


def _calculate_context(
    date: datetime,
    tides: list[TideData],
    river_levels: list[RiverLevel],
    rainfall: list[RainfallObservation],
) -> EpisodeContext:
    """Calculate aggregated context metrics."""

    # Rainfall calculations
    rain_24h_before = sum(
        r.precipitation_mm_h
        for r in rainfall
        if date - timedelta(hours=24) <= r.timestamp <= date
    )
    rain_48h_before = sum(
        r.precipitation_mm_h
        for r in rainfall
        if date - timedelta(hours=48) <= r.timestamp <= date
    )
    rain_72h_before = sum(
        r.precipitation_mm_h
        for r in rainfall
        if date - timedelta(hours=72) <= r.timestamp <= date
    )

    # River level calculations
    levels_around_date = [
        r.height_m
        for r in river_levels
        if date - timedelta(hours=24) <= r.timestamp <= date + timedelta(hours=12)
    ]
    max_dordogne = max(levels_around_date) if levels_around_date else 0.0

    # Tide calculations
    high_tides_around = [
        t for t in tides
        if t.tide_type == TideType.HIGH
        and date - timedelta(hours=24) <= t.timestamp <= date + timedelta(hours=12)
    ]
    max_coefficient = max(
        (t.coefficient for t in high_tides_around if t.coefficient),
        default=0,
    )

    # Find tide phase at peak
    closest_high_tide = None
    min_diff = timedelta(days=999)
    for t in high_tides_around:
        diff = abs(t.timestamp - date)
        if diff < min_diff:
            min_diff = diff
            closest_high_tide = t

    if closest_high_tide:
        hours_since_high = (date - closest_high_tide.timestamp).total_seconds() / 3600
        if hours_since_high > 0:
            tide_phase = f"PM +{hours_since_high:.1f}h"
        else:
            tide_phase = f"PM {hours_since_high:.1f}h"
    else:
        hours_since_high = None
        tide_phase = None

    return EpisodeContext(
        rain_24h_before_mm=rain_24h_before,
        rain_48h_before_mm=rain_48h_before,
        rain_72h_before_mm=rain_72h_before,
        max_dordogne_level_m=max_dordogne,
        max_tide_coefficient=max_coefficient,
        tide_phase_at_peak=tide_phase,
        hours_since_high_tide=hours_since_high,
    )


def _identify_matched_criteria(context: EpisodeContext, settings) -> list[str]:
    """Identify which risk criteria were matched."""
    criteria = []

    # Rainfall criteria
    if context.rain_24h_before_mm >= settings.risk_rain_24h_red:
        criteria.append(f"Pluie 24h critique ({context.rain_24h_before_mm:.1f}mm >= {settings.risk_rain_24h_red}mm)")
    elif context.rain_24h_before_mm >= settings.risk_rain_24h_orange:
        criteria.append(f"Pluie 24h élevée ({context.rain_24h_before_mm:.1f}mm >= {settings.risk_rain_24h_orange}mm)")
    elif context.rain_24h_before_mm >= settings.risk_rain_24h_yellow:
        criteria.append(f"Pluie 24h notable ({context.rain_24h_before_mm:.1f}mm >= {settings.risk_rain_24h_yellow}mm)")

    # River level criteria
    if context.max_dordogne_level_m >= settings.risk_dordogne_level_red:
        criteria.append(f"Niveau Dordogne critique ({context.max_dordogne_level_m:.2f}m >= {settings.risk_dordogne_level_red}m)")
    elif context.max_dordogne_level_m >= settings.risk_dordogne_level_orange:
        criteria.append(f"Niveau Dordogne élevé ({context.max_dordogne_level_m:.2f}m >= {settings.risk_dordogne_level_orange}m)")
    elif context.max_dordogne_level_m >= settings.risk_dordogne_level_yellow:
        criteria.append(f"Niveau Dordogne notable ({context.max_dordogne_level_m:.2f}m >= {settings.risk_dordogne_level_yellow}m)")

    # Tide criteria
    if context.max_tide_coefficient >= settings.risk_tide_coef_high:
        criteria.append(f"Fort coefficient de marée ({context.max_tide_coefficient} >= {settings.risk_tide_coef_high})")

    # Tide phase criteria
    if context.hours_since_high_tide is not None:
        if abs(context.hours_since_high_tide) <= 2:
            criteria.append(f"Pic proche de la pleine mer ({context.tide_phase_at_peak})")

    # Combined criteria
    if (context.rain_24h_before_mm >= settings.risk_rain_24h_yellow and
        context.max_dordogne_level_m >= settings.risk_dordogne_level_yellow and
        context.max_tide_coefficient >= 80):
        criteria.append("Conjonction défavorable: pluie + niveau + marée")

    return criteria


async def extract_patterns_from_episodes(episodes: list[FloodEpisode]) -> dict:
    """
    Extract common patterns from multiple episodes.

    Useful for calibrating risk thresholds.
    """
    if not episodes:
        return {"status": "no_data"}

    analyses = []
    for ep in episodes:
        try:
            analysis = await analyze_episode(
                date=ep.start_time,
                episode=ep,
            )
            analyses.append(analysis)
        except Exception as e:
            logger.warning("Error analyzing episode %s: %s", ep.id, e)
            continue

    if not analyses:
        return {"status": "analysis_failed"}

    # Extract statistics
    rain_24h_values = [a.context.rain_24h_before_mm for a in analyses]
    rain_72h_values = [a.context.rain_72h_before_mm for a in analyses]
    dordogne_values = [a.context.max_dordogne_level_m for a in analyses]
    coef_values = [a.context.max_tide_coefficient for a in analyses]

    def stats(values: list) -> dict:
        if not values:
            return {}
        sorted_vals = sorted(values)
        return {
            "min": min(values),
            "max": max(values),
            "avg": sum(values) / len(values),
            "median": sorted_vals[len(sorted_vals) // 2],
            "p25": sorted_vals[len(sorted_vals) // 4],
            "p75": sorted_vals[3 * len(sorted_vals) // 4],
        }

    return {
        "status": "success",
        "episode_count": len(analyses),
        "patterns": {
            "rain_24h_mm": stats(rain_24h_values),
            "rain_72h_mm": stats(rain_72h_values),
            "dordogne_level_m": stats(dordogne_values),
            "tide_coefficient": stats(coef_values),
        },
        "suggested_thresholds": {
            "rain_24h_yellow": stats(rain_24h_values).get("p25", 30),
            "rain_24h_orange": stats(rain_24h_values).get("median", 50),
            "rain_24h_red": stats(rain_24h_values).get("p75", 80),
            "dordogne_yellow": stats(dordogne_values).get("p25", 3.0),
            "dordogne_orange": stats(dordogne_values).get("median", 4.0),
            "dordogne_red": stats(dordogne_values).get("p75", 5.0),
        },
    }
