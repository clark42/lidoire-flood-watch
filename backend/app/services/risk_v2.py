"""Enhanced risk scoring service with multi-station support.

Calculates flood risk based on:
- Multiple river stations (Dordogne, Isle, Dronne)
- Rate of water level change (tendency)
- Multi-station momentum (stations rising simultaneously)
- Tide coefficient and phase
- Conjunction factor (active flood combinations = multiplier)
- Rainfall (24h, 72h, 7d cumulative)
- Westerly wind (surge amplification on Gironde estuary)
- Soil saturation (runoff factor from Open-Meteo + Meteo Agricole)
- Video analysis (water level index)

Risk model weights (v3.0 - 11 factors):
- Multi-station river levels: 28% (reduced: level alone doesn't predict moulin flooding)
- Rainfall (24h + 72h + 7d): 27%
- Tendency: 12% (increased: rising is the strongest signal)
- Momentum (multi-station rising): 8% (NEW: simultaneous rise across basin)
- Tide (coefficient + phase): 10%
- Soil saturation: 5%
- Westerly wind: 3%
- Video analysis: 5%
- Conjunction multiplier: up to 2.5x (redesigned for active flood detection)

v3.0 changes (Jan 2026):
- Calibrated from 14 annotated episodes (5 confirmed floods, 9 false positives).
- Key insight: moulin is on the Lidoire (tributary), NOT on the Dordogne.
  High Dordogne level alone does NOT mean flood at the moulin.
  Pessac at 4.92m RED in May 2024 = no flood (0 rising, 0mm rain).
  Pessac at 4.11m in Dec 2023 = 25cm inside (5 rising, 18mm rain).
- NEW momentum factor: counts stations actively rising simultaneously.
  4+ stations rising = strongest flood signal across all episodes.
- NEW stability dampener: high but stable/falling river → reduced score.
  Catches false positives like EP5 (Pessac RED, 0 rising = no flood).
- Conjunction multiplier redesigned: triggers on "active flood" pattern
  (multiple rising + rain) rather than just "high values".
- Unified weights between calculate_risk_score_v2 and backfill function.
- River weight reduced 40%→28%: raw level is poor predictor alone.
- Tendency weight increased 10%→12%: rising = danger signal.

v2.2 changes (Jan 2026):
- Libourne (P577001001) excluded from river factor (weight=0):
  tidal oscillation ~4m masks flood signal.
- Primary station for tendency: Pessac-sur-Dordogne (P555001001).
- Backfill uses weighted multi-station average instead of single station.
- Based on analysis of episode avril 2025 (id=1).
"""

from datetime import datetime, timedelta
from typing import Optional

from app.config import get_settings
from app.models.schemas import (
    RiskScore,
    RiskLevel,
    RiskFactors,
    RiskWeights,
    StationRiskScore,
    TideForecast,
    RainfallSummary,
    MultiStationData,
    StationLevel,
    TideType,
    Tendency,
)


def _normalize_score(
    value: float,
    thresholds: tuple[float, float, float],
    exponential: bool = True,
) -> float:
    """
    Normalize a value to 0-1 based on thresholds.

    thresholds = (yellow, orange, red)
    Returns:
    - 0.0-0.33: below yellow
    - 0.33-0.66: yellow to orange
    - 0.66-1.0: orange to red and above

    If exponential=True, applies a power curve to make the score
    increase faster as values approach critical thresholds.
    This reflects the non-linear nature of flood risk.
    """
    yellow, orange, red = thresholds

    if value <= 0:
        return 0.0
    elif value < yellow:
        linear = (value / yellow) * 0.33
    elif value < orange:
        linear = 0.33 + ((value - yellow) / (orange - yellow)) * 0.33
    elif value < red:
        linear = 0.66 + ((value - orange) / (red - orange)) * 0.34
    else:
        linear = 1.0

    if exponential and linear > 0:
        # Apply power curve: steeper increase at higher values
        # score^0.6 makes high values contribute even more (was 0.7)
        # e.g., 0.33 -> 0.52, 0.5 -> 0.66, 0.8 -> 0.87
        return linear ** 0.6
    return linear


def _calculate_station_score(station: StationLevel) -> StationRiskScore:
    """Calculate risk score for a single station."""
    height = station.current_level.height_m

    # Use station-specific thresholds
    score = _normalize_score(
        height,
        (station.threshold_yellow, station.threshold_orange, station.threshold_red),
    )

    return StationRiskScore(
        station_id=station.station_id,
        station_name=station.station_name,
        river=station.river,
        height_m=height,
        score=score,
        weight=station.weight,
        weighted_score=score * station.weight,
        tendency=station.tendency,
        rate_cm_per_hour=station.rate_cm_per_hour,
    )


def _calculate_multi_station_score(stations_data: MultiStationData) -> tuple[float, list[StationRiskScore]]:
    """
    Calculate combined river level score from all stations.

    Stations with weight=0 (e.g. Libourne, tidal) are scored for display
    but excluded from the weighted average.

    Returns:
        Tuple of (combined_score, list of per-station scores)
    """
    station_scores = []
    total_weighted_score = 0.0
    total_weight = 0.0

    for station in stations_data.stations:
        score = _calculate_station_score(station)
        station_scores.append(score)
        # Skip zero-weight stations in the aggregate
        if station.weight > 0:
            total_weighted_score += score.weighted_score
            total_weight += station.weight

    # Normalize to 0-1
    combined_score = total_weighted_score / total_weight if total_weight > 0 else 0.0

    return combined_score, station_scores


def _calculate_tendency_score(stations_data: MultiStationData) -> float:
    """
    Calculate risk bonus from rate of rise.

    Fast rising water is more dangerous than stable high water.
    Uses primary station (Pessac-sur-Dordogne) or falls back to
    the highest-weight station with data.

    v2.5: More aggressive scoring - even slow rise is significant
    when monitoring for floods.
    """
    primary_id = stations_data.primary_station_id
    primary_station = next(
        (s for s in stations_data.stations if s.station_id == primary_id),
        None
    )

    # Fallback: highest-weight station with weight > 0
    if not primary_station:
        candidates = [s for s in stations_data.stations if s.weight > 0]
        if candidates:
            primary_station = max(candidates, key=lambda s: s.weight)
        else:
            return 0.0

    if not primary_station:
        return 0.0

    rate = primary_station.rate_cm_per_hour

    # Score based on rate of rise (v2.5: more aggressive)
    if rate <= 0:
        return 0.0  # Falling or stable
    elif rate < 2:
        return 0.3  # Very slow rise (was 0 for <5)
    elif rate < 5:
        return 0.5  # Slow rise (was 0.2)
    elif rate < 10:
        return 0.7  # Moderate rise (was 0.4)
    elif rate < 20:
        return 0.85  # Fast rise (was 0.6)
    else:
        return 1.0  # Very fast rise


def _calculate_tide_phase_score(
    tide: TideForecast,
    timestamp: Optional[datetime] = None,
) -> float:
    """
    Calculate risk from tide phase.

    Risk is highest at high tide (pleine mer).
    Returns 0-1 score.
    """
    if timestamp is None:
        timestamp = datetime.utcnow()

    if not tide.next_high_tide:
        return 0.0

    # Calculate hours until next high tide
    hours_to_high = (tide.next_high_tide.timestamp - timestamp).total_seconds() / 3600

    # Also check if we just passed high tide
    past_high_times = [
        t for t in tide.tides
        if t.tide_type == TideType.HIGH and t.timestamp < timestamp
    ]
    if past_high_times:
        hours_since_high = (timestamp - past_high_times[-1].timestamp).total_seconds() / 3600
    else:
        hours_since_high = 12  # Assume long time ago

    # Closest high tide (past or future)
    min_hours = min(abs(hours_to_high), abs(hours_since_high))

    # Risk is highest at high tide (0 hours), decreases with time
    if min_hours <= 1:
        return 1.0
    elif min_hours <= 2:
        return 0.8
    elif min_hours <= 3:
        return 0.5
    elif min_hours <= 4:
        return 0.3
    else:
        return 0.1


def _calculate_momentum_score(
    station_scores: list[StationRiskScore] | None = None,
    n_rising: int = 0,
    n_total: int = 6,
    river_score: float = 0.5,
) -> float:
    """
    Calculate multi-station momentum score (v3.0).

    Measures how many stations are rising simultaneously — the strongest
    predictor of actual flooding at the moulin across all 14 annotated episodes.

    Flooded episodes had 4.4 rising stations on average vs 2.1 for non-flooded.

    IMPORTANT: Gated by river level. Rising from a LOW level (EP4: 5 rising
    but river=0.514, EP6: 6 rising but river=0.353) is an early warning,
    not an emergency. Full momentum only counts when rivers are ALSO elevated.

    Args:
        station_scores: Per-station scores (from realtime)
        n_rising: Number of rising stations (from backfill)
        n_total: Total active stations
        river_score: Current weighted river score (0-1)

    Returns:
        Score 0-1 representing multi-station momentum.
    """
    if station_scores:
        active = [s for s in station_scores if s.weight > 0]
        n_total = len(active) if active else 6
        n_rising = sum(1 for s in active if s.rate_cm_per_hour > 1.0)

    if n_total == 0:
        return 0.0

    # Base momentum score from rising count
    if n_rising == 0:
        base = 0.0
    elif n_rising <= 2:
        base = 0.2
    elif n_rising == 3:
        base = 0.5
    elif n_rising == 4:
        base = 0.8
    else:
        base = 1.0

    # River level gate: rising from LOW level = early warning only
    # EP4: 5 rising but river=0.514 → no flood (rivers rising but not yet high)
    # EP6: 6 rising but river=0.353 → no flood (early rise from low)
    # EP14: 5 rising + river=0.602 → 25cm flood (rivers already elevated)
    if river_score >= 0.55:
        gate = 1.0   # Rivers elevated → full momentum signal
    elif river_score >= 0.40:
        gate = 0.7   # Rivers moderate → partial momentum
    elif river_score >= 0.30:
        gate = 0.4   # Rivers still low → early warning
    else:
        gate = 0.2   # Rivers low → minimal signal

    return base * gate


def _calculate_stability_dampener(
    river_score: float,
    tendency: Tendency,
    station_scores: list[StationRiskScore] | None = None,
    n_rising: int = 0,
) -> float:
    """
    Dampener for high-but-stable river conditions (v3.0).

    When the river is elevated but NOT rising, the flood risk at the moulin
    is much lower than the raw score suggests. This catches false positives
    like EP5 (Pessac 4.92m RED, 0 rising, 0 rain = no flood).

    Returns a multiplier 0.6-1.0 to apply to the final score.
    """
    if station_scores:
        active = [s for s in station_scores if s.weight > 0]
        n_rising = sum(1 for s in active if s.rate_cm_per_hour > 1.0)

    # Only dampens when river is elevated
    if river_score < 0.4:
        return 1.0

    # High river + stable/falling + few stations rising = dampened
    if tendency in (Tendency.STABLE, Tendency.FALLING):
        if n_rising == 0:
            return 0.65  # Strong dampening (EP5: Pessac RED, 0 rising = score×0.65)
        elif n_rising <= 1:
            return 0.80  # Moderate dampening (EP9: 1 rising)
    elif tendency == Tendency.RISING and n_rising <= 1:
        # Rising at primary but only 1 station = moderate dampening
        return 0.85

    return 1.0


def _calculate_conjunction_multiplier(
    river_score: float,
    tide_phase_score: float,
    tendency: Tendency,
    station_scores: list[StationRiskScore] | None = None,
    rain_72h_score: float = 0.0,
    rain_24h_score: float = 0.0,
    n_rising: int = 0,
) -> float:
    """
    Calculate conjunction multiplier for dangerous combinations (v3.0).

    Redesigned based on 14 annotated episodes. The key trigger is
    "active flood" pattern: multiple stations rising + recent rain,
    not just high raw values.

    Triggers:
    - Multiple stations rising simultaneously (strongest signal)
    - Rising + recent rain (active water arrival)
    - Rising + elevated river
    - High tide + elevated river
    - Sustained rain + elevated river
    - Multi-station above thresholds

    Returns multiplier between 1.0 and 2.5.
    """
    multiplier = 1.0

    # Count rising stations
    if station_scores:
        active = [s for s in station_scores if s.weight > 0]
        n_rising_local = sum(1 for s in active if s.rate_cm_per_hour > 1.0)
    else:
        n_rising_local = n_rising

    # === PRIMARY TRIGGER: active basin-wide flood ===
    # Multiple stations rising with rivers already elevated.
    # This is the single most discriminating pattern.
    # EP14: 5 rising + river 0.602 → 25cm flood (river above 0.55)
    # EP4: 5 rising + river 0.514 → NO flood (river below 0.55)
    # The 0.55 threshold corresponds to ~Pessac 3.5m (between yellow and orange).
    if n_rising_local >= 4 and river_score > 0.55:
        multiplier += 0.45  # Strong basin-wide flood signal
    elif n_rising_local >= 4 and river_score > 0.45:
        multiplier += 0.12  # Rivers rising but not yet critical
    elif n_rising_local >= 3 and river_score > 0.55:
        multiplier += 0.25

    # === SECONDARY: rising + rain + elevated river ===
    # Only significant when rivers are clearly elevated (>0.55)
    if tendency == Tendency.RISING and rain_24h_score > 0.3 and river_score > 0.55:
        multiplier += 0.20

    # === TERTIARY: sustained rain + elevated river ===
    if rain_72h_score > 0.5 and river_score > 0.55:
        multiplier += 0.15

    # === TIDE: high tide + elevated river ===
    if tide_phase_score > 0.5 and river_score > 0.5:
        multiplier += 0.15

    return min(multiplier, 2.5)


def _score_to_level(score: float) -> RiskLevel:
    """Convert numerical score (0-100) to risk level.

    v2.5: More aggressive thresholds - RED 24h before peak based on Feb 2024.
    - Green: < 15
    - Yellow: 15-35
    - Orange: 35-55
    - Red: >= 55
    """
    if score < 15:
        return RiskLevel.GREEN
    elif score < 35:
        return RiskLevel.YELLOW
    elif score < 55:
        return RiskLevel.ORANGE
    else:
        return RiskLevel.RED


def _generate_description(
    level: RiskLevel,
    factors: RiskFactors,
    primary_station: Optional[StationLevel],
) -> str:
    """Generate human-readable description of risk."""
    descriptions = {
        RiskLevel.GREEN: "Risque faible - conditions normales",
        RiskLevel.YELLOW: "Vigilance - conditions à surveiller",
        RiskLevel.ORANGE: "Risque modéré - conditions défavorables",
        RiskLevel.RED: "Risque élevé - conditions critiques",
    }

    desc = descriptions[level]

    # Add specific factors
    high_factors = []
    if factors.rain_24h_score > 0.5:
        high_factors.append("pluie 24h")
    if factors.rain_72h_score > 0.5:
        high_factors.append("pluie 72h")
    if factors.rain_7d_score > 0.5:
        high_factors.append("pluie 7j")
    if factors.river_level_score > 0.5:
        high_factors.append("niveau rivières")
    if factors.tide_score > 0.5:
        high_factors.append("coefficient marée")
    if factors.tide_phase_score > 0.7:
        high_factors.append("pleine mer")
    if factors.tendency_score > 0.5:
        high_factors.append("montée rapide")
    if factors.momentum_score > 0.5:
        high_factors.append("hausse multi-stations")
    if factors.wind_west_score > 0.5:
        high_factors.append("vent d'ouest")
    if factors.soil_saturation_factor > 1.3:
        high_factors.append("sol saturé")
    if factors.conjunction_multiplier > 1.2:
        conj_parts = []
        if factors.tide_phase_score > 0.6:
            conj_parts.append("marée haute")
        if factors.river_level_score > 0.5:
            conj_parts.append("niveaux élevés")
        if factors.tendency_score > 0.3:
            conj_parts.append("montée")
        # Check multi-station factor
        if factors.stations_detail:
            active = [s for s in factors.stations_detail if s.weight > 0]
            above_yellow = sum(1 for s in active if s.score > 0.33)
            if active and above_yellow / len(active) >= 0.6:
                conj_parts.append(f"{above_yellow} stations élevées")
        if conj_parts:
            high_factors.append(f"conjonction: {' + '.join(conj_parts)}")
        else:
            high_factors.append("conjonction défavorable")

    if high_factors:
        desc += f" ({', '.join(high_factors)})"

    # Add rate info if rising fast
    if primary_station and primary_station.rate_cm_per_hour > 5:
        desc += f" - Montée: {primary_station.rate_cm_per_hour:.0f} cm/h"

    return desc


def _generate_recommendations(
    level: RiskLevel,
    factors: RiskFactors,
    primary_station: Optional[StationLevel],
) -> list[str]:
    """Generate recommendations based on risk level and factors."""
    recommendations = []

    if level == RiskLevel.GREEN:
        recommendations.append("Aucune action nécessaire")
    elif level == RiskLevel.YELLOW:
        recommendations.append("Surveiller l'évolution des conditions")
        if factors.rain_24h_score > 0.4:
            recommendations.append("Vérifier les prévisions météo")
        if factors.tendency_score > 0.3:
            recommendations.append("Le niveau monte - surveiller régulièrement")
        if factors.soil_saturation_factor > 1.3:
            recommendations.append("Sol saturé - ruissellement accru")
    elif level == RiskLevel.ORANGE:
        recommendations.append("Préparer les mesures de protection")
        recommendations.append("Surveiller régulièrement le niveau d'eau")
        if factors.tide_phase_score > 0.6:
            recommendations.append("Attention à la prochaine pleine mer")
        if factors.conjunction_multiplier > 1.2:
            recommendations.append("Conjonction marée/niveau défavorable")
    else:  # RED
        recommendations.append("Mettre en place les protections")
        recommendations.append("Surveiller en continu")
        recommendations.append("Préparer l'évacuation des zones à risque")

    # Add upstream warnings (skip zero-weight / tidal stations)
    for station_score in factors.stations_detail:
        if station_score.score > 0.6 and station_score.weight > 0:
            # Find station config for delay
            settings = get_settings()
            station_config = next(
                (s for s in settings.hubeau_stations if s["id"] == station_score.station_id),
                None
            )
            if station_config:
                delay = station_config.get("delay_hours", 0)
                if delay > 0 and station_score.tendency == Tendency.RISING:
                    recommendations.append(
                        f"Niveau élevé à {station_score.station_name} - "
                        f"impact prévu dans ~{delay:.0f}h"
                    )

    return recommendations


async def calculate_risk_score_v2(
    stations_data: MultiStationData,
    tide: TideForecast,
    rainfall: RainfallSummary,
    video_level: Optional[float] = None,
    westerly_wind_kmh: float = 0.0,
    soil_saturation_factor: float = 1.0,
) -> RiskScore:
    """
    Calculate overall flood risk score using multi-station data.

    Weights (v2.3 - 10 factors, calibrated from Feb 2024 episode):
    - River levels (multi-station): 42%
    - Rainfall 24h: 8%
    - Rainfall 72h: 10%  (increased: sustained rain matters more)
    - Rainfall 7d: 5%    (increased: multi-day cumul is critical)
    - Tide coefficient: 8%
    - Tide phase: 8%
    - Tendency: 5%
    - Westerly wind: 3%
    - Soil saturation: 5% (increased: amplifies runoff)
    - Video level: 5% (if available)
    - Conjunction multiplier: up to 1.6x (multi-station factor added)

    Soil saturation also acts as multiplier on rain scores (like v1).
    """
    settings = get_settings()
    now = datetime.utcnow()

    # Get primary station for tendency
    primary_station = next(
        (s for s in stations_data.stations if s.station_id == stations_data.primary_station_id),
        stations_data.stations[0] if stations_data.stations else None,
    )
    primary_tendency = primary_station.tendency if primary_station else Tendency.STABLE

    # Calculate multi-station river score
    river_score, station_scores = _calculate_multi_station_score(stations_data)

    # Calculate tendency score
    tendency_score = _calculate_tendency_score(stations_data)

    # Calculate rainfall scores
    rain_24h_raw = rainfall.last_24h_mm + rainfall.forecast_24h_mm * 0.5
    rain_72h_raw = rainfall.last_72h_mm + rainfall.forecast_72h_mm * 0.3

    rain_24h_score = _normalize_score(
        rain_24h_raw,
        (settings.risk_rain_24h_yellow, settings.risk_rain_24h_orange, settings.risk_rain_24h_red),
    )

    rain_72h_score = _normalize_score(
        rain_72h_raw,
        (settings.risk_rain_24h_yellow * 2, settings.risk_rain_24h_orange * 2, settings.risk_rain_24h_red * 2),
    )

    # Apply soil saturation multiplier on rain scores (like v1)
    rain_24h_score = min(1.0, rain_24h_score * soil_saturation_factor)
    rain_72h_score = min(1.0, rain_72h_score * soil_saturation_factor)

    # Rain 7d score - thresholds: 60mm (yellow), 100mm (orange), 150mm (red)
    rain_7d_score = _normalize_score(
        rainfall.last_7d_mm,
        (60.0, 100.0, 150.0),
    )

    # Westerly wind score - 0 if <30 km/h, linear 30-100 -> 0-1
    if westerly_wind_kmh < 30:
        wind_west_score = 0.0
    elif westerly_wind_kmh >= 100:
        wind_west_score = 1.0
    else:
        wind_west_score = (westerly_wind_kmh - 30) / 70.0

    # Soil saturation score - (factor - 1.0) / 0.5 => 0 if dry, 1 if saturated
    soil_sat_score = min(1.0, max(0.0, (soil_saturation_factor - 1.0) / 0.5))

    # Tide coefficient score
    coef = tide.current_coefficient or 70
    tide_score = _normalize_score(coef, (70, settings.risk_tide_coef_high, 110))

    # Tide phase score
    tide_phase_score = _calculate_tide_phase_score(tide, now)

    # Calculate hours to high tide for raw value
    hours_to_high: Optional[float] = None
    if tide.next_high_tide:
        hours_to_high = (tide.next_high_tide.timestamp - now).total_seconds() / 3600

    # Get primary station rate for raw value
    primary_rate = primary_station.rate_cm_per_hour if primary_station else 0.0

    # Multi-station momentum score (v3.0 — gated by river level)
    momentum_score = _calculate_momentum_score(
        station_scores=station_scores, river_score=river_score,
    )

    # Conjunction multiplier (v3.0: active flood pattern detection)
    conjunction_multiplier = _calculate_conjunction_multiplier(
        river_score, tide_phase_score, primary_tendency, station_scores,
        rain_72h_score, rain_24h_score,
    )

    # Stability dampener (v3.0: high-but-stable = less risk)
    stability_dampener = _calculate_stability_dampener(
        river_score, primary_tendency, station_scores,
    )

    # Video level score (if available)
    video_score = (video_level / 100) if video_level is not None else None

    # Calculate weighted score (v3.0 - momentum added, river reduced)
    weights = {
        "river": 0.28,       # Reduced: level alone doesn't predict moulin flooding
        "rain_24h": 0.10,    # Increased: recent rain = water arriving
        "rain_72h": 0.12,    # Sustained rain matters
        "rain_7d": 0.05,
        "tide": 0.05,
        "tide_phase": 0.05,
        "tendency": 0.12,    # Increased: rising = danger signal
        "momentum": 0.08,    # NEW: multi-station rising simultaneously
        "wind_west": 0.03,
        "soil_sat": 0.05,
        "video": 0.05,
    }

    total_weight = sum(weights.values())
    if video_score is None:
        total_weight -= weights["video"]

    weighted_score = (
        river_score * weights["river"] +
        rain_24h_score * weights["rain_24h"] +
        rain_72h_score * weights["rain_72h"] +
        rain_7d_score * weights["rain_7d"] +
        tide_score * weights["tide"] +
        tide_phase_score * weights["tide_phase"] +
        tendency_score * weights["tendency"] +
        momentum_score * weights["momentum"] +
        wind_west_score * weights["wind_west"] +
        soil_sat_score * weights["soil_sat"]
    )

    if video_score is not None:
        weighted_score += video_score * weights["video"]

    # Apply conjunction multiplier (amplifies active flood patterns)
    weighted_score *= conjunction_multiplier

    # Apply stability dampener (reduces high-but-stable false positives)
    weighted_score *= stability_dampener

    # Normalize to 0-100
    final_score = min((weighted_score / total_weight) * 100, 100)

    # Determine level
    level = _score_to_level(final_score)

    factors = RiskFactors(
        rain_24h_score=rain_24h_score,
        rain_72h_score=rain_72h_score,
        rain_7d_score=rain_7d_score,
        river_level_score=river_score,
        tide_score=tide_score,
        tide_phase_score=tide_phase_score,
        wind_west_score=wind_west_score,
        soil_saturation_factor=soil_saturation_factor,
        video_level_score=video_score,
        tendency_score=tendency_score,
        momentum_score=momentum_score,
        conjunction_multiplier=conjunction_multiplier,
        stations_detail=station_scores,
        # Weights and raw values for detailed breakdown
        weights=RiskWeights(),
        rain_24h_raw_mm=rain_24h_raw,
        rain_72h_raw_mm=rain_72h_raw,
        tide_coefficient_raw=coef,
        tide_hours_to_high=hours_to_high,
        tendency_rate_cm_h=primary_rate,
    )

    risk_score = RiskScore(
        timestamp=now,
        level=level,
        score=round(final_score, 1),
        factors=factors,
        description=_generate_description(level, factors, primary_station),
        recommendations=_generate_recommendations(level, factors, primary_station),
    )

    # Store to InfluxDB
    await _store_risk_to_influx(risk_score)

    return risk_score


async def _store_risk_to_influx(risk: RiskScore) -> bool:
    """Store risk score to InfluxDB."""
    try:
        from app.services import influx_client

        return await influx_client.write_risk_score(
            score=risk.score,
            level=risk.level.value,
            timestamp=risk.timestamp,
            factors={
                "rain_24h": risk.factors.rain_24h_score,
                "rain_72h": risk.factors.rain_72h_score,
                "rain_7d": risk.factors.rain_7d_score,
                "river_level": risk.factors.river_level_score,
                "tide": risk.factors.tide_score,
                "tide_phase": risk.factors.tide_phase_score,
                "tendency": risk.factors.tendency_score or 0,
                "wind_west": risk.factors.wind_west_score,
                "soil_sat": (risk.factors.soil_saturation_factor - 1.0) / 0.5,
            },
        )
    except Exception as e:
        print(f"Error storing risk score to InfluxDB: {e}")
        return False


async def calculate_risk_from_values(
    rain_24h_mm: float,
    rain_72h_mm: float,
    dordogne_level_m: float,
    tide_coefficient: int,
    hours_to_high_tide: float,
    rate_cm_per_hour: float = 0.0,
    video_level: Optional[float] = None,
    override_timestamp: Optional[datetime] = None,
    n_rising_stations: int = 0,
    n_total_stations: int = 6,
) -> RiskScore:
    """Calculate risk from explicit values (for simulation/testing/backfill).

    v3.0: Unified weights with calculate_risk_score_v2. Added momentum and
    stability dampener support via n_rising_stations parameter.
    """
    settings = get_settings()
    now = override_timestamp or datetime.utcnow()

    # Calculate individual scores
    rain_24h_score = _normalize_score(
        rain_24h_mm,
        (settings.risk_rain_24h_yellow, settings.risk_rain_24h_orange, settings.risk_rain_24h_red),
    )

    rain_72h_score = _normalize_score(
        rain_72h_mm,
        (settings.risk_rain_24h_yellow * 2, settings.risk_rain_24h_orange * 2, settings.risk_rain_24h_red * 2),
    )

    river_score = _normalize_score(
        dordogne_level_m,
        (settings.risk_dordogne_level_yellow, settings.risk_dordogne_level_orange, settings.risk_dordogne_level_red),
    )

    tide_score = _normalize_score(tide_coefficient, (70, settings.risk_tide_coef_high, 110))

    # Tide phase from hours
    if abs(hours_to_high_tide) <= 1:
        tide_phase_score = 1.0
    elif abs(hours_to_high_tide) <= 2:
        tide_phase_score = 0.8
    elif abs(hours_to_high_tide) <= 3:
        tide_phase_score = 0.5
    else:
        tide_phase_score = 0.2

    # Tendency score from rate (v3.0: same as _calculate_tendency_score)
    if rate_cm_per_hour <= 0:
        tendency_score = 0.0
    elif rate_cm_per_hour < 2:
        tendency_score = 0.3
    elif rate_cm_per_hour < 5:
        tendency_score = 0.5
    elif rate_cm_per_hour < 10:
        tendency_score = 0.7
    elif rate_cm_per_hour < 20:
        tendency_score = 0.85
    else:
        tendency_score = 1.0

    tendency = Tendency.RISING if rate_cm_per_hour > 1 else (
        Tendency.FALLING if rate_cm_per_hour < -1 else Tendency.STABLE
    )

    # Momentum score (v3.0 — gated by river level)
    momentum_score = _calculate_momentum_score(
        n_rising=n_rising_stations, n_total=n_total_stations,
        river_score=river_score,
    )

    # Conjunction multiplier (v3.0)
    conjunction_multiplier = _calculate_conjunction_multiplier(
        river_score, tide_phase_score, tendency,
        rain_72h_score=rain_72h_score,
        rain_24h_score=rain_24h_score,
        n_rising=n_rising_stations,
    )

    # Stability dampener (v3.0)
    stability_dampener = _calculate_stability_dampener(
        river_score, tendency, n_rising=n_rising_stations,
    )

    video_score = (video_level / 100) if video_level is not None else None

    # Unified weights (v3.0 - same as calculate_risk_score_v2)
    weights = {
        "river": 0.28,
        "rain_24h": 0.10,
        "rain_72h": 0.12,
        "rain_7d": 0.0,  # Not available in simple values mode
        "tide": 0.05,
        "tide_phase": 0.05,
        "tendency": 0.12,
        "momentum": 0.08,
        "wind_west": 0.0,  # Not available in simple values mode
        "soil_sat": 0.0,   # Not available in simple values mode
        "video": 0.05,
    }
    total_weight = sum(weights.values()) - (weights["video"] if video_score is None else 0)

    weighted_score = (
        river_score * weights["river"] +
        rain_24h_score * weights["rain_24h"] +
        rain_72h_score * weights["rain_72h"] +
        tide_score * weights["tide"] +
        tide_phase_score * weights["tide_phase"] +
        tendency_score * weights["tendency"] +
        momentum_score * weights["momentum"]
    )
    if video_score is not None:
        weighted_score += video_score * weights["video"]

    weighted_score *= conjunction_multiplier
    weighted_score *= stability_dampener
    final_score = min((weighted_score / total_weight) * 100, 100)
    level = _score_to_level(final_score)

    factors = RiskFactors(
        rain_24h_score=rain_24h_score,
        rain_72h_score=rain_72h_score,
        river_level_score=river_score,
        tide_score=tide_score,
        tide_phase_score=tide_phase_score,
        video_level_score=video_score,
        tendency_score=tendency_score,
        momentum_score=momentum_score,
        conjunction_multiplier=conjunction_multiplier,
        stations_detail=[],
        # Weights and raw values for detailed breakdown
        weights=RiskWeights(),
        rain_24h_raw_mm=rain_24h_mm,
        rain_72h_raw_mm=rain_72h_mm,
        tide_coefficient_raw=tide_coefficient,
        tide_hours_to_high=hours_to_high_tide,
        tendency_rate_cm_h=rate_cm_per_hour,
    )

    return RiskScore(
        timestamp=now,
        level=level,
        score=round(final_score, 1),
        factors=factors,
        description=_generate_description(level, factors, None),
        recommendations=_generate_recommendations(level, factors, None),
    )


async def backfill_risk_scores(start: datetime, end: datetime) -> int:
    """Backfill risk scores by recalculating from historical data in InfluxDB.

    v2.2: Uses weighted multi-station river levels (excluding tidal stations
    like Libourne) instead of a single primary station.

    For each time step in the period:
    1. Reads river levels from ALL non-tidal stations, rainfall, and tide data
    2. Computes weighted average river level + rate from primary station
    3. Calculates the risk score via calculate_risk_from_values()
    4. Stores the result back to InfluxDB

    Resolution adapts to the period length:
    - <= 7 days: hourly
    - <= 30 days: every 3 hours
    - > 30 days: every 6 hours

    Prerequisites: river, rainfall, and tide data must already exist in InfluxDB
    for the requested period (call _ensure_* functions first).
    """
    from app.services import influx_client

    settings = get_settings()
    primary_station_id = settings.hubeau_primary_station  # P555001001 (Pessac)

    s = start.replace(tzinfo=None) if start.tzinfo else start
    e = end.replace(tzinfo=None) if end.tzinfo else end

    # Adaptive resolution
    period_days = (e - s).days
    if period_days <= 7:
        step = timedelta(hours=1)
    elif period_days <= 30:
        step = timedelta(hours=3)
    else:
        step = timedelta(hours=6)

    # --- Bulk pre-fetch all data for the full period ---
    lookback = timedelta(hours=72)
    rain_start = s - lookback

    print(f"Risk backfill v2.2: loading data for {s} -> {e} (step={step})...")

    # River levels — fetch ALL stations with weight > 0
    station_river_data: dict[str, list[dict]] = {}
    station_weights: dict[str, float] = {}
    station_thresholds: dict[str, tuple[float, float, float]] = {}

    for station_cfg in settings.hubeau_stations:
        w = station_cfg.get("weight", 0)
        if w <= 0:
            continue  # Skip tidal/zero-weight stations
        sid = station_cfg["id"]
        station_weights[sid] = w
        th = station_cfg.get("thresholds", {})
        station_thresholds[sid] = (
            th.get("yellow", 3.0),
            th.get("orange", 4.0),
            th.get("red", 5.0),
        )

        data = await influx_client.query_river_levels(
            sid, s - timedelta(hours=12), e + timedelta(hours=1)
        )
        points = []
        for r in data:
            ts = r["timestamp"]
            if hasattr(ts, "tzinfo") and ts.tzinfo:
                ts = ts.replace(tzinfo=None)
            points.append({"timestamp": ts, "height_m": r["height_m"]})
        points.sort(key=lambda p: p["timestamp"])
        if points:
            station_river_data[sid] = points

    if not station_river_data:
        print("Risk backfill v2.2: no river data for any station, cannot compute scores")
        return 0

    # Primary station data for tendency/rate calculation
    primary_points = station_river_data.get(primary_station_id, [])
    if not primary_points:
        # Fallback to highest-weight station with data
        fallback_sid = max(
            station_river_data.keys(),
            key=lambda sid: station_weights.get(sid, 0),
        )
        primary_points = station_river_data[fallback_sid]
        print(f"Risk backfill v2.2: primary {primary_station_id} has no data, using {fallback_sid}")

    # Rainfall (need 72h lookback from start)
    rainfall_data = await influx_client.query_rainfall(rain_start, e)
    rain_by_hour: dict[datetime, list[float]] = {}
    for r in rainfall_data:
        precip = r.get("precipitation_mm_h")
        if precip is None:
            continue
        ts = r["timestamp"]
        if hasattr(ts, "tzinfo") and ts.tzinfo:
            ts = ts.replace(tzinfo=None)
        bucket = ts.replace(minute=0, second=0, microsecond=0)
        rain_by_hour.setdefault(bucket, []).append(precip)

    rain_hourly = {
        bucket: sum(vals) / len(vals)
        for bucket, vals in rain_by_hour.items()
    }

    # Tide data (try BORDEAUX first, then LIBOURNE)
    tide_heights: list[dict] = []
    tide_coefficients: list[dict] = []
    for port in ["BORDEAUX", "LIBOURNE"]:
        tide_raw = await influx_client.query_tides(
            port, s - timedelta(hours=12), e + timedelta(hours=12)
        )
        for t in tide_raw:
            ts = t["timestamp"]
            if hasattr(ts, "tzinfo") and ts.tzinfo:
                ts = ts.replace(tzinfo=None)
            if t.get("height_m") is not None:
                tide_heights.append({"timestamp": ts, "height_m": t["height_m"]})
            if t.get("coefficient") is not None:
                tide_coefficients.append({"timestamp": ts, "coefficient": t["coefficient"]})
        if tide_heights:
            break

    tide_heights.sort(key=lambda t: t["timestamp"])
    tide_coefficients.sort(key=lambda t: t["timestamp"])

    # Pre-compute high tide events (local maxima in tide heights)
    high_tide_times: list[datetime] = []
    for i in range(1, len(tide_heights) - 1):
        if (tide_heights[i]["height_m"] >= tide_heights[i - 1]["height_m"] and
                tide_heights[i]["height_m"] >= tide_heights[i + 1]["height_m"]):
            high_tide_times.append(tide_heights[i]["timestamp"])

    total_river_pts = sum(len(pts) for pts in station_river_data.values())
    print(f"Risk backfill v2.2: {len(station_river_data)} stations, "
          f"{total_river_pts} river pts, "
          f"{len(rain_hourly)} rain hours, "
          f"{len(tide_heights)} tide pts, "
          f"{len(high_tide_times)} high tides detected")

    # --- Iterate through time steps ---
    stored = 0
    current = s

    while current <= e:
        # 1. Weighted multi-station river level score
        weighted_score_sum = 0.0
        total_weight = 0.0

        for sid, points in station_river_data.items():
            w = station_weights.get(sid, 0)
            if w <= 0:
                continue
            # Find closest point
            closest = min(points, key=lambda p: abs((p["timestamp"] - current).total_seconds()))
            dist_h = abs((closest["timestamp"] - current).total_seconds()) / 3600
            if dist_h > 48:
                continue
            # Normalize to 0-1 using station thresholds
            score = _normalize_score(closest["height_m"], station_thresholds[sid])
            weighted_score_sum += score * w
            total_weight += w

        if total_weight == 0:
            current += step
            continue

        # Convert weighted river score back to a "virtual level" for calculate_risk_from_values
        # The river score is already 0-1, multiply by red threshold to get an equivalent level
        river_score_01 = weighted_score_sum / total_weight
        # Map back to the default thresholds used by calculate_risk_from_values
        # score 0->0m, 0.33->yellow(3.0), 0.66->orange(4.0), 1.0->red(5.0)
        if river_score_01 <= 0.33:
            virtual_level = (river_score_01 / 0.33) * settings.risk_dordogne_level_yellow
        elif river_score_01 <= 0.66:
            virtual_level = settings.risk_dordogne_level_yellow + (
                (river_score_01 - 0.33) / 0.33
            ) * (settings.risk_dordogne_level_orange - settings.risk_dordogne_level_yellow)
        else:
            virtual_level = settings.risk_dordogne_level_orange + (
                (river_score_01 - 0.66) / 0.34
            ) * (settings.risk_dordogne_level_red - settings.risk_dordogne_level_orange)

        # 2. Rate of change from primary station + count rising stations
        rate_cm_h = 0.0
        n_rising = 0
        n_stations_with_data = 0

        def _calc_rate(points_list, t):
            """Calculate rate of change at time t from a sorted list of points."""
            before = [p for p in points_list if p["timestamp"] <= t]
            after = [p for p in points_list if p["timestamp"] > t]
            if before and after:
                p0, p1 = before[-1], after[0]
                dt_h = (p1["timestamp"] - p0["timestamp"]).total_seconds() / 3600
                if 0.1 < dt_h < 48:
                    return (p1["height_m"] - p0["height_m"]) * 100 / dt_h
            elif len(before) >= 2:
                p0, p1 = before[-2], before[-1]
                dt_h = (p1["timestamp"] - p0["timestamp"]).total_seconds() / 3600
                if 0.1 < dt_h < 48:
                    return (p1["height_m"] - p0["height_m"]) * 100 / dt_h
            return 0.0

        # Count rising stations across ALL stations (v3.0: momentum)
        for sid, points in station_river_data.items():
            w = station_weights.get(sid, 0)
            if w <= 0:
                continue
            closest = min(points, key=lambda p: abs((p["timestamp"] - current).total_seconds()))
            dist_h = abs((closest["timestamp"] - current).total_seconds()) / 3600
            if dist_h > 48:
                continue
            n_stations_with_data += 1
            station_rate = _calc_rate(points, current)
            if station_rate > 1.0:  # Rising threshold: >1 cm/h
                n_rising += 1

        # Primary station rate
        if primary_points:
            rate_cm_h = _calc_rate(primary_points, current)

        # 3. Cumulative rainfall (24h and 72h)
        rain_24h = 0.0
        rain_72h = 0.0
        for h in range(72):
            bucket = (current - timedelta(hours=h)).replace(minute=0, second=0, microsecond=0)
            mm = rain_hourly.get(bucket, 0.0)
            if h < 24:
                rain_24h += mm
            rain_72h += mm

        # 4. Tide coefficient
        tide_coef = 70
        if tide_coefficients:
            closest_coef = min(
                tide_coefficients,
                key=lambda t: abs((t["timestamp"] - current).total_seconds()),
            )
            if abs((closest_coef["timestamp"] - current).total_seconds()) < 48 * 3600:
                tide_coef = int(closest_coef["coefficient"])

        # 5. Hours to closest high tide
        hours_to_high = 6.0
        if high_tide_times:
            closest_ht = min(
                high_tide_times,
                key=lambda ht: abs((ht - current).total_seconds()),
            )
            hours_to_high = (closest_ht - current).total_seconds() / 3600

        # 6. Calculate risk score (v3.0: pass momentum data)
        risk = await calculate_risk_from_values(
            rain_24h_mm=rain_24h,
            rain_72h_mm=rain_72h,
            dordogne_level_m=virtual_level,
            tide_coefficient=tide_coef,
            hours_to_high_tide=hours_to_high,
            rate_cm_per_hour=rate_cm_h,
            override_timestamp=current,
            n_rising_stations=n_rising,
            n_total_stations=max(n_stations_with_data, 1),
        )

        # 7. Store to InfluxDB
        success = await influx_client.write_risk_score(
            score=risk.score,
            level=risk.level.value,
            timestamp=current,
            factors={
                "rain_24h": risk.factors.rain_24h_score,
                "rain_72h": risk.factors.rain_72h_score,
                "river_level": risk.factors.river_level_score,
                "tide": risk.factors.tide_score,
                "tide_phase": risk.factors.tide_phase_score,
                "tendency": risk.factors.tendency_score or 0,
            },
        )

        if success:
            stored += 1

        current += step

    print(f"Risk backfill v2.2: stored {stored} scores for {s} -> {e}")
    return stored
