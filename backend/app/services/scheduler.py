"""Background scheduler for data collection.

Periodically collects data from:
- Tide APIs
- Hub'Eau (river levels)
- Weather services
- Netatmo (rainfall)
- Camera snapshots
"""

from datetime import datetime, timedelta
from typing import Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.services.collectors.tide import collect_and_store_tides
from app.services.collectors.weather import collect_and_store_weather
from app.services.collectors.netatmo import get_basin_rainfall, store_rainfall_to_influx
from app.services.collectors.meteo_agricole import collect_and_store_meteo_agricole, collect_and_store_nowcast_rain
from app.services.data_store import _fetch_and_store_river_levels

# Global scheduler instance
_scheduler: Optional[AsyncIOScheduler] = None


async def collect_tide_job():
    """Job to collect tide data."""
    try:
        count = await collect_and_store_tides()
        print(f"[{datetime.now()}] Collected {count} tide data points")
    except Exception as e:
        print(f"[{datetime.now()}] Error collecting tide data: {e}")


async def collect_river_job():
    """Job to collect river level data from Hub'Eau and store in InfluxDB."""
    try:
        now = datetime.utcnow()
        start = now - timedelta(hours=1)
        count = await _fetch_and_store_river_levels(start, now)
        print(f"[{datetime.now()}] Collected {count} river level data points (Hub'Eau)")
    except Exception as e:
        print(f"[{datetime.now()}] Error collecting river data: {e}")


async def collect_weather_job():
    """Job to collect weather data."""
    try:
        count = await collect_and_store_weather()
        print(f"[{datetime.now()}] Collected {count} weather data points")
    except Exception as e:
        print(f"[{datetime.now()}] Error collecting weather data: {e}")


async def collect_netatmo_job():
    """Job to collect Netatmo rainfall data."""
    try:
        basin_data = await get_basin_rainfall()
        if basin_data.get("available"):
            count = await store_rainfall_to_influx(basin_data)
            lidoire_count = len(basin_data.get("lidoire_stations", []))
            print(f"[{datetime.now()}] Collected {count} Netatmo data points ({lidoire_count} Lidoire stations)")
        else:
            print(f"[{datetime.now()}] Netatmo not available (token missing?)")
    except Exception as e:
        print(f"[{datetime.now()}] Error collecting Netatmo data: {e}")


async def collect_meteo_agricole_job():
    """Job to collect La Meteo Agricole data."""
    try:
        count = await collect_and_store_meteo_agricole()
        print(f"[{datetime.now()}] Collected {count} Meteo Agricole data points")
    except Exception as e:
        print(f"[{datetime.now()}] Error collecting Meteo Agricole data: {e}")


async def collect_nowcast_rain_job():
    """Job to collect nowcast rain radar data (minute-by-minute)."""
    try:
        count = await collect_and_store_nowcast_rain()
        print(f"[{datetime.now()}] Collected {count} nowcast rain data points")
    except Exception as e:
        print(f"[{datetime.now()}] Error collecting nowcast rain data: {e}")


async def collect_camera_job():
    """Job to capture camera snapshot and analyze."""
    from app.config import get_settings

    settings = get_settings()
    if not settings.camera_rtsp_url:
        return

    try:
        from app.services.video_analyzer import capture_and_analyze
        result = await capture_and_analyze()
        print(f"[{datetime.now()}] Camera analysis: level={result.water_level_index:.1f}")
    except Exception as e:
        print(f"[{datetime.now()}] Error analyzing camera: {e}")


async def start_scheduler():
    """Start the background scheduler."""
    global _scheduler

    if _scheduler is not None:
        return

    _scheduler = AsyncIOScheduler()

    # Tide data: every 6 hours (predictions are stable)
    _scheduler.add_job(
        collect_tide_job,
        trigger=IntervalTrigger(hours=6),
        id="tide_collector",
        name="Collect tide data",
        replace_existing=True,
    )

    # River levels: every 15 minutes
    _scheduler.add_job(
        collect_river_job,
        trigger=IntervalTrigger(minutes=15),
        id="river_collector",
        name="Collect river levels",
        replace_existing=True,
    )

    # Weather data: every hour
    _scheduler.add_job(
        collect_weather_job,
        trigger=IntervalTrigger(hours=1),
        id="weather_collector",
        name="Collect weather data",
        replace_existing=True,
    )

    # Netatmo rainfall: every 15 minutes
    _scheduler.add_job(
        collect_netatmo_job,
        trigger=IntervalTrigger(minutes=15),
        id="netatmo_collector",
        name="Collect Netatmo rainfall",
        replace_existing=True,
    )

    # Meteo Agricole: every 6 hours (daily data, stable)
    _scheduler.add_job(
        collect_meteo_agricole_job,
        trigger=IntervalTrigger(hours=6),
        id="meteo_agricole_collector",
        name="Collect Meteo Agricole data",
        replace_existing=True,
    )

    # Nowcast rain radar: every 5 minutes (volatile radar data)
    _scheduler.add_job(
        collect_nowcast_rain_job,
        trigger=IntervalTrigger(minutes=5),
        id="nowcast_rain_collector",
        name="Collect nowcast rain radar",
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
    print("Background scheduler started")

    # Run initial collection
    await collect_tide_job()
    await collect_river_job()
    await collect_weather_job()
    await collect_netatmo_job()
    await collect_meteo_agricole_job()
    await collect_nowcast_rain_job()


async def stop_scheduler():
    """Stop the background scheduler."""
    global _scheduler

    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        print("Background scheduler stopped")


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
