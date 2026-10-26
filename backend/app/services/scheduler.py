"""Background scheduler for data collection.

Periodically collects data from:
- Tide APIs
- Hub'Eau (river levels)
- Weather services
- Netatmo, Infoclimat (rainfall; request paths only read their caches)
- Camera snapshots

and computes + stores the realtime risk score every hour (the only writer
of realtime risk points; API reads never store).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.services.collectors.tide import collect_and_store_tides
from app.services.collectors.weather import collect_and_store_weather
from app.services.collectors.netatmo import get_basin_rainfall, store_rainfall_to_influx
from app.services.collectors.meteo_agricole import collect_and_store_meteo_agricole, collect_and_store_nowcast_rain
from app.services.data_store import _fetch_and_store_river_levels

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler: Optional[AsyncIOScheduler] = None

# Delay before the first risk computation, so the initial collections
# (started immediately, in the background) have populated caches.
RISK_JOB_FIRST_RUN_DELAY = timedelta(minutes=2)


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def collect_tide_job():
    """Job to collect tide data."""
    try:
        count = await collect_and_store_tides()
        logger.info("Collected %s tide data points", count)
    except Exception as e:
        logger.error("Error collecting tide data: %s", e)


async def collect_river_job():
    """Job to collect river level data from Hub'Eau and store in InfluxDB."""
    try:
        now = _utcnow()
        start = now - timedelta(hours=1)
        count = await _fetch_and_store_river_levels(start, now)
        logger.info("Collected %s river level data points (Hub'Eau)", count)
    except Exception as e:
        logger.error("Error collecting river data: %s", e)


async def collect_weather_job():
    """Job to collect weather data."""
    try:
        count = await collect_and_store_weather()
        logger.info("Collected %s weather data points", count)
    except Exception as e:
        logger.error("Error collecting weather data: %s", e)


async def collect_netatmo_job():
    """Job to collect Netatmo rainfall data."""
    try:
        basin_data = await get_basin_rainfall()
        if basin_data.get("available"):
            count = await store_rainfall_to_influx(basin_data)
            lidoire_count = len(basin_data.get("lidoire_stations", []))
            logger.info("Collected %s Netatmo data points (%s Lidoire stations)", count, lidoire_count)
        else:
            logger.info("Netatmo not available (token missing?)")
    except Exception as e:
        logger.error("Error collecting Netatmo data: %s", e)


async def collect_infoclimat_job():
    """Job to refresh the Infoclimat basin rainfall (request paths only read its cache)."""
    try:
        from app.services.collectors.infoclimat import get_basin_rainfall_infoclimat

        data = await get_basin_rainfall_infoclimat()
        if data.get("available"):
            logger.info("Collected Infoclimat rainfall (%s stations)", data.get("stations_count", 0))
        else:
            logger.info("Infoclimat not available: %s", data.get("reason"))
    except Exception as e:
        logger.error("Error collecting Infoclimat data: %r", e)


async def collect_meteo_agricole_job():
    """Job to collect La Meteo Agricole data."""
    try:
        count = await collect_and_store_meteo_agricole()
        logger.info("Collected %s Meteo Agricole data points", count)
    except Exception as e:
        logger.error("Error collecting Meteo Agricole data: %s", e)


async def collect_nowcast_rain_job():
    """Job to collect nowcast rain radar data (minute-by-minute)."""
    try:
        count = await collect_and_store_nowcast_rain()
        logger.info("Collected %s nowcast rain data points", count)
    except Exception as e:
        logger.error("Error collecting nowcast rain data: %s", e)


async def compute_risk_job():
    """Job to compute the realtime risk score and store it (model v3.3)."""
    try:
        from app.services.risk_v2 import compute_current_risk

        risk, _ = await compute_current_risk(store=True)
        logger.info(
            "Risk score computed and stored: %.1f (%s, model %s)",
            risk.score, risk.level.value, risk.model_version,
        )
    except Exception as e:
        logger.error("Error computing risk score: %s", e)


async def collect_camera_job():
    """Job to capture camera snapshot and analyze."""
    from app.config import get_settings

    settings = get_settings()
    if not settings.camera_rtsp_url:
        return

    try:
        from app.services.video_analyzer import capture_and_analyze
        result = await capture_and_analyze()
        logger.info("Camera analysis: level=%.1f", result.water_level_index)
    except Exception as e:
        logger.error("Error analyzing camera: %s", e)


async def start_scheduler():
    """Start the background scheduler.

    Initial collections run immediately as scheduled jobs
    (``next_run_time=now``) instead of being awaited here, so the API starts
    serving without waiting for external sources.
    """
    global _scheduler

    if _scheduler is not None:
        return

    _scheduler = AsyncIOScheduler(timezone=timezone.utc)
    now = datetime.now(timezone.utc)

    # Tide data: every 6 hours (predictions are stable)
    _scheduler.add_job(
        collect_tide_job,
        trigger=IntervalTrigger(hours=6),
        id="tide_collector",
        next_run_time=now,
        name="Collect tide data",
        replace_existing=True,
    )

    # River levels: every 15 minutes
    _scheduler.add_job(
        collect_river_job,
        trigger=IntervalTrigger(minutes=15),
        id="river_collector",
        next_run_time=now,
        name="Collect river levels",
        replace_existing=True,
    )

    # Weather data: every hour
    _scheduler.add_job(
        collect_weather_job,
        trigger=IntervalTrigger(hours=1),
        id="weather_collector",
        next_run_time=now,
        name="Collect weather data",
        replace_existing=True,
    )

    # Netatmo rainfall: every 15 minutes
    _scheduler.add_job(
        collect_netatmo_job,
        trigger=IntervalTrigger(minutes=15),
        id="netatmo_collector",
        next_run_time=now,
        name="Collect Netatmo rainfall",
        replace_existing=True,
    )

    # Infoclimat rainfall: every 15 minutes
    _scheduler.add_job(
        collect_infoclimat_job,
        trigger=IntervalTrigger(minutes=15),
        id="infoclimat_collector",
        next_run_time=now,
        name="Collect Infoclimat rainfall",
        replace_existing=True,
    )

    # Meteo Agricole: every 6 hours (daily data, stable)
    _scheduler.add_job(
        collect_meteo_agricole_job,
        trigger=IntervalTrigger(hours=6),
        id="meteo_agricole_collector",
        next_run_time=now,
        name="Collect Meteo Agricole data",
        replace_existing=True,
    )

    # Nowcast rain radar: every 5 minutes (volatile radar data)
    _scheduler.add_job(
        collect_nowcast_rain_job,
        trigger=IntervalTrigger(minutes=5),
        id="nowcast_rain_collector",
        next_run_time=now,
        name="Collect nowcast rain radar",
        replace_existing=True,
    )

    # Realtime risk score: computed and stored every hour
    _scheduler.add_job(
        compute_risk_job,
        trigger=IntervalTrigger(hours=1),
        id="risk_scorer",
        name="Compute and store realtime risk score",
        next_run_time=now + RISK_JOB_FIRST_RUN_DELAY,
        replace_existing=True,
    )

    # Camera: every minute (configurable)
    from app.config import get_settings
    settings = get_settings()
    if settings.camera_rtsp_url:
        _scheduler.add_job(
            collect_camera_job,
            trigger=IntervalTrigger(seconds=settings.camera_snapshot_interval),
            id="camera_analyzer",
            name="Analyze camera",
            replace_existing=True,
        )

    _scheduler.start()
    logger.info("Background scheduler started (initial collections running in background)")


async def stop_scheduler():
    """Stop the background scheduler."""
    global _scheduler

    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Background scheduler stopped")


def get_scheduler_status() -> dict:
    """Get scheduler status and job info."""
    if _scheduler is None:
        return {"status": "stopped", "jobs": []}

    jobs = []
    for job in _scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
        })

    return {
        "status": "running",
        "jobs": jobs,
    }
