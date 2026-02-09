"""Risk scoring endpoints.

Uses the v3.2 multi-station risk model (risk_v2) for all calculations.
"""

import asyncio
import logging
from fastapi import APIRouter, HTTPException, Query

from app.models.schemas import RiskScore
from app.services.risk_v2 import calculate_risk_score_v2, calculate_risk_from_values
from app.services.forecast import get_risk_forecast_v2
from app.services.collectors.tide import get_current_tide_data
from app.services.collectors.hubeau import get_multi_station_data
from app.services.collectors.weather import get_rainfall_summary, get_soil_wind_cached
from app.services.collectors.meteo_agricole import get_meteo_agricole_cached

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("", response_model=RiskScore)
async def get_current_risk():
    """Get current risk score and breakdown using multi-station v3.2 model."""
    try:
        tide_data, rainfall_data, stations_data, soil_wind_data, meteo_agricole_data = (
            await asyncio.gather(
                get_current_tide_data(),
                get_rainfall_summary(),
                get_multi_station_data(),
                get_soil_wind_cached(),
                get_meteo_agricole_cached(),
            )
        )

        # Determine soil saturation factor (same logic as realtime.py)
        soil_sat_factor = rainfall_data.soil_saturation_factor

        moisture_deep = soil_wind_data.get("moisture_deep", 0)
        if moisture_deep and moisture_deep > 0.30:
            soil_sat_factor = min(1.5, soil_sat_factor + 0.15)
        elif moisture_deep and moisture_deep > 0.25:
            soil_sat_factor = min(1.5, soil_sat_factor + 0.05)

        et0 = soil_wind_data.get("et0_mm")
        if et0 is not None and et0 < 1.0:
            soil_sat_factor = min(1.5, soil_sat_factor + 0.05)

        if meteo_agricole_data:
            surplus = meteo_agricole_data.surplus_deficit_30d_pct
            if surplus is not None:
                if surplus > 20:
                    soil_sat_factor = min(1.5, soil_sat_factor + 0.1)
                elif surplus < -20:
                    soil_sat_factor = max(1.0, soil_sat_factor - 0.1)

        westerly_wind = soil_wind_data.get("westerly_wind_kmh", 0.0)
        if meteo_agricole_data and meteo_agricole_data.wind_gust_kmh > 0:
            westerly_wind = max(westerly_wind, meteo_agricole_data.wind_gust_kmh * 0.7)

        risk = await calculate_risk_score_v2(
            stations_data=stations_data,
            tide=tide_data,
            rainfall=rainfall_data,
            westerly_wind_kmh=westerly_wind,
            soil_saturation_factor=soil_sat_factor,
        )

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
    dordogne_level_m: float = Query(..., ge=0, description="Niveau Dordogne simule en m"),
    tide_coefficient: int = Query(..., ge=20, le=120, description="Coefficient de maree simule"),
    hours_to_high_tide: float = Query(0, description="Heures avant pleine mer"),
    westerly_wind_kmh: float = Query(0, ge=0, description="Vent d'ouest en km/h"),
    soil_saturation: float = Query(1.0, ge=1.0, le=1.5, description="Saturation sols (1.0=sec, 1.5=sature)"),
    rate_cm_per_hour: float = Query(0, description="Vitesse montee en cm/h"),
    n_rising_stations: int = Query(0, ge=0, le=7, description="Nombre de stations en hausse"),
):
    """
    Simulate risk score with custom values using v3.2 multi-station model.

    Useful for understanding how different factors affect risk,
    testing threshold configurations, training and educational purposes.
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
            n_total_stations=6,
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
            "model_version": "v3.2",
        }

    except Exception as e:
        logger.exception("Error in risk simulation")
        raise HTTPException(status_code=500, detail=f"Risk simulation failed: {e}")
