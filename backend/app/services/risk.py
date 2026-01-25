"""Risk scoring service.

Calculates flood risk based on:
- Rainfall (24h, 72h cumulative)
- River level (Dordogne at Libourne)
- Tide (coefficient and phase)
- Video analysis (water level index)
"""

from datetime import datetime, timedelta
from typing import Optional

from app.config import get_settings
from app.models.schemas import (
    RiskScore,
    RiskLevel,
    RiskFactors,
    TideForecast,
    RiverLevelForecast,
    RainfallSummary,
    TideType,
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
    # Full tidal cycle is ~12.4 hours, so max distance is ~6.2 hours
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


def _generate_description(level: RiskLevel, factors: RiskFactors) -> str:
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
        high_factors.append("niveau Dordogne")
    if factors.tide_score > 0.5:
        high_factors.append("coefficient marée")
    if factors.tide_phase_score > 0.7:
        high_factors.append("pleine mer")

    if high_factors:
        desc += f" ({', '.join(high_factors)})"

    return desc


def _generate_recommendations(level: RiskLevel, factors: RiskFactors) -> list[str]:
    """Generate recommendations based on risk level and factors."""
    recommendations = []

    if level == RiskLevel.GREEN:
        recommendations.append("Aucune action nécessaire")
    elif level == RiskLevel.YELLOW:
        recommendations.append("Surveiller l'évolution des conditions")
        if factors.rain_24h_score > 0.4:
            recommendations.append("Vérifier les prévisions météo")
    elif level == RiskLevel.ORANGE:
        recommendations.append("Préparer les mesures de protection")
        recommendations.append("Surveiller régulièrement le niveau d'eau")
        if factors.tide_phase_score > 0.6:
            recommendations.append("Attention à la prochaine pleine mer")
    else:  # RED
        recommendations.append("Mettre en place les protections")
        recommendations.append("Surveiller en continu")
        recommendations.append("Préparer l'évacuation des zones à risque")

    return recommendations


async def calculate_risk_score(
    tide: TideForecast,
    river: RiverLevelForecast,
    rainfall: RainfallSummary,
    video_level: Optional[float] = None,
) -> RiskScore:
    """
    Calculate overall flood risk score.

    Weights:
    - Rainfall 24h: 25%
    - Rainfall 72h: 15%
    - River level: 30%
    - Tide coefficient: 15%
    - Tide phase: 10%
    - Video level: 5% (if available)
    """
    settings = get_settings()
    now = datetime.utcnow()

    # Calculate individual scores (0-1)
    rain_24h_score = _normalize_score(
        rainfall.last_24h_mm + rainfall.forecast_24h_mm * 0.5,  # Weight forecast less
        (settings.risk_rain_24h_yellow, settings.risk_rain_24h_orange, settings.risk_rain_24h_red),
    )

    rain_72h_score = _normalize_score(
        rainfall.last_72h_mm + rainfall.forecast_72h_mm * 0.3,
        (settings.risk_rain_24h_yellow * 2, settings.risk_rain_24h_orange * 2, settings.risk_rain_24h_red * 2),
    )

    river_score = _normalize_score(
        river.current.height_m,
        (settings.risk_dordogne_level_yellow, settings.risk_dordogne_level_orange, settings.risk_dordogne_level_red),
    )

    # Tide coefficient score
    coef = tide.current_coefficient or 70
    tide_score = _normalize_score(
        coef,
        (70, settings.risk_tide_coef_high, 110),
    )

    # Tide phase score
    tide_phase_score = _calculate_tide_phase_score(tide, now)

    # Video level score (if available)
    video_score = (video_level / 100) if video_level is not None else None

    # Calculate weighted score
    weights = {
        "rain_24h": 0.25,
        "rain_72h": 0.15,
        "river": 0.30,
        "tide": 0.15,
        "tide_phase": 0.10,
        "video": 0.05,
    }

    total_weight = sum(weights.values())
    if video_score is None:
        # Redistribute video weight to other factors
        total_weight -= weights["video"]

    weighted_score = (
        rain_24h_score * weights["rain_24h"] +
        rain_72h_score * weights["rain_72h"] +
        river_score * weights["river"] +
        tide_score * weights["tide"] +
        tide_phase_score * weights["tide_phase"]
    )

    if video_score is not None:
        weighted_score += video_score * weights["video"]

    # Normalize to 0-100
    final_score = (weighted_score / total_weight) * 100

    # Determine level
    level = _score_to_level(final_score)

    factors = RiskFactors(
        rain_24h_score=rain_24h_score,
        rain_72h_score=rain_72h_score,
        river_level_score=river_score,
        tide_score=tide_score,
        tide_phase_score=tide_phase_score,
        video_level_score=video_score,
    )

    return RiskScore(
        timestamp=now,
        level=level,
        score=round(final_score, 1),
        factors=factors,
        description=_generate_description(level, factors),
        recommendations=_generate_recommendations(level, factors),
    )


async def calculate_risk_from_values(
    rain_24h_mm: float,
    rain_72h_mm: float,
    dordogne_level_m: float,
    tide_coefficient: int,
    hours_to_high_tide: float,
    video_level: Optional[float] = None,
) -> RiskScore:
    """Calculate risk from explicit values (for simulation)."""
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

    tide_score = _normalize_score(
        tide_coefficient,
        (70, settings.risk_tide_coef_high, 110),
    )

    # Tide phase from hours
    if abs(hours_to_high_tide) <= 1:
        tide_phase_score = 1.0
    elif abs(hours_to_high_tide) <= 2:
        tide_phase_score = 0.8
    elif abs(hours_to_high_tide) <= 3:
        tide_phase_score = 0.5
    else:
        tide_phase_score = 0.2

    video_score = (video_level / 100) if video_level is not None else None

    # Weighted calculation
    weights = {"rain_24h": 0.25, "rain_72h": 0.15, "river": 0.30, "tide": 0.15, "tide_phase": 0.10, "video": 0.05}
    total_weight = sum(weights.values()) - (weights["video"] if video_score is None else 0)

    weighted_score = (
        rain_24h_score * weights["rain_24h"] +
        rain_72h_score * weights["rain_72h"] +
        river_score * weights["river"] +
        tide_score * weights["tide"] +
        tide_phase_score * weights["tide_phase"]
    )
    if video_score is not None:
        weighted_score += video_score * weights["video"]

    final_score = (weighted_score / total_weight) * 100
    level = _score_to_level(final_score)

    factors = RiskFactors(
        rain_24h_score=rain_24h_score,
        rain_72h_score=rain_72h_score,
        river_level_score=river_score,
        tide_score=tide_score,
        tide_phase_score=tide_phase_score,
        video_level_score=video_score,
    )

    return RiskScore(
        timestamp=now,
        level=level,
        score=round(final_score, 1),
        factors=factors,
        description=_generate_description(level, factors),
        recommendations=_generate_recommendations(level, factors),
    )


async def get_risk_forecast(hours: int = 72) -> list[dict]:
    """
    Generate risk forecast for the next hours.

    Combines:
    - Tide predictions (known accurately)
    - Weather forecast (less certain)
    - River level trends (extrapolated)
    """
    from app.services.collectors.tide import get_current_tide_data
    from app.services.collectors.weather import get_forecast_rainfall

    now = datetime.utcnow()
    forecasts = []

    # Get tide data
    tide_data = await get_current_tide_data()

    # Get rainfall forecast
    rain_forecast = await get_forecast_rainfall(hours)

    # Generate hourly forecasts
    for h in range(0, hours, 3):  # Every 3 hours
        forecast_time = now + timedelta(hours=h)

        # Find relevant tide
        future_tides = [t for t in tide_data.tides if t.timestamp > forecast_time]
        next_high = next((t for t in future_tides if t.tide_type == TideType.HIGH), None)

        # Calculate hours to high tide
        if next_high:
            hours_to_high = (next_high.timestamp - forecast_time).total_seconds() / 3600
            tide_coef = next_high.coefficient or 70
        else:
            hours_to_high = 6
            tide_coef = 70

        # Get rainfall for this period
        period_rain = [
            r for r in rain_forecast
            if forecast_time <= r.timestamp < forecast_time + timedelta(hours=3)
        ]
        rain_3h = sum(r.precipitation_mm for r in period_rain)

        # Estimate cumulative rain (simplified)
        rain_24h_est = rain_3h * 4  # Very rough estimate

        # Calculate risk for this time
        risk = await calculate_risk_from_values(
            rain_24h_mm=rain_24h_est,
            rain_72h_mm=rain_24h_est * 2,
            dordogne_level_m=2.5,  # Baseline, would need proper forecast
            tide_coefficient=tide_coef,
            hours_to_high_tide=hours_to_high,
        )

        forecasts.append({
            "timestamp": forecast_time.isoformat(),
            "hours_ahead": h,
            "score": risk.score,
            "level": risk.level.value,
            "tide_coefficient": tide_coef,
            "rain_forecast_mm": rain_3h,
        })

    return forecasts
