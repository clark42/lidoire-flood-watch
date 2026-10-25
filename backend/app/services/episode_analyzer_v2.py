"""Enhanced episode analyzer for flood event reconstruction.

Fetches and correlates historical data from multiple sources to analyze
past flood events and calibrate the risk model.

Data sources:
- Hub'Eau: River levels from all stations (Dordogne, Isle, Dronne)
- Tide data: Historical tide coefficients and times
- Weather: Rainfall data from Netatmo, Open-Meteo archive
- Local observations: User-provided notes and photos
"""

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


async def analyze_episode(
    episode_date: datetime,
    severity: int = 3,
    description: str = "",
    hours_before: int = 72,
    hours_after: int = 24,
) -> EpisodeAnalysis:
    """
    Analyze a historical flood episode by fetching all available data.

    Args:
        episode_date: Date/time of the flood peak (or approximate date)
        severity: Estimated severity 1-5
        description: User description of the event
        hours_before: Hours of context before the event
        hours_after: Hours of context after the event

    Returns:
        EpisodeAnalysis with all correlated data
    """
    settings = get_settings()

    # Create episode record
    episode = FloodEpisode(
        start_time=episode_date - timedelta(hours=hours_before),
        peak_time=episode_date,
        end_time=episode_date + timedelta(hours=hours_after),
        severity=severity,
        description=description,
    )

    # Fetch river levels from all stations
    river_data = await fetch_episode_context(
        episode_date,
        hours_before=hours_before,
        hours_after=hours_after,
    )

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

    # Fetch tide data
    tides = await _fetch_historical_tides(
        episode_date - timedelta(hours=hours_before),
        episode_date + timedelta(hours=hours_after),
    )

    # Fetch rainfall data
    rainfall = await _fetch_historical_rainfall(
        episode_date - timedelta(hours=hours_before),
        episode_date + timedelta(hours=hours_after),
    )

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


async def _fetch_historical_tides(
    start: datetime,
    end: datetime,
) -> list[TideData]:
    """
    Fetch historical tide data for a period.

    Uses maree.info archive or SHOM historical data.
    """
    # For now, return empty - would need to integrate maree.info historical API
    # or scrape tide tables
    tides = []

    # TODO: Implement historical tide fetching
    # Options:
    # 1. maree.info historical data
    # 2. SHOM historical archives
    # 3. Calculated from astronomical predictions

    return tides


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
                    "timezone": "Europe/Paris",
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
            print(f"Open-Meteo archive: {len(rainfall)} hourly points, total={total_mm:.1f}mm "
                  f"({start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')})")

    except Exception as e:
        print(f"Error fetching historical rainfall: {e}")

    return rainfall


def _calculate_episode_context(
    episode_date: datetime,
    river_data: dict[str, list[RiverLevel]],
    tides: list[TideData],
    rainfall: list[RainfallObservation],
) -> EpisodeContext:
    """Calculate summary context metrics for an episode."""
    settings = get_settings()

    # Strip timezone to avoid naive/aware comparison failures
    ep_date = episode_date.replace(tzinfo=None) if episode_date.tzinfo else episode_date

    def _strip_tz(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    # Rainfall sums: mean per hour across sources, then sum (same rule as
    # the risk model), only hours before the episode date
    from app.services.risk_core import aggregate_hourly_rain, rain_window_sum, to_utc_naive

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

    # Find max tide coefficient
    max_tide_coef = max(
        (t.coefficient for t in tides if t.coefficient),
        default=70,
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

    # Check tide
    if context.max_tide_coefficient > 100:
        criteria.append(f"Grande marée: coef {context.max_tide_coefficient}")
    elif context.max_tide_coefficient > settings.risk_tide_coef_high:
        criteria.append(f"Forte marée: coef {context.max_tide_coefficient}")

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
    if context.max_tide_coefficient > 80:
        conjunction_factors.append(f"maree forte (coef {context.max_tide_coefficient})")
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
