"""Real-time data endpoints."""

from datetime import datetime
from fastapi import APIRouter

from app.models.schemas import (
    RealtimeData,
    RiskLevel,
    TideForecast,
    RiverLevelForecast,
    RainfallSummary,
    MultiStationData,
)
from app.services.risk_v2 import calculate_risk_score_v2
from app.services.collectors.tide import get_current_tide_data
from app.services.collectors.vigicrues import get_current_river_level
from app.services.collectors.weather import get_rainfall_summary
from app.services.collectors.hubeau import get_multi_station_data
from app.services.collectors.netatmo import get_basin_rainfall
from app.services.collectors.infoclimat import find_stations_near, get_basin_rainfall_infoclimat
from app.config import get_settings

router = APIRouter()


@router.get("", response_model=RealtimeData)
async def get_realtime_data():
    """Get current real-time data for the dashboard."""
    now = datetime.utcnow()

    # Fetch current data from all sources
    tide_data = await get_current_tide_data()
    river_data = await get_current_river_level()
    rainfall_data = await get_rainfall_summary()

    # Fetch multi-station data from Hub'Eau
    stations_data = await get_multi_station_data()

    # Enrich rainfall with Netatmo data if available
    netatmo_data = await get_basin_rainfall()
    if netatmo_data.get("available"):
        # Update rainfall with Netatmo observations
        netatmo_1h = netatmo_data.get("rain_1h_avg_mm", 0)
        netatmo_24h = netatmo_data.get("rain_24h_avg_mm", 0)

        # Use Netatmo if it has data, otherwise keep original
        if netatmo_1h > 0 or netatmo_24h > 0:
            rainfall_data = RainfallSummary(
                last_hour_mm=max(rainfall_data.last_hour_mm, netatmo_1h),
                last_6h_mm=rainfall_data.last_6h_mm,
                last_24h_mm=max(rainfall_data.last_24h_mm, netatmo_24h),
                last_72h_mm=rainfall_data.last_72h_mm,
                forecast_24h_mm=rainfall_data.forecast_24h_mm,
                forecast_72h_mm=rainfall_data.forecast_72h_mm,
            )

    # Calculate risk score using v2 multi-station model
    risk = await calculate_risk_score_v2(
        stations_data=stations_data,
        tide=tide_data,
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

    # Add upstream alerts from multi-station data
    for station in stations_data.stations:
        if station.station_id != stations_data.primary_station_id:
            if station.current_level.height_m > station.threshold_orange:
                alerts.append(
                    f"Niveau élevé à {station.station_name} ({station.river}): "
                    f"{station.current_level.height_m:.2f}m - "
                    f"impact dans ~{station.delay_hours:.0f}h"
                )

    # Add Netatmo info if available
    if netatmo_data.get("available"):
        stations_count = netatmo_data.get("stations_count", 0)
        if stations_count > 0:
            alerts.append(f"Données Netatmo: {stations_count} stations actives")

    return RealtimeData(
        timestamp=now,
        risk=risk,
        tide=tide_data,
        river=river_data,
        rainfall=rainfall_data,
        video=None,  # TODO: integrate video analysis
        alerts=alerts,
        stations=stations_data,
        netatmo_available=netatmo_data.get("available", False),
    )


@router.get("/stations", response_model=MultiStationData)
async def get_all_stations():
    """Get current data from all monitoring stations."""
    return await get_multi_station_data()


@router.get("/netatmo")
async def get_netatmo_rainfall():
    """Get rainfall data from Netatmo stations."""
    return await get_basin_rainfall()


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


@router.get("/infoclimat")
async def get_infoclimat_data():
    """Get Infoclimat stations and rainfall data."""
    settings = get_settings()

    # Get nearby stations
    stations = await find_stations_near(
        lat=settings.moulin_lat,
        lon=settings.moulin_lon,
        radius_km=50
    )

    # Try to get rainfall data (may fail due to IP restrictions)
    rainfall = await get_basin_rainfall_infoclimat()

    return {
        "stations_found": len(stations),
        "nearest_stations": [
            {
                "name": s.get("name"),
                "distance_km": s.get("distance_km"),
                "source": s.get("source"),
                "departement": s.get("departement"),
            }
            for s in stations[:10]
        ],
        "rainfall": rainfall,
    }


@router.get("/sources")
async def get_data_sources_status():
    """Get status of all data sources."""
    settings = get_settings()
    sources = []

    # Hub'Eau
    try:
        stations_data = await get_multi_station_data()
        active_stations = len([s for s in stations_data.stations if s.current_level])
        sources.append({
            "name": "Hub'Eau Hydrometrie",
            "status": "ok" if active_stations > 0 else "error",
            "description": "Niveaux des cours d'eau (Vigicrues/DREAL)",
            "details": f"{active_stations} stations actives sur {len(stations_data.stations)}",
            "url": "https://hubeau.eaufrance.fr/page/api-hydrometrie",
        })
    except Exception as e:
        sources.append({
            "name": "Hub'Eau Hydrometrie",
            "status": "error",
            "description": "Niveaux des cours d'eau",
            "details": str(e),
        })

    # Netatmo
    try:
        netatmo_data = await get_basin_rainfall()
        sources.append({
            "name": "Netatmo Weather",
            "status": "ok" if netatmo_data.get("available") else "degraded",
            "description": "Pluviometrie citoyenne",
            "details": f"{netatmo_data.get('stations_count', 0)} stations avec pluviometre",
            "url": "https://dev.netatmo.com/",
        })
    except Exception as e:
        sources.append({
            "name": "Netatmo Weather",
            "status": "error",
            "description": "Pluviometrie citoyenne",
            "details": str(e),
        })

    # Infoclimat
    try:
        infoclimat_stations = await find_stations_near(
            settings.moulin_lat, settings.moulin_lon, 50
        )
        infoclimat_rainfall = await get_basin_rainfall_infoclimat()
        sources.append({
            "name": "Infoclimat",
            "status": "ok" if infoclimat_rainfall.get("available") else "degraded",
            "description": "Reseau meteo amateur (StatIC)",
            "details": f"{len(infoclimat_stations)} stations proches" + (
                " - API restreinte par IP" if not infoclimat_rainfall.get("available") else ""
            ),
            "url": "https://www.infoclimat.fr/opendata/",
        })
    except Exception as e:
        sources.append({
            "name": "Infoclimat",
            "status": "error",
            "description": "Reseau meteo amateur",
            "details": str(e),
        })

    # Tide (Open-Meteo Marine)
    try:
        tide_data = await get_current_tide_data()
        has_tide = tide_data.tides and len(tide_data.tides) > 0
        sources.append({
            "name": "Open-Meteo Marine",
            "status": "ok" if has_tide else "degraded",
            "description": "Marees et coefficients",
            "details": f"Coef actuel: {tide_data.current_coefficient or 'N/A'}",
            "url": "https://open-meteo.com/en/docs/marine-weather-api",
        })
    except Exception as e:
        sources.append({
            "name": "Open-Meteo Marine",
            "status": "error",
            "description": "Marees et coefficients",
            "details": str(e),
        })

    # Weather forecast (Open-Meteo)
    try:
        rainfall = await get_rainfall_summary()
        sources.append({
            "name": "Open-Meteo Weather",
            "status": "ok",
            "description": "Previsions meteorologiques",
            "details": f"Prevu 24h: {rainfall.forecast_24h_mm:.1f}mm, 72h: {rainfall.forecast_72h_mm:.1f}mm",
            "url": "https://open-meteo.com/en/docs",
        })
    except Exception as e:
        sources.append({
            "name": "Open-Meteo Weather",
            "status": "error",
            "description": "Previsions meteorologiques",
            "details": str(e),
        })

    return {
        "timestamp": datetime.utcnow().isoformat(),
        "sources": sources,
    }
