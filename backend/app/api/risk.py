"""Risk scoring endpoints."""

from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Query

from app.models.schemas import RiskScore, RiskLevel
from app.services.risk import calculate_risk_score
from app.services.forecast import get_risk_forecast_v2
from app.services.collectors.tide import get_current_tide_data
from app.services.collectors.vigicrues import get_current_river_level
from app.services.collectors.weather import get_rainfall_summary, get_current_wind

router = APIRouter()


@router.get("", response_model=RiskScore)
async def get_current_risk():
    """Get current risk score and breakdown."""
    # Fetch current data
    tide_data = await get_current_tide_data()
    river_data = await get_current_river_level()
    rainfall_data = await get_rainfall_summary()
    wind_data = await get_current_wind()

    # Calculate risk with all factors
    risk = await calculate_risk_score(
        tide=tide_data,
        river=river_data,
        rainfall=rainfall_data,
        westerly_wind_kmh=wind_data.get("westerly_wind_kmh", 0),
    )

    return risk


@router.get("/forecast")
async def get_risk_forecast_endpoint(
    hours: int = Query(72, ge=6, le=168, description="Hours to forecast"),
):
    """
    Get risk forecast for the next hours.

    v2.0: Full risk forecast combining:
    - Hourly rainfall forecast (Open-Meteo AROME/ARPEGE)
    - Tide predictions with coefficients
    - Projected river levels from upstream stations with propagation delays
    - Current soil saturation factor

    Returns hourly risk predictions with cumulative rain, tide info,
    and projected river levels for each hour.

    Cached in Redis for 30 minutes.
    """
    return await get_risk_forecast_v2(hours=hours)


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
    rain_24h_mm: float = Query(..., ge=0, description="Pluie 24h simulée en mm"),
    rain_72h_mm: float = Query(..., ge=0, description="Pluie 72h simulée en mm"),
    rain_7d_mm: float = Query(0, ge=0, description="Pluie 7 jours simulée en mm"),
    dordogne_level_m: float = Query(..., ge=0, description="Niveau Dordogne simulé en m"),
    tide_coefficient: int = Query(..., ge=20, le=120, description="Coefficient de marée simulé"),
    hours_to_high_tide: float = Query(0, description="Heures avant pleine mer (2 = pic risque)"),
    westerly_wind_kmh: float = Query(0, ge=0, description="Vent d'ouest en km/h (>30 = surcote)"),
    soil_saturation: float = Query(1.0, ge=1.0, le=1.5, description="Saturation sols (1.0=sec, 1.5=saturé)"),
):
    """
    Simulate risk score with custom values.

    Based on FLOOD_RISK_ANALYSIS.md scoring:
    - Pluie: 0-30 points (avec saturation sols)
    - Niveau Dordogne: 0-25 points
    - Coefficient marée: 0-25 points
    - Phase marée: 0-15 points (pic à PM-2h)
    - Vent ouest: 0-5 points (surcote estuaire)

    Useful for:
    - Understanding how different factors affect risk
    - Testing threshold configurations
    - Training and educational purposes
    """
    from app.services.risk import calculate_risk_from_values

    risk = await calculate_risk_from_values(
        rain_24h_mm=rain_24h_mm,
        rain_72h_mm=rain_72h_mm,
        rain_7d_mm=rain_7d_mm,
        dordogne_level_m=dordogne_level_m,
        tide_coefficient=tide_coefficient,
        hours_to_high_tide=hours_to_high_tide,
        westerly_wind_kmh=westerly_wind_kmh,
        soil_saturation_factor=soil_saturation,
    )

    return {
        "simulated_values": {
            "rain_24h_mm": rain_24h_mm,
            "rain_72h_mm": rain_72h_mm,
            "rain_7d_mm": rain_7d_mm,
            "dordogne_level_m": dordogne_level_m,
            "tide_coefficient": tide_coefficient,
            "hours_to_high_tide": hours_to_high_tide,
            "westerly_wind_kmh": westerly_wind_kmh,
            "soil_saturation": soil_saturation,
        },
        "risk": risk,
    }
