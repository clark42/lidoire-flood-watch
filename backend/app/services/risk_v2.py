"""Enhanced risk scoring service with multi-station support.

Calculates flood risk based on:
- Multiple river stations (Dordogne, Isle, Dronne)
- Rate of water level change (tendency)
- Tide coefficient and phase
- Conjunction factor (tide + high river = multiplier)
- Rainfall (24h, 72h cumulative)
- Video analysis (water level index)

Risk model weights (v2):
- Multi-station river levels: 50% (distributed by station weight)
- Tide (coefficient + phase): 20%
- Rainfall: 20%
- Tendency bonus: 5%
- Video analysis: 5%
- Conjunction multiplier: up to 1.5x
"""

from datetime import datetime, timedelta
from typing import Optional

from app.config import get_settings
from app.models.schemas import (
    RiskScore,
    RiskLevel,
    RiskFactors,
    StationRiskScore,
    TideForecast,
    RainfallSummary,
    MultiStationData,
    StationLevel,
    TideType,
    Tendency,
)


def _normalize_score(value: float, thresholds: tuple[float, float, float]) -> float:
    """
    Normalize a value to 0-1 based on thresholds.

    thresholds = (yellow, orange, red)
    Returns:
    - 0.0-0.33: below yellow
    - 0.33-0.66: yellow to orange
    - 0.66-1.0: orange to red and above
    """
    yellow, orange, red = thresholds

    if value <= 0:
        return 0.0
    elif value < yellow:
        return (value / yellow) * 0.33
    elif value < orange:
        return 0.33 + ((value - yellow) / (orange - yellow)) * 0.33
    elif value < red:
        return 0.66 + ((value - orange) / (red - orange)) * 0.34
    else:
        return 1.0


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

    Returns:
        Tuple of (combined_score, list of per-station scores)
    """
    station_scores = []
    total_weighted_score = 0.0
    total_weight = 0.0

    for station in stations_data.stations:
        score = _calculate_station_score(station)
        station_scores.append(score)
        total_weighted_score += score.weighted_score
        total_weight += station.weight

    # Normalize to 0-1
    combined_score = total_weighted_score / total_weight if total_weight > 0 else 0.0

    return combined_score, station_scores


def _calculate_tendency_score(stations_data: MultiStationData) -> float:
    """
    Calculate risk bonus from rate of rise.

    Fast rising water is more dangerous than stable high water.
    """
    # Focus on primary station and nearby stations
    primary_id = stations_data.primary_station_id
    primary_station = next(
        (s for s in stations_data.stations if s.station_id == primary_id),
        None
    )

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
) -> float:
    """
    Calculate conjunction multiplier for dangerous combinations.

    When tide is high AND river is high AND rising, risk increases exponentially.
    Returns multiplier between 1.0 and 1.5.
    """
    # Base multiplier
    multiplier = 1.0

    # High tide + high river = danger
    if tide_phase_score > 0.6 and river_score > 0.5:
        multiplier += 0.2

    # Add more if rising
    if tendency == Tendency.RISING:
        if tide_phase_score > 0.6 and river_score > 0.5:
            multiplier += 0.2  # Rising during high tide with high river
        elif river_score > 0.6:
            multiplier += 0.1  # Rising with high river

    return min(multiplier, 1.5)


def _score_to_level(score: float) -> RiskLevel:
    """Convert numerical score (0-100) to risk level."""
    if score < 25:
        return RiskLevel.GREEN
    elif score < 50:
        return RiskLevel.YELLOW
    elif score < 75:
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
    if factors.river_level_score > 0.5:
        high_factors.append("niveau rivières")
    if factors.tide_score > 0.5:
        high_factors.append("coefficient marée")
    if factors.tide_phase_score > 0.7:
        high_factors.append("pleine mer")
    if factors.tendency_score > 0.5:
        high_factors.append("montée rapide")
    if factors.conjunction_multiplier > 1.2:
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

    # Add upstream warnings
    for station_score in factors.stations_detail:
        if station_score.score > 0.6 and station_score.station_id != "P577001001":
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
) -> RiskScore:
    """
    Calculate overall flood risk score using multi-station data.

    Weights (v2):
    - River levels (multi-station): 50%
    - Rainfall 24h: 12%
    - Rainfall 72h: 8%
    - Tide coefficient: 12%
    - Tide phase: 8%
    - Tendency: 5%
    - Video level: 5% (if available)

    Plus conjunction multiplier (up to 1.5x)
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
    rain_24h_score = _normalize_score(
        rainfall.last_24h_mm + rainfall.forecast_24h_mm * 0.5,
        (settings.risk_rain_24h_yellow, settings.risk_rain_24h_orange, settings.risk_rain_24h_red),
    )

    rain_72h_score = _normalize_score(
        rainfall.last_72h_mm + rainfall.forecast_72h_mm * 0.3,
        (settings.risk_rain_24h_yellow * 2, settings.risk_rain_24h_orange * 2, settings.risk_rain_24h_red * 2),
    )

    # Tide coefficient score
    coef = tide.current_coefficient or 70
    tide_score = _normalize_score(coef, (70, settings.risk_tide_coef_high, 110))

    # Tide phase score
    tide_phase_score = _calculate_tide_phase_score(tide, now)

    # Conjunction multiplier
    conjunction_multiplier = _calculate_conjunction_multiplier(
        river_score, tide_phase_score, primary_tendency
    )

    # Video level score (if available)
    video_score = (video_level / 100) if video_level is not None else None

    # Calculate weighted score
    weights = {
        "river": 0.50,
        "rain_24h": 0.12,
        "rain_72h": 0.08,
        "tide": 0.12,
        "tide_phase": 0.08,
        "tendency": 0.05,
        "video": 0.05,
    }

    total_weight = sum(weights.values())
    if video_score is None:
        total_weight -= weights["video"]

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

    # Apply conjunction multiplier
    weighted_score *= conjunction_multiplier

    # Normalize to 0-100
    final_score = min((weighted_score / total_weight) * 100, 100)

    # Determine level
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
        stations_detail=station_scores,
    )

    return RiskScore(
        timestamp=now,
        level=level,
        score=round(final_score, 1),
        factors=factors,
        description=_generate_description(level, factors, primary_station),
        recommendations=_generate_recommendations(level, factors, primary_station),
    )


async def calculate_risk_from_values(
    rain_24h_mm: float,
    rain_72h_mm: float,
    dordogne_level_m: float,
    tide_coefficient: int,
    hours_to_high_tide: float,
    rate_cm_per_hour: float = 0.0,
    video_level: Optional[float] = None,
) -> RiskScore:
    """Calculate risk from explicit values (for simulation/testing)."""
    settings = get_settings()
    now = datetime.utcnow()

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
    )

    return RiskScore(
        timestamp=now,
        level=level,
        score=round(final_score, 1),
        factors=factors,
        description=_generate_description(level, factors, None),
        recommendations=_generate_recommendations(level, factors, None),
    )
