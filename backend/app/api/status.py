"""Status and health check endpoints."""

from datetime import datetime
from fastapi import APIRouter

from app.config import get_settings

router = APIRouter()


@router.get("/status")
async def get_status():
    """Get API status and health information."""
    settings = get_settings()
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "1.0.0",
        "location": {
            "lat": settings.moulin_lat,
            "lon": settings.moulin_lon,
            "name": "Moulin de la Lidoire",
        },
        "services": {
            "database": "connected",  # TODO: actual check
            "influxdb": "connected",   # TODO: actual check
            "collectors": {
                "tide": "running",
                "vigicrues": "running",
                "weather": "running",
                "camera": "running" if settings.camera_rtsp_url else "disabled",
            },
        },
    }


@router.get("/health")
async def health_check():
    """Simple health check endpoint."""
    return {"status": "ok"}


@router.get("/sources-detail")
async def get_sources_detail():
    """Return exhaustive detail of all configured data sources."""
    settings = get_settings()

    # River stations
    river_stations = []
    for s in settings.hubeau_stations:
        river_stations.append({
            "id": s["id"],
            "name": s["name"],
            "river": s["river"],
            "distance_km": s["distance_km"],
            "delay_hours": s["delay_hours"],
            "weight": s["weight"],
            "lat": s.get("lat"),
            "lon": s.get("lon"),
            "thresholds": s.get("thresholds", {}),
        })

    # Netatmo priority stations
    netatmo_stations = []
    for s in settings.lidoire_netatmo_priority:
        netatmo_stations.append({
            "city": s["city"],
            "lat": s["lat"],
            "lon": s["lon"],
            "position": s["position"],
        })

    # Lidoire basin points (Open-Meteo)
    basin_points = [
        {"id": p["id"], "name": p["name"], "lat": p["lat"], "lon": p["lon"]}
        for p in settings.lidoire_basin_points
    ]

    # Risk thresholds
    risk_thresholds = {
        "rain_24h": {
            "yellow": settings.risk_rain_24h_yellow,
            "orange": settings.risk_rain_24h_orange,
            "red": settings.risk_rain_24h_red,
        },
        "dordogne_level": {
            "yellow": settings.risk_dordogne_level_yellow,
            "orange": settings.risk_dordogne_level_orange,
            "red": settings.risk_dordogne_level_red,
        },
        "tide_coef_high": settings.risk_tide_coef_high,
    }

    return {
        "location": {
            "name": "Moulin de la Lidoire",
            "lat": settings.moulin_lat,
            "lon": settings.moulin_lon,
        },
        "primary_station": settings.hubeau_primary_station,
        "sources": {
            "river": {
                "name": "Hub'Eau Hydrometrie",
                "description": "Niveaux des cours d'eau via API Hub'Eau (DREAL/Vigicrues)",
                "api_url": "https://hubeau.eaufrance.fr/api/v2/hydrometrie",
                "stations": river_stations,
            },
            "netatmo": {
                "name": "Netatmo Weather",
                "description": "Pluviometrie citoyenne via stations Netatmo publiques",
                "api_url": "https://api.netatmo.com/api/getpublicdata",
                "configured": bool(settings.netatmo_client_id),
                "search_radius_km": 75,
                "priority_stations": netatmo_stations,
            },
            "infoclimat": {
                "name": "Infoclimat StatIC",
                "description": "Reseau meteo amateur — decouverte dynamique par rayon",
                "api_url": "https://www.infoclimat.fr/opendata/",
                "configured": bool(settings.infoclimat_api_key),
                "search_radius_km": 40,
                "max_stations": 5,
                "note": "API restreinte par IP — l'IP du serveur doit etre whitelistee",
            },
            "open_meteo": {
                "name": "Open-Meteo",
                "description": "Previsions meteo (AROME/ARPEGE) et donnees historiques",
                "api_forecast": "https://api.open-meteo.com/v1/meteofrance",
                "api_archive": "https://archive-api.open-meteo.com/v1/archive",
                "fields": [
                    "precipitation", "temperature", "humidity", "wind",
                    "soil_moisture (0-7cm, 7-100cm)", "soil_temperature",
                    "evapotranspiration (ET0)",
                ],
                "basin_points": basin_points,
            },
            "tide": {
                "name": "Open-Meteo Marine",
                "description": "Marees et coefficients estimes depuis hauteur mer",
                "api_url": "https://marine-api.open-meteo.com/v1/marine",
                "ports": [settings.tide_port_bordeaux, settings.tide_port_libourne],
                "reference_point": {"lat": 45.6, "lon": -1.2, "name": "Embouchure Gironde"},
            },
            "meteo_agricole": {
                "name": "La Meteo Agricole",
                "description": "Previsions agricoles + nowcast radar (scraping)",
                "url": settings.meteo_agricole_url,
                "fields": [
                    "pluie min/max (10j)", "probabilite pluie",
                    "vent moyen/rafales/direction",
                    "cumul 30j", "bilan hydrique 30j (%)", "deficit 12 mois (mm)",
                    "previsions 3j/7j",
                ],
            },
            "camera": {
                "name": "Camera RTSP",
                "description": "Analyse video du niveau d'eau",
                "configured": bool(settings.camera_rtsp_url),
            },
        },
        "risk_model": {
            "version": "v3.2",
            "weights": {
                "river_levels": "28% (multi-station weighted avg, Libourne exclue — maree)",
                "rain_24h": "10%",
                "rain_72h": "13% (pluie soutenue > pic 24h)",
                "rain_7d": "5% (cumul multi-jours critique)",
                "tide_coefficient": "5%",
                "tide_phase": "5%",
                "tendency": "13% (vitesse de montee cm/h)",
                "momentum": "8% (stations en hausse simultanee)",
                "soil_saturation": "5% (amplifie ruissellement)",
                "westerly_wind": "3% (surcote Gironde)",
                "video": "5% (index niveau camera)",
            },
            "conjunction_multiplier": "jusqu'a 2.5x (crue active: multi-stations montent + pluie)",
            "stability_dampener": "0.65x a 1.0x (riviere haute mais stable/baisse -> score reduit)",
            "multi_station_trigger": ">=3 stations au-dessus du seuil orange -> conditions de crue",
            "thresholds": risk_thresholds,
        },
    }
