"""Risk scoring endpoints."""

from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Query

from app.models.schemas import RiskScore, RiskLevel
from app.services.risk import calculate_risk_score, get_risk_forecast
from app.services.collectors.tide import get_current_tide_data
from app.services.collectors.vigicrues import get_current_river_level
from app.services.collectors.weather import get_rainfall_summary

router = APIRouter()


@router.get("", response_model=RiskScore)
async def get_current_risk():
    """Get current risk score and breakdown."""
    # Fetch current data
    tide_data = await get_current_tide_data()
    river_data = await get_current_river_level()
    rainfall_data = await get_rainfall_summary()

    # Calculate risk
    risk = await calculate_risk_score(
        tide=tide_data,
        river=river_data,
        rainfall=rainfall_data,
    )

    return risk


@router.get("/forecast")
async def get_risk_forecast_endpoint(
    hours: int = Query(72, ge=6, le=168, description="Hours to forecast"),
):
    """
    Get risk forecast for the next hours.

    Combines:
    - Predicted tides
    - Weather forecast (rainfall)
    - River level trends

    Returns hourly risk predictions.
    """
    forecasts = await get_risk_forecast(hours=hours)

    return {
        "generated_at": datetime.utcnow(),
        "hours_ahead": hours,
        "forecasts": forecasts,
        "max_risk": max(f["score"] for f in forecasts) if forecasts else 0,
        "critical_periods": [
            f for f in forecasts if f["level"] in [RiskLevel.ORANGE.value, RiskLevel.RED.value]
        ],
    }


@router.get("/thresholds")
async def get_risk_thresholds():
    """Get current risk threshold configuration."""
    from app.config import get_settings

    settings = get_settings()

    return {
        "rainfall_24h": {
            "yellow": settings.risk_rain_24h_yellow,
            "orange": settings.risk_rain_24h_orange,
            "red": settings.risk_rain_24h_red,
            "unit": "mm",
        },
        "dordogne_level": {
            "yellow": settings.risk_dordogne_level_yellow,
            "orange": settings.risk_dordogne_level_orange,
            "red": settings.risk_dordogne_level_red,
            "unit": "m",
        },
        "tide_coefficient": {
            "high": settings.risk_tide_coef_high,
            "description": "Coefficient above which tide is considered high",
        },
    }


@router.post("/simulate")
async def simulate_risk(
    rain_24h_mm: float = Query(..., ge=0, description="Simulated 24h rainfall in mm"),
    rain_72h_mm: float = Query(..., ge=0, description="Simulated 72h rainfall in mm"),
    dordogne_level_m: float = Query(..., ge=0, description="Simulated Dordogne level in m"),
    tide_coefficient: int = Query(..., ge=20, le=120, description="Simulated tide coefficient"),
    hours_to_high_tide: float = Query(0, description="Hours until next high tide"),
):
    """
    Simulate risk score with custom values.

    Useful for:
    - Understanding how different factors affect risk
    - Testing threshold configurations
    - Training and educational purposes
    """
    from app.services.risk import calculate_risk_from_values

    risk = await calculate_risk_from_values(
        rain_24h_mm=rain_24h_mm,
        rain_72h_mm=rain_72h_mm,
        dordogne_level_m=dordogne_level_m,
        tide_coefficient=tide_coefficient,
        hours_to_high_tide=hours_to_high_tide,
    )

    return {
        "simulated_values": {
            "rain_24h_mm": rain_24h_mm,
            "rain_72h_mm": rain_72h_mm,
            "dordogne_level_m": dordogne_level_m,
            "tide_coefficient": tide_coefficient,
            "hours_to_high_tide": hours_to_high_tide,
        },
        "risk": risk,
    }
