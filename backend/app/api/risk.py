"""Risk scoring endpoints.

All scores come from the single v3.4 scoring core (risk_core.compute_risk).
Reads never persist anything: the hourly scheduler job stores the realtime
score.
"""

import logging
from fastapi import APIRouter, HTTPException, Query

from app.models.schemas import RiskScore
from app.services.risk_core import MODEL_VERSION
from app.services.risk_v2 import compute_current_risk, calculate_risk_from_values
from app.services.forecast import get_risk_forecast_v2

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("", response_model=RiskScore)
async def get_current_risk():
    """Get current risk score and breakdown (same inputs as /api/realtime)."""
    try:
        risk, _ = await compute_current_risk(store=False)
        return risk

    except Exception as e:
        logger.exception("Error calculating risk score")
        raise HTTPException(status_code=500, detail=f"Risk calculation failed: {e}")


@router.get("/forecast", response_model=None)
async def get_risk_forecast_endpoint(
    hours: int = Query(168, ge=6, le=336, description="Hours to forecast (max 14 days)"),
):
    """
    Get risk forecast for the next hours (default 7 days, max 14 days).

    v2.0: Full risk forecast combining:
    - Hourly rainfall forecast (Open-Meteo AROME/ARPEGE) - up to 16 days
    - Tide predictions with coefficients
    - Projected river levels from upstream stations with propagation delays
    - Current soil saturation factor

    Returns hourly risk predictions with cumulative rain, tide info,
    and projected river levels for each hour.

    Cached in Redis for 30 minutes.
    """
    try:
        return await get_risk_forecast_v2(hours=hours)
    except Exception as e:
        logger.exception("Error generating risk forecast")
        raise HTTPException(status_code=500, detail=f"Risk forecast failed: {e}")


@router.get("/forecast/multi-model")
async def get_multi_model_forecast_endpoint():
    """
    Get precipitation forecast from multiple weather models.

    Fetches forecasts from:
    - Meteo-France (AROME/ARPEGE) - best for France, 3-5 days
    - ECMWF (European model) - very reliable, 10+ days
    - GFS (US model) - good for long range
    - Open-Meteo Best Match (auto-select)
    - Meteo Agricole (10-day forecast)

    Returns:
    - Individual model forecasts
    - Ensemble average/median with min/max spread
    - Useful for understanding forecast uncertainty
    """
    try:
        from app.services.collectors.weather import fetch_multi_model_forecast
        from app.config import get_settings

        settings = get_settings()
        return await fetch_multi_model_forecast(
            settings.moulin_lat, settings.moulin_lon, forecast_days=14
        )
    except Exception as e:
        logger.exception("Error fetching multi-model forecast")
        raise HTTPException(status_code=500, detail=f"Multi-model forecast failed: {e}")


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
    rain_24h_mm: float = Query(..., ge=0, description="Pluie 24h simulee en mm"),
    rain_72h_mm: float = Query(..., ge=0, description="Pluie 72h simulee en mm"),
    rain_7d_mm: float = Query(0, ge=0, description="Pluie 7 jours simulee en mm"),
    dordogne_level_m: float = Query(..., ge=0, description="Niveau Dordogne (Pessac) simule en m"),
    tide_coefficient: int = Query(..., ge=20, le=120, description="Coefficient de maree simule"),
    hours_to_high_tide: float = Query(0, description="Heures avant pleine mer"),
    westerly_wind_kmh: float = Query(0, ge=0, description="Vent d'ouest en km/h"),
    soil_saturation: float = Query(1.0, ge=1.0, le=1.5, description="Saturation sols (1.0=sec, 1.5=sature)"),
    rate_cm_per_hour: float = Query(0, description="Vitesse montee en cm/h"),
    n_rising_stations: int = Query(0, ge=0, le=7, description="Nombre de stations en hausse"),
):
    """
    Simulate risk score with custom values (v3.4 model, nothing is stored).

    Every input is used: the Dordogne level is scored as a single aggregate
    station with the Pessac thresholds, the rising-station count drives
    momentum/conjunction/dampener.
    """
    try:
        risk = await calculate_risk_from_values(
            rain_24h_mm=rain_24h_mm,
            rain_72h_mm=rain_72h_mm,
            dordogne_level_m=dordogne_level_m,
            tide_coefficient=tide_coefficient,
            hours_to_high_tide=hours_to_high_tide,
            rate_cm_per_hour=rate_cm_per_hour,
            n_rising_stations=n_rising_stations,
            rain_7d_mm=rain_7d_mm,
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
                "rate_cm_per_hour": rate_cm_per_hour,
                "n_rising_stations": n_rising_stations,
            },
            "risk": risk,
            "model_version": f"v{MODEL_VERSION}",
        }

    except Exception as e:
        logger.exception("Error in risk simulation")
        raise HTTPException(status_code=500, detail=f"Risk simulation failed: {e}")
