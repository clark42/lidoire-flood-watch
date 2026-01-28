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

    Based on FLOOD_RISK_ANALYSIS.md:
    - Risk is HIGHEST at PM-2h (2 hours BEFORE high tide)
    - This is because flood risk comes from blocking of Lidoire outflow
    - The blocking is worst during rising tide, just before high tide
    - Window PM-4h to PM+1h is critical

    Returns 0-1 score (0-15 points in doc, normalized here)
    """
    if timestamp is None:
        timestamp = datetime.utcnow()

    if not tide.next_high_tide:
        return 0.0

    # Calculate hours until next high tide (positive = before PM)
    hours_to_high = (tide.next_high_tide.timestamp - timestamp).total_seconds() / 3600

    # Also check if we just passed high tide
    past_high_times = [
        t for t in tide.tides
        if t.tide_type == TideType.HIGH and t.timestamp < timestamp
    ]
    if past_high_times:
        hours_since_high = (timestamp - past_high_times[-1].timestamp).total_seconds() / 3600
        # Use negative to indicate "after high tide"
        if hours_since_high < hours_to_high:
            hours_to_high = -hours_since_high

    # Scoring based on document:
    # Peak risk at PM-2h (hours_to_high = 2)
    # Risk window: PM-6h to PM+3h
    if hours_to_high < -3:  # More than 3h after PM
        return 0.0
    elif hours_to_high > 6:  # More than 6h before PM
        return 0.0
    else:
        # Peak at PM-2h (hours_to_high = 2)
        # Gaussian-like curve centered at 2h before PM
        distance_from_peak = abs(hours_to_high - 2)
        # Max at 0 distance, decreases to 0 at distance 5
        score = max(0, 1 - (distance_from_peak / 5))
        return score


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
        if factors.soil_saturation_factor > 1.2:
            high_factors.append("pluie 24h (sols saturés)")
        else:
            high_factors.append("pluie 24h")
    if factors.rain_72h_score > 0.5:
        high_factors.append("pluie 72h")
    if factors.rain_7d_score > 0.5:
        high_factors.append("pluie 7j")
    if factors.river_level_score > 0.5:
        high_factors.append("niveau Dordogne")
    if factors.tide_score > 0.5:
        high_factors.append("coefficient marée")
    if factors.tide_phase_score > 0.6:
        high_factors.append("approche pleine mer")
    if factors.wind_west_score > 0.3:
        high_factors.append("vent d'ouest (surcote)")

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


async def _store_risk_to_influx(risk: RiskScore) -> bool:
    """Store risk score to InfluxDB."""
    from app.services import influx_client

    try:
        return await influx_client.write_risk_score(
            score=risk.score,
            level=risk.level.value,
            timestamp=risk.timestamp,
            factors={
                "rain_24h": risk.factors.rain_24h_score,
                "rain_72h": risk.factors.rain_72h_score,
                "river_level": risk.factors.river_level_score,
                "tide": risk.factors.tide_score,
                "tide_phase": risk.factors.tide_phase_score,
                "video_level": risk.factors.video_level_score,
            },
        )
    except Exception as e:
        print(f"Error storing risk to InfluxDB: {e}")
        return False


async def calculate_risk_score(
    tide: TideForecast,
    river: RiverLevelForecast,
    rainfall: RainfallSummary,
    video_level: Optional[float] = None,
    westerly_wind_kmh: float = 0.0,
) -> RiskScore:
    """
    Calculate overall flood risk score based on FLOOD_RISK_ANALYSIS.md.

    Weights (out of 100 points):
    - Rainfall (with soil saturation): 0-30 points
    - River level (Dordogne): 0-25 points
    - Tide coefficient: 0-25 points
    - Tide phase (peak at PM-2h): 0-15 points
    - Westerly wind (surge): 0-5 points

    Soil saturation factor (1.0-1.5) multiplies rainfall impact.
    """
    settings = get_settings()
    now = datetime.utcnow()

    # === Soil saturation factor ===
    saturation = rainfall.soil_saturation_factor or 1.0

    # === Rain score (0-30 points) ===
    # Apply saturation factor to effective rainfall
    effective_rain_24h = (rainfall.last_24h_mm + rainfall.forecast_24h_mm * 0.5) * saturation

    # Scoring from doc: <20mm = 0-10pts, 20-50mm = 10-20pts, >50mm = 20-30pts
    if effective_rain_24h < 20:
        rain_points = (effective_rain_24h / 20) * 10
    elif effective_rain_24h < 50:
        rain_points = 10 + ((effective_rain_24h - 20) / 30) * 10
    else:
        rain_points = 20 + ((effective_rain_24h - 50) / 30) * 10
    rain_points = min(30, rain_points)

    # Bonus for 72h/7d rain (if significant)
    if rainfall.last_72h_mm > 80:
        rain_points = min(30, rain_points + 3)
    if rainfall.last_7d_mm > 120:
        rain_points = min(30, rain_points + 2)

    rain_24h_score = rain_points / 30  # Normalize to 0-1
    rain_72h_score = _normalize_score(
        rainfall.last_72h_mm * saturation,
        (40, 70, 110),
    )
    rain_7d_score = _normalize_score(
        rainfall.last_7d_mm * saturation,
        (60, 100, 150),
    )

    # === River level score (0-25 points) ===
    # Doc seuils: <2.5m OK, 2.5-4m vigilance, >4m danger
    level_m = river.current.height_m
    if level_m < 2.5:
        river_points = (level_m / 2.5) * 5
    elif level_m < 4.0:
        river_points = 5 + ((level_m - 2.5) / 1.5) * 10
    elif level_m < 5.5:
        river_points = 15 + ((level_m - 4.0) / 1.5) * 10
    else:
        river_points = 25
    river_points = min(25, river_points)
    river_score = river_points / 25

    # === Tide coefficient score (0-25 points) ===
    # Doc: <70 faible, 70-90 modéré, 90-105 fort, >105 très fort
    coef = tide.current_coefficient or 70
    if coef < 70:
        tide_points = (coef / 70) * 5
    elif coef < 90:
        tide_points = 5 + ((coef - 70) / 20) * 10
    elif coef < 105:
        tide_points = 15 + ((coef - 90) / 15) * 7
    else:
        tide_points = 22 + ((coef - 105) / 15) * 3
    tide_points = min(25, tide_points)
    tide_score = tide_points / 25

    # === Tide phase score (0-15 points) ===
    # Peak at PM-2h based on document
    tide_phase_score = _calculate_tide_phase_score(tide, now)
    phase_points = tide_phase_score * 15

    # === Westerly wind score (0-5 points) ===
    # Surge from west wind (doc: >30 km/h = risk)
    if westerly_wind_kmh < 30:
        wind_points = 0
    elif westerly_wind_kmh < 60:
        wind_points = ((westerly_wind_kmh - 30) / 30) * 2
    elif westerly_wind_kmh < 100:
        wind_points = 2 + ((westerly_wind_kmh - 60) / 40) * 2
    else:
        wind_points = 4 + min(1, (westerly_wind_kmh - 100) / 50)
    wind_points = min(5, wind_points)
    wind_score = wind_points / 5 if wind_points > 0 else 0

    # === Video level score (bonus, not in doc weights) ===
    video_score = (video_level / 100) if video_level is not None else None

    # === Total score (0-100) ===
    total_points = rain_points + river_points + tide_points + phase_points + wind_points

    # Video adds bonus (max +5 if very high)
    if video_score is not None and video_score > 0.5:
        total_points = min(100, total_points + (video_score - 0.5) * 10)

    final_score = min(100, total_points)

    # Determine level
    level = _score_to_level(final_score)

    factors = RiskFactors(
        rain_24h_score=rain_24h_score,
        rain_72h_score=rain_72h_score,
        rain_7d_score=rain_7d_score,
        river_level_score=river_score,
        tide_score=tide_score,
        tide_phase_score=tide_phase_score,
        wind_west_score=wind_score,
        soil_saturation_factor=saturation,
        video_level_score=video_score,
    )

    risk = RiskScore(
        timestamp=now,
        level=level,
        score=round(final_score, 1),
        factors=factors,
        description=_generate_description(level, factors),
        recommendations=_generate_recommendations(level, factors),
    )

    # Store to InfluxDB
    await _store_risk_to_influx(risk)

    return risk


async def calculate_risk_from_values(
    rain_24h_mm: float,
    rain_72h_mm: float,
    dordogne_level_m: float,
    tide_coefficient: int,
    hours_to_high_tide: float,
    video_level: Optional[float] = None,
    westerly_wind_kmh: float = 0.0,
    soil_saturation_factor: float = 1.0,
    rain_7d_mm: float = 0.0,
) -> RiskScore:
    """
    Calculate risk from explicit values (for simulation).

    Uses same algorithm as calculate_risk_score but with direct values.
    Based on FLOOD_RISK_ANALYSIS.md weights.
    """
    now = datetime.utcnow()

    # === Rain score (0-30 points) ===
    effective_rain = rain_24h_mm * soil_saturation_factor
    if effective_rain < 20:
        rain_points = (effective_rain / 20) * 10
    elif effective_rain < 50:
        rain_points = 10 + ((effective_rain - 20) / 30) * 10
    else:
        rain_points = 20 + ((effective_rain - 50) / 30) * 10
    rain_points = min(30, rain_points)

    if rain_72h_mm > 80:
        rain_points = min(30, rain_points + 3)
    if rain_7d_mm > 120:
        rain_points = min(30, rain_points + 2)

    rain_24h_score = rain_points / 30
    rain_72h_score = _normalize_score(rain_72h_mm * soil_saturation_factor, (40, 70, 110))
    rain_7d_score = _normalize_score(rain_7d_mm * soil_saturation_factor, (60, 100, 150))

    # === River level score (0-25 points) ===
    if dordogne_level_m < 2.5:
        river_points = (dordogne_level_m / 2.5) * 5
    elif dordogne_level_m < 4.0:
        river_points = 5 + ((dordogne_level_m - 2.5) / 1.5) * 10
    elif dordogne_level_m < 5.5:
        river_points = 15 + ((dordogne_level_m - 4.0) / 1.5) * 10
    else:
        river_points = 25
    river_points = min(25, river_points)
    river_score = river_points / 25

    # === Tide coefficient score (0-25 points) ===
    if tide_coefficient < 70:
        tide_points = (tide_coefficient / 70) * 5
    elif tide_coefficient < 90:
        tide_points = 5 + ((tide_coefficient - 70) / 20) * 10
    elif tide_coefficient < 105:
        tide_points = 15 + ((tide_coefficient - 90) / 15) * 7
    else:
        tide_points = 22 + ((tide_coefficient - 105) / 15) * 3
    tide_points = min(25, tide_points)
    tide_score = tide_points / 25

    # === Tide phase score (0-15 points) ===
    # Peak at PM-2h (hours_to_high_tide = 2)
    distance_from_peak = abs(hours_to_high_tide - 2)
    tide_phase_score = max(0, 1 - (distance_from_peak / 5))
    phase_points = tide_phase_score * 15

    # === Wind score (0-5 points) ===
    if westerly_wind_kmh < 30:
        wind_points = 0
    elif westerly_wind_kmh < 60:
        wind_points = ((westerly_wind_kmh - 30) / 30) * 2
    elif westerly_wind_kmh < 100:
        wind_points = 2 + ((westerly_wind_kmh - 60) / 40) * 2
    else:
        wind_points = 4 + min(1, (westerly_wind_kmh - 100) / 50)
    wind_points = min(5, wind_points)
    wind_score = wind_points / 5 if wind_points > 0 else 0

    video_score = (video_level / 100) if video_level is not None else None

    # === Total score ===
    total_points = rain_points + river_points + tide_points + phase_points + wind_points

    if video_score is not None and video_score > 0.5:
        total_points = min(100, total_points + (video_score - 0.5) * 10)

    final_score = min(100, total_points)
    level = _score_to_level(final_score)

    factors = RiskFactors(
        rain_24h_score=rain_24h_score,
        rain_72h_score=rain_72h_score,
        rain_7d_score=rain_7d_score,
        river_level_score=river_score,
        tide_score=tide_score,
        tide_phase_score=tide_phase_score,
        wind_west_score=wind_score,
        soil_saturation_factor=soil_saturation_factor,
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
