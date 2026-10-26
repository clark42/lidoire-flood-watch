"""Status and health check endpoints."""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

import httpx
from fastapi import APIRouter

from app.config import get_settings
from app.services import influx_client
from app.services.cache import TTLCache
from app.services.collectors.vigilance import STATUS_MAX_AGE_HOURS as VIGILANCE_MAX_AGE_HOURS
from app.services.risk_core import MODEL_VERSION

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


router = APIRouter()

# Each dependency check must answer within this budget (checks run in parallel).
HEALTH_CHECK_TIMEOUT_S = 2.0


async def _check_influxdb() -> None:
    """InfluxDB 2.x /health must answer 200 with status 'pass'."""
    settings = get_settings()
    url = settings.influxdb_url.rstrip("/") + "/health"
    async with httpx.AsyncClient(timeout=HEALTH_CHECK_TIMEOUT_S) as client:
        resp = await client.get(url)
    resp.raise_for_status()
    status = resp.json().get("status")
    if status != "pass":
        raise RuntimeError(f"influxdb health status={status!r}")


async def _check_postgres() -> None:
    from sqlalchemy import text
    from app.db import get_engine

    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))


async def _check_redis() -> None:
    from app.services.cache import get_redis

    if not await get_redis().ping():
        raise RuntimeError("redis ping returned false")


async def _run_check(name: str, check: Callable[[], Awaitable[None]]) -> dict:
    """Run one check with a timeout; never raises."""
    started = time.perf_counter()
    try:
        await asyncio.wait_for(check(), timeout=HEALTH_CHECK_TIMEOUT_S)
        status, error = "connected", None
    except asyncio.TimeoutError:
        status, error = "timeout", f"no answer within {HEALTH_CHECK_TIMEOUT_S}s"
    except Exception as e:
        # Only the exception type: messages may contain connection strings.
        status, error = "error", type(e).__name__
        logger.warning("Health check %s failed: %r", name, e)
    result = {"status": status, "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
    if error:
        result["error"] = error
    return result


async def check_dependencies() -> dict[str, dict]:
    """Check InfluxDB, PostgreSQL and Redis concurrently."""
    checks = {
        "influxdb": _check_influxdb,
        "database": _check_postgres,
        "redis": _check_redis,
    }
    results = await asyncio.gather(*(_run_check(n, c) for n, c in checks.items()))
    return dict(zip(checks, results))


# ---------------------------------------------------------------------------
# Data freshness: newest stored point of each source
# ---------------------------------------------------------------------------

SOURCES_CACHE_TTL_S = 60
_sources_cache = TTLCache(ttl_seconds=SOURCES_CACHE_TTL_S, max_entries=4)

# Tide points are high/low water times stored ahead of time: the newest one
# is searched up to this far in the future and must not be in the past.
TIDE_HORIZON = timedelta(days=8)


def _netatmo_note(settings) -> Optional[str]:
    if not (settings.netatmo_client_id and settings.netatmo_client_secret):
        return "non configure (NETATMO_CLIENT_ID/SECRET)"
    try:
        from app.api.auth import get_netatmo_tokens
        stored = get_netatmo_tokens() or {}
    except Exception:
        stored = {}
    if not (stored.get("refresh_token") or settings.netatmo_refresh_token):
        return "aucun jeton : autoriser via /api/auth/netatmo"
    return None


def source_specs() -> dict[str, dict]:
    """Sources whose freshness /api/status reports.

    ``max_age_hours``: older newest point = "stale". ``critical`` sources make
    the overall status "degraded" when stale or unavailable.
    """
    settings = get_settings()
    primary = settings.hubeau_primary_station
    primary_name = next(
        (s["name"] for s in settings.hubeau_stations if s["id"] == primary), primary,
    )
    return {
        "river_primary": {
            "label": f"Niveau {primary_name} (Hub'Eau)",
            "measurement": "river_level",
            "tags": {"station_id": [primary]},
            "field": "height_m",
            "max_age_hours": settings.risk_level_max_age_hours,
            "critical": True,
        },
        "rain_openmeteo": {
            "label": "Pluie Open-Meteo",
            "measurement": "rainfall",
            "tags": {"source": ["open-meteo"]},
            "field": "precipitation_mm_h",
            "max_age_hours": 3.0,
        },
        "infoclimat": {
            "label": "Pluie Infoclimat",
            "measurement": "rainfall",
            "tags": {"source": ["infoclimat"]},
            "max_age_hours": 3.0,
            "note": None if settings.infoclimat_api_key else "non configure (INFOCLIMAT_API_KEY)",
        },
        "netatmo": {
            "label": "Pluie Netatmo",
            "measurement": "rainfall",
            "tags": {"source": ["netatmo", "netatmo-lidoire"]},
            "max_age_hours": 1.0,
            "note": _netatmo_note(settings),
        },
        "tide": {
            "label": "Marees (PM/BM a venir)",
            "measurement": "tide",
            "tags": {"port": [settings.tide_port_confluence]},
            "max_age_hours": 0.0,
            "horizon": TIDE_HORIZON,
        },
        "soil": {
            "label": "Sol (Open-Meteo)",
            "measurement": "soil_moisture",
            "tags": {"location": ["moulin"]},
            "max_age_hours": 3.0,
        },
        "risk_score": {
            "label": f"Score de risque v{MODEL_VERSION} (temps reel)",
            "measurement": "risk_score",
            "tags": {"model_version": [MODEL_VERSION], "source": [influx_client.RISK_SOURCE_REALTIME]},
            "field": "score",
            "max_age_hours": 2.0,
            "critical": True,
        },
        # Informative: colour changes plus a keepalive every 6 h (collectors/vigilance.py)
        "vigilance": {
            "label": "Vigilance officielle (Vigicrues, Meteo-France)",
            "measurement": "vigilance",
            "field": "level",
            "max_age_hours": VIGILANCE_MAX_AGE_HOURS,
        },
    }


def _freshness_entry(spec: dict, now: datetime, last: Optional[datetime], error: Optional[str]) -> dict:
    entry = {
        "label": spec["label"],
        "status": "unknown",
        "last_timestamp": last.isoformat() if last else None,
        # Negative for points ahead of now (tide forecasts)
        "age_hours": round((now - last).total_seconds() / 3600, 2) if last else None,
        "max_age_hours": spec["max_age_hours"],
        "critical": spec.get("critical", False),
    }
    if error:
        entry["error"] = error
    elif last is None:
        entry["status"] = "unavailable"
    else:
        entry["status"] = "stale" if entry["age_hours"] > spec["max_age_hours"] else "ok"
    if spec.get("note"):
        entry["note"] = spec["note"]
    return entry


async def _last_time(spec: dict, now: datetime) -> Optional[datetime]:
    return await influx_client.query_last_time(
        spec["measurement"],
        tags=spec.get("tags"),
        field=spec.get("field"),
        stop=now + spec.get("horizon", timedelta(0)),
    )


async def check_sources(influx_ok: bool = True) -> dict[str, dict]:
    """Freshness of each source ("ok" | "stale" | "unavailable" | "unknown").

    One cheap ``last()`` query per source, in parallel, each bounded by
    HEALTH_CHECK_TIMEOUT_S; the result is cached SOURCES_CACHE_TTL_S seconds.
    "unavailable" = no point in the last 30 days, "unknown" = the query
    failed or InfluxDB is down (not cached).
    """
    cached = _sources_cache.get("sources")
    if cached is not None:
        return cached

    specs = source_specs()
    now = _utcnow()
    if not influx_ok:
        return {k: _freshness_entry(s, now, None, "influxdb not connected") for k, s in specs.items()}

    async def one(spec: dict) -> tuple[Optional[datetime], Optional[str]]:
        try:
            last = await asyncio.wait_for(_last_time(spec, now), timeout=HEALTH_CHECK_TIMEOUT_S)
            return last, None
        except asyncio.TimeoutError:
            return None, f"no answer within {HEALTH_CHECK_TIMEOUT_S}s"
        except Exception as e:
            logger.warning("Freshness check failed: %r", e)
            return None, type(e).__name__

    results = await asyncio.gather(*(one(s) for s in specs.values()))
    sources = {
        key: _freshness_entry(spec, now, last, error)
        for (key, spec), (last, error) in zip(specs.items(), results)
    }
    if not any("error" in e for e in sources.values()):
        _sources_cache.set("sources", sources)
    return sources


@router.get("/status")
async def get_status():
    """Get API status and health information.

    ``services.database`` / ``services.influxdb`` / ``services.redis`` are real
    checks ("connected" | "error" | "timeout"); details (latency, error type)
    are under ``services.checks``. ``sources`` gives the freshness of each data
    source (see :func:`check_sources`). Overall status is "degraded" when any
    dependency is not connected or a critical source (``river_primary``,
    ``risk_score``) is stale or unavailable; other sources are informative
    only. ``services.collectors`` is a static declaration kept for
    compatibility.
    """
    settings = get_settings()
    checks = await check_dependencies()
    sources = await check_sources(influx_ok=checks["influxdb"]["status"] == "connected")
    healthy = all(c["status"] == "connected" for c in checks.values()) and not any(
        s["critical"] and s["status"] in ("stale", "unavailable") for s in sources.values()
    )
    return {
        "status": "healthy" if healthy else "degraded",
        "timestamp": _utcnow().isoformat(),
        "version": "1.0.0",
        "location": {
            "lat": settings.moulin_lat,
            "lon": settings.moulin_lon,
            "name": "Moulin de la Lidoire",
        },
        "services": {
            "database": checks["database"]["status"],
            "influxdb": checks["influxdb"]["status"],
            "redis": checks["redis"]["status"],
            "checks": checks,
            "collectors": {
                "tide": "running",
                "vigicrues": "running",
                "weather": "running",
                "camera": "running" if settings.camera_rtsp_url else "disabled",
            },
        },
        "sources": sources,
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
                "note": "Cle API requise ; seules les 5 stations les plus proches sont interrogees",
            },
            "open_meteo": {
                "name": "Open-Meteo",
                "description": "Previsions meteo (AROME/ARPEGE) et donnees historiques",
                "api_forecast": "https://api.open-meteo.com/v1/meteofrance",
                "api_archive": "https://archive-api.open-meteo.com/v1/archive",
                "fields": [
                    "precipitation", "temperature", "humidity", "wind",
                    "soil_moisture (temps reel : ICON 0-9 / 9-81 cm ; timeline : ECMWF 0-7 / 7-100 cm)",
                    "soil_temperature",
                    "evapotranspiration (ET0)",
                ],
                "basin_points": basin_points,
            },
            "tide": {
                "name": "Open-Meteo Marine",
                "description": (
                    "Marees et coefficients estimes depuis hauteur mer, heures decalees "
                    f"de +{settings.tide_phase_lag_hours.get('open-meteo-marine', 0):g} h "
                    "(embouchure -> confluence Lidoire)"
                ),
                "api_url": "https://marine-api.open-meteo.com/v1/marine",
                "ports": [settings.tide_port_confluence],
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
