"""Enhanced risk scoring service with multi-station support.

Calculates flood risk based on:
- Multiple river stations (Dordogne, Isle, Dronne)
- Rate of water level change (tendency)
- Tide coefficient and phase
- Conjunction factor (tide + high river = multiplier)
- Rainfall (24h, 72h, 7d cumulative)
- Westerly wind (surge amplification on Gironde estuary)
- Soil saturation (runoff factor from Open-Meteo + Meteo Agricole)
- Video analysis (water level index)

Risk model weights (v2.3 - 10 factors):
- Multi-station river levels: 42% (Libourne excluded — tidal)
- Rainfall (24h + 72h + 7d): 23% (72h/7d increased per Feb 2024 analysis)
- Tide (coefficient + phase): 16%
- Tendency: 5%
- Soil saturation: 5% (increased per Feb 2024 analysis)
- Westerly wind: 3%
- Video analysis: 5%
- Conjunction multiplier: up to 1.6x (multi-station factor added)

v2.3 changes (Jan 2026):
- Based on analysis of episode fevrier 2024 (id=2, severity 5, moulin inonde).
- Increased rain_72h weight (7%->10%) and rain_7d (3%->5%): sustained
  multi-day rain (62mm/5d) caused flooding despite moderate 24h rain (34mm).
- Added multi-station simultaneous elevation to conjunction multiplier:
  all 7 stations above yellow simultaneously is a critical signal.
- Increased soil_sat weight (4%->5%): winter saturated soil amplifies risk.
- Conjunction multiplier max raised to 1.6x.

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
        # score^0.7 makes high values contribute more
        # e.g., 0.5 -> 0.62, 0.8 -> 0.86, 0.33 -> 0.48
        return linear ** 0.7
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

    # Score based on rate of rise
    if rate <= 0:
        return 0.0  # Falling or stable
    elif rate < 5:
        return 0.2  # Slow rise
    elif rate < 10:
        return 0.4  # Moderate rise
    elif rate < 20:
        return 0.6  # Fast rise
    elif rate < 50:
        return 0.8  # Very fast rise
    else:
        return 1.0  # Extreme rise


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


def _calculate_conjunction_multiplier(
    river_score: float,
    tide_phase_score: float,
    tendency: Tendency,
    station_scores: list[StationRiskScore] | None = None,
    rain_72h_score: float = 0.0,
) -> float:
    """
    Calculate conjunction multiplier for dangerous combinations.

    When multiple risk factors align, danger increases exponentially.
    v2.4: More aggressive multiplier based on Feb 2024 analysis.

    Triggers:
    - High river levels (>0.4 score)
    - Rising tendency
    - Multiple stations above yellow simultaneously
    - Sustained rain (72h cumul > 0.4)
    - High tide phase (when available)

    Returns multiplier between 1.0 and 2.0.
    """
    multiplier = 1.0

    # Rising river = increased risk
    if tendency == Tendency.RISING:
        if river_score > 0.5:
            multiplier += 0.25  # Rising with elevated river
        elif river_score > 0.3:
            multiplier += 0.15  # Rising with moderate river

    # High tide + elevated river (when tide data available)
    if tide_phase_score > 0.5 and river_score > 0.4:
        multiplier += 0.2

    # Sustained rain + elevated river = soil saturated, more runoff coming
    if rain_72h_score > 0.4 and river_score > 0.4:
        multiplier += 0.15

    # Multi-station simultaneous elevation (basin-wide flood signal)
    if station_scores:
        active_stations = [s for s in station_scores if s.weight > 0]
        if active_stations:
            above_yellow = sum(1 for s in active_stations if s.score > 0.33)
            above_orange = sum(1 for s in active_stations if s.score > 0.5)
            ratio = above_yellow / len(active_stations)

            if ratio >= 0.7:  # 70%+ stations above yellow
                multiplier += 0.20
                if above_orange >= 2:  # Plus 2+ stations near orange
                    multiplier += 0.15
            elif ratio >= 0.5:  # 50%+ stations above yellow
                multiplier += 0.10

    return min(multiplier, 2.0)


def _score_to_level(score: float) -> RiskLevel:
    """Convert numerical score (0-100) to risk level.

    v2.4: Adjusted thresholds for earlier warnings based on Feb 2024 analysis.
    - Green: < 20 (was 25)
    - Yellow: 20-45 (was 25-50)
    - Orange: 45-70 (was 50-75)
    - Red: >= 70 (was 75)
    """
    if score < 20:
        return RiskLevel.GREEN
    elif score < 45:
        return RiskLevel.YELLOW
    elif score < 70:
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

    # Conjunction multiplier (v2.4: includes rain_72h and more aggressive triggers)
    conjunction_multiplier = _calculate_conjunction_multiplier(
        river_score, tide_phase_score, primary_tendency, station_scores, rain_72h_score
    )

    # Video level score (if available)
    video_score = (video_level / 100) if video_level is not None else None

    # Calculate weighted score (v2.3 - 10 factors, calibrated Feb 2024)
    weights = {
        "river": 0.42,
        "rain_24h": 0.08,
        "rain_72h": 0.10,
        "rain_7d": 0.05,
        "tide": 0.08,
        "tide_phase": 0.08,
        "tendency": 0.05,
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
        wind_west_score * weights["wind_west"] +
        soil_sat_score * weights["soil_sat"]
    )

    if video_score is not None:
        weighted_score += video_score * weights["video"]

    # Apply conjunction multiplier
    weighted_score *= conjunction_multiplier

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
) -> RiskScore:
    """Calculate risk from explicit values (for simulation/testing/backfill)."""
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

    # Tendency score from rate
    if rate_cm_per_hour <= 0:
        tendency_score = 0.0
    elif rate_cm_per_hour < 10:
        tendency_score = 0.3
    elif rate_cm_per_hour < 20:
        tendency_score = 0.6
    else:
        tendency_score = 1.0

    tendency = Tendency.RISING if rate_cm_per_hour > 5 else Tendency.STABLE

    # Conjunction multiplier
    conjunction_multiplier = _calculate_conjunction_multiplier(
        river_score, tide_phase_score, tendency
    )

    video_score = (video_level / 100) if video_level is not None else None

    # Weighted calculation
    weights = {
        "river": 0.50,
        "rain_24h": 0.12,
        "rain_72h": 0.08,
        "tide": 0.12,
        "tide_phase": 0.08,
        "tendency": 0.05,
        "video": 0.05,
    }
    total_weight = sum(weights.values()) - (weights["video"] if video_score is None else 0)

    weighted_score = (
        river_score * weights["river"] +
        rain_24h_score * weights["rain_24h"] +
        rain_72h_score * weights["rain_72h"] +
        tide_score * weights["tide"] +
        tide_phase_score * weights["tide_phase"] +
        tendency_score * weights["tendency"]
    )
    if video_score is not None:
        weighted_score += video_score * weights["video"]

    weighted_score *= conjunction_multiplier
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

        # 2. Rate of change from primary station
        rate_cm_h = 0.0
        if primary_points:
            before = [p for p in primary_points if p["timestamp"] <= current]
            after = [p for p in primary_points if p["timestamp"] > current]
            if before and after:
                p0, p1 = before[-1], after[0]
                dt_h = (p1["timestamp"] - p0["timestamp"]).total_seconds() / 3600
                if 0.1 < dt_h < 48:
                    rate_cm_h = (p1["height_m"] - p0["height_m"]) * 100 / dt_h
            elif len(before) >= 2:
                p0, p1 = before[-2], before[-1]
                dt_h = (p1["timestamp"] - p0["timestamp"]).total_seconds() / 3600
                if 0.1 < dt_h < 48:
                    rate_cm_h = (p1["height_m"] - p0["height_m"]) * 100 / dt_h

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

        # 6. Calculate risk score
        risk = await calculate_risk_from_values(
            rain_24h_mm=rain_24h,
            rain_72h_mm=rain_72h,
            dordogne_level_m=virtual_level,
            tide_coefficient=tide_coef,
            hours_to_high_tide=hours_to_high,
            rate_cm_per_hour=rate_cm_h,
            override_timestamp=current,
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
