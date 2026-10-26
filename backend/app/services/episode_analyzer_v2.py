"""Episode analyzer for flood event reconstruction.

Correlates the data of several sources around a past flood event (single
analyzer behind every /api/episodes analysis route):
- River levels of all stations: stored series (InfluxDB), else Hub'Eau /
  Hydroportail
- Rainfall: stored series (mean per hour across sources, as the risk model),
  else the Open-Meteo archive
- Tides: stored PM/BM events at the Lidoire confluence (``data_store``);
  without tide data the coefficient is unknown (None), never invented
"""

import logging
from datetime import datetime, timedelta
from typing import Optional
import httpx

from app.config import get_settings
from app.models.schemas import (
    FloodEpisode,
    EpisodeAnalysis,
    EpisodeContext,
    RiverLevel,
    TideData,
    TideType,
    RainfallObservation,
)
from app.services.collectors.hubeau import fetch_episode_context, fetch_historical_data
from app.services.risk_core import to_utc_naive

logger = logging.getLogger(__name__)


async def analyze_episode(
    episode_date: datetime,
    severity: int = 3,
    description: str = "",
    hours_before: int = 72,
    hours_after: int = 24,
    episode: Optional[FloodEpisode] = None,
) -> EpisodeAnalysis:
    """
    Analyze a historical flood episode by fetching all available data.

    Args:
        episode_date: Date/time of the flood peak (or approximate date)
        severity: Estimated severity 1-5
        description: User description of the event
        hours_before: Hours of context before the event
        hours_after: Hours of context after the event
        episode: Stored episode (returned as is, with its id and outcome)

    Returns:
        EpisodeAnalysis with all correlated data
    """
    # Naive UTC bounds (aware dates are converted)
    start = to_utc_naive(episode_date) - timedelta(hours=hours_before)
    end = to_utc_naive(episode_date) + timedelta(hours=hours_after)

    if episode is None:
        episode = FloodEpisode(
            start_time=start,
            peak_time=episode_date,
            end_time=end,
            severity=severity,
            description=description,
        )

    river_data = await _fetch_river_levels(episode_date, start, end, hours_before, hours_after)

    # Flatten river levels into a single list
    all_river_levels = []
    for station_id, levels in river_data.items():
        all_river_levels.extend(levels)

    # Find max levels per station
    max_levels = {}
    for station_id, levels in river_data.items():
        if levels:
            max_level = max(levels, key=lambda x: x.height_m)
            max_levels[station_id] = max_level

    tides = await _fetch_historical_tides(start, end)
    rainfall = await _fetch_rainfall(start, end)

    # Calculate context
    context = _calculate_episode_context(
        episode_date,
        river_data,
        tides,
        rainfall,
    )

    # Identify matched criteria (what made this flood happen)
    matched_criteria = _identify_flood_criteria(context, max_levels)

    return EpisodeAnalysis(
        episode=episode,
        context=context,
        tides=tides,
        river_levels=all_river_levels,
        rainfall=rainfall,
        matched_criteria=matched_criteria,
    )


async def _stored_river_levels(start: datetime, end: datetime) -> dict[str, list[RiverLevel]]:
    """Stored levels of every configured station over [start, end] (empty if none)."""
    from app.services import influx_client

    out: dict[str, list[RiverLevel]] = {}
    for station in get_settings().hubeau_stations:
        rows = await influx_client.query_river_levels(station["id"], start, end + timedelta(seconds=1))
        levels = [
            RiverLevel(
                timestamp=r["timestamp"],
                station_id=station["id"],
                station_name=r.get("station_name") or station["name"],
                river=r.get("river") or station["river"],
                height_m=float(r["height_m"]),
            )
            for r in rows
            if r.get("height_m") is not None and r.get("timestamp") is not None
        ]
        if levels:
            out[station["id"]] = levels
    return out


async def _fetch_river_levels(
    episode_date: datetime,
    start: datetime,
    end: datetime,
    hours_before: int,
    hours_after: int,
) -> dict[str, list[RiverLevel]]:
    """Stored levels when the primary station has some, else Hub'Eau / Hydroportail."""
    try:
        stored = await _stored_river_levels(start, end)
    except Exception as e:
        logger.warning("Stored river levels unavailable: %s", e)
        stored = {}
    if stored.get(get_settings().hubeau_primary_station):
        return stored
    return await fetch_episode_context(episode_date, hours_before=hours_before, hours_after=hours_after)


async def _fetch_historical_tides(
    start: datetime,
    end: datetime,
) -> list[TideData]:
    """Stored PM/BM events at the Lidoire confluence over [start, end] ([] if none)."""
    from app.services.data_store import get_historical_tides

    try:
        return await get_historical_tides(start, end)
    except Exception as e:
        logger.warning("Historical tides unavailable: %s", e)
        return []


async def _stored_rainfall(start: datetime, end: datetime) -> list[RainfallObservation]:
    """Stored hourly rain rows over [start, end] ([] if none)."""
    from app.services import influx_client

    rows = await influx_client.query_rainfall(start, end + timedelta(seconds=1))
    return [
        RainfallObservation(
            timestamp=r["timestamp"],
            source=f"{r.get('source') or 'unknown'}:{r.get('city') or ''}",
            precipitation_mm_h=float(r["precipitation_mm_h"]),
            basin_avg=False,
        )
        for r in rows
        if r.get("precipitation_mm_h") is not None and r.get("timestamp") is not None
    ]


async def _fetch_rainfall(start: datetime, end: datetime) -> list[RainfallObservation]:
    """Stored rain when present, else the Open-Meteo archive."""
    try:
        stored = await _stored_rainfall(start, end)
    except Exception as e:
        logger.warning("Stored rainfall unavailable: %s", e)
        stored = []
    return stored or await _fetch_historical_rainfall(start, end)


async def _fetch_historical_rainfall(
    start: datetime,
    end: datetime,
) -> list[RainfallObservation]:
    """
    Fetch historical rainfall data from Open-Meteo archive.

    Open-Meteo provides historical weather data for free.
    """
    settings = get_settings()
    rainfall = []

    # Use moulin coordinates
    lat = settings.moulin_lat
    lon = settings.moulin_lon

    # Open-Meteo Archive API
    url = "https://archive-api.open-meteo.com/v1/archive"

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "start_date": start.strftime("%Y-%m-%d"),
                    "end_date": end.strftime("%Y-%m-%d"),
                    "hourly": "precipitation",
                    # UTC labels (naive), like every other collector: the
                    # 24/48/72h windows below end at the episode date in UTC.
                    # Each value is the rain of the hour ENDING at its label.
                    "timezone": "UTC",
                },
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()

            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            precip = hourly.get("precipitation", [])

            for i, time_str in enumerate(times):
                if i < len(precip) and precip[i] is not None:
                    rainfall.append(
                        RainfallObservation(
                            timestamp=datetime.fromisoformat(time_str),
                            source="open-meteo-archive",
                            precipitation_mm_h=precip[i],
                        )
                    )

            total_mm = sum(p for p in precip if p is not None)
            logger.info(
                "Open-Meteo archive: %d hourly points, total=%.1fmm (%s to %s)",
                len(rainfall), total_mm, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
            )

    except Exception as e:
        logger.warning("Error fetching historical rainfall: %s", e)

    return rainfall


def _calculate_episode_context(
    episode_date: datetime,
    river_data: dict[str, list[RiverLevel]],
    tides: list[TideData],
    rainfall: list[RainfallObservation],
) -> EpisodeContext:
    """Calculate summary context metrics for an episode."""
    settings = get_settings()

    from app.services.risk_core import aggregate_hourly_rain, rain_window_sum

    # Naive UTC (aware values are converted, not just stripped)
    ep_date = to_utc_naive(episode_date)
    _strip_tz = to_utc_naive

    # Rainfall sums: mean per hour across sources, then sum (same rule as
    # the risk model), only hours before the episode date

    rain_hourly = aggregate_hourly_rain(
        (to_utc_naive(r.timestamp), r.precipitation_mm_h, getattr(r, "source", ""))
        for r in rainfall
    )
    rain_24h = rain_window_sum(rain_hourly, ep_date, 24)
    rain_48h = rain_window_sum(rain_hourly, ep_date, 48)
    rain_72h = rain_window_sum(rain_hourly, ep_date, 72)

    # Max Dordogne level = Pessac (primary, non-tidal). Libourne is never
    # used: it is tidally modulated, its peaks reflect tide, not flood.
    dordogne_levels = river_data.get(settings.hubeau_primary_station, [])
    max_dordogne = max(
        (lv.height_m for lv in dordogne_levels),
        default=0.0,
    )

    # Max tide coefficient: None (unknown) without tide data, never a default
    max_tide_coef = max(
        (t.coefficient for t in tides if t.coefficient),
        default=None,
    )

    # Find tide phase at peak
    high_tides = [t for t in tides if t.tide_type == TideType.HIGH]
    tide_phase = None
    hours_since_high = None

    if high_tides:
        # Find closest high tide to episode peak
        closest_high = min(
            high_tides,
            key=lambda t: abs((_strip_tz(t.timestamp) - ep_date).total_seconds()),
        )
        hours_diff = (ep_date - _strip_tz(closest_high.timestamp)).total_seconds() / 3600

        if hours_diff > 0:
            tide_phase = "après PM"
            hours_since_high = hours_diff
        else:
            tide_phase = "avant PM"
            hours_since_high = -hours_diff

    return EpisodeContext(
        rain_24h_before_mm=rain_24h,
        rain_48h_before_mm=rain_48h,
        rain_72h_before_mm=rain_72h,
        max_dordogne_level_m=max_dordogne,
        max_tide_coefficient=max_tide_coef,
        tide_phase_at_peak=tide_phase,
        hours_since_high_tide=hours_since_high,
    )


def _identify_flood_criteria(
    context: EpisodeContext,
    max_levels: dict[str, RiverLevel],
) -> list[str]:
    """
    Identify which criteria contributed to the flood.

    This helps understand patterns and calibrate thresholds.
    """
    settings = get_settings()
    criteria = []

    # Check rainfall
    if context.rain_24h_before_mm > settings.risk_rain_24h_red:
        criteria.append(f"Pluie 24h critique: {context.rain_24h_before_mm:.0f}mm")
    elif context.rain_24h_before_mm > settings.risk_rain_24h_orange:
        criteria.append(f"Pluie 24h élevée: {context.rain_24h_before_mm:.0f}mm")

    if context.rain_72h_before_mm > settings.risk_rain_24h_red * 2:
        criteria.append(f"Pluie 72h critique: {context.rain_72h_before_mm:.0f}mm")

    # Check Dordogne level
    if context.max_dordogne_level_m > settings.risk_dordogne_level_red:
        criteria.append(f"Dordogne niveau critique: {context.max_dordogne_level_m:.2f}m")
    elif context.max_dordogne_level_m > settings.risk_dordogne_level_orange:
        criteria.append(f"Dordogne niveau élevé: {context.max_dordogne_level_m:.2f}m")

    # Check tide (unknown coefficient: no tide criterion)
    coef = context.max_tide_coefficient
    if coef is not None and coef > 100:
        criteria.append(f"Grande marée: coef {coef}")
    elif coef is not None and coef > settings.risk_tide_coef_high:
        criteria.append(f"Forte marée: coef {coef}")

    # Check tide phase
    if context.hours_since_high_tide is not None and context.hours_since_high_tide < 2:
        criteria.append("Pic proche de la pleine mer")

    # Check other stations
    for station_id, level in max_levels.items():
        if station_id == settings.hubeau_primary_station:
            continue  # Already checked as "Dordogne" (Pessac)

        station_config = next(
            (s for s in settings.hubeau_stations if s["id"] == station_id),
            None,
        )
        if station_config and station_config.get("weight", 0) <= 0:
            continue  # Libourne: tidal, excluded
        if station_config:
            thresholds = station_config.get("thresholds", {})
            if level.height_m > thresholds.get("orange", 4.0):
                criteria.append(
                    f"{station_config['name']} ({station_config['river']}) élevé: "
                    f"{level.height_m:.2f}m"
                )

    # Check for conjunction — list actual contributing factors
    conjunction_factors = []
    if context.max_dordogne_level_m > settings.risk_dordogne_level_yellow:
        conjunction_factors.append(f"niveau riviere eleve ({context.max_dordogne_level_m:.1f}m)")
    if coef is not None and coef > 80:
        conjunction_factors.append(f"maree forte (coef {coef})")
    if context.rain_24h_before_mm > settings.risk_rain_24h_yellow:
        conjunction_factors.append(f"pluie 24h significative ({context.rain_24h_before_mm:.0f}mm)")
    if context.rain_72h_before_mm > settings.risk_rain_24h_yellow * 2:
        conjunction_factors.append(f"pluie cumulative 72h ({context.rain_72h_before_mm:.0f}mm)")

    # Count elevated stations
    elevated_stations = []
    for station_id, level in max_levels.items():
        station_config = next(
            (s for s in settings.hubeau_stations if s["id"] == station_id), None
        )
        if station_config:
            threshold_yellow = station_config.get("thresholds", {}).get("yellow", 3.0)
            if level.height_m > threshold_yellow:
                elevated_stations.append(station_config["name"])
    if len(elevated_stations) >= 3:
        conjunction_factors.append(
            f"{len(elevated_stations)} stations simultanement elevees "
            f"({', '.join(elevated_stations)})"
        )

    if len(conjunction_factors) >= 2:
        criteria.append(f"Conjonction: {' + '.join(conjunction_factors)}")

    return criteria


async def get_station_max_levels(
    start_date: datetime,
    end_date: datetime,
) -> dict[str, dict]:
    """
    Get maximum recorded levels for each station over a period.

    Useful for identifying extreme events and calibrating thresholds.
    """
    settings = get_settings()
    results = {}

    for station in settings.hubeau_stations:
        station_id = station["id"]
        levels = await fetch_historical_data(
            station_id,
            start_date,
            end_date,
            granularity="daily_max",
        )

        if levels:
            max_level = max(levels, key=lambda x: x.height_m)
            results[station_id] = {
                "station_name": station["name"],
                "river": station["river"],
                "max_height_m": max_level.height_m,
                "max_date": max_level.timestamp,
                "count": len(levels),
            }

    return results


async def compare_episodes(
    episode_dates: list[datetime],
) -> list[dict]:
    """
    Compare multiple flood episodes to identify patterns.

    Args:
        episode_dates: List of flood event dates to compare

    Returns:
        List of episode summaries for comparison
    """
    comparisons = []

    for date in episode_dates:
        analysis = await analyze_episode(date)

        comparisons.append({
            "date": date.isoformat(),
            "rain_24h_mm": analysis.context.rain_24h_before_mm,
            "rain_72h_mm": analysis.context.rain_72h_before_mm,
            "dordogne_max_m": analysis.context.max_dordogne_level_m,
            "tide_coef": analysis.context.max_tide_coefficient,
            "tide_phase": analysis.context.tide_phase_at_peak,
            "criteria": analysis.matched_criteria,
        })

    return comparisons
