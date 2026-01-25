"""Real-time data endpoints."""

from datetime import datetime
from fastapi import APIRouter, Depends

from app.models.schemas import (
    RealtimeData,
    RiskScore,
    RiskLevel,
    RiskFactors,
    TideForecast,
    TideData,
    TideType,
    RiverLevelForecast,
    RiverLevel,
    Tendency,
    RainfallSummary,
    VideoAnalysis,
)
from app.services.risk import calculate_risk_score
from app.services.collectors.tide import get_current_tide_data
from app.services.collectors.vigicrues import get_current_river_level
from app.services.collectors.weather import get_rainfall_summary

router = APIRouter()


@router.get("", response_model=RealtimeData)
async def get_realtime_data():
    """Get current real-time data for the dashboard."""
    now = datetime.utcnow()

    # Fetch current data from all sources
    tide_data = await get_current_tide_data()
    river_data = await get_current_river_level()
    rainfall_data = await get_rainfall_summary()

    # Calculate risk score
    risk = await calculate_risk_score(
        tide=tide_data,
        river=river_data,
        rainfall=rainfall_data,
    )

    # Build alerts list
    alerts = []
    if risk.level in [RiskLevel.ORANGE, RiskLevel.RED]:
        alerts.append(f"Risque {risk.level.value}: {risk.description}")
    if rainfall_data.last_24h_mm > 40:
        alerts.append(f"Fortes pluies: {rainfall_data.last_24h_mm:.1f}mm en 24h")
    if river_data.current.height_m > 4.0:
        alerts.append(f"Niveau Dordogne élevé: {river_data.current.height_m:.2f}m")

    return RealtimeData(
        timestamp=now,
        risk=risk,
        tide=tide_data,
        river=river_data,
        rainfall=rainfall_data,
        video=None,  # TODO: integrate video analysis
        alerts=alerts,
    )


@router.get("/tide", response_model=TideForecast)
async def get_tide():
    """Get current tide information."""
    return await get_current_tide_data()


@router.get("/river", response_model=RiverLevelForecast)
async def get_river():
    """Get current river level information."""
    return await get_current_river_level()


@router.get("/rainfall", response_model=RainfallSummary)
async def get_rainfall():
    """Get current rainfall summary."""
    return await get_rainfall_summary()
