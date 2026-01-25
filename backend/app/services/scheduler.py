"""Background scheduler for data collection.

Periodically collects data from:
- Tide APIs
- Vigicrues
- Weather services
- Camera snapshots
"""

from datetime import datetime
from typing import Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.services.collectors.tide import collect_and_store_tides
from app.services.collectors.vigicrues import collect_and_store_river_levels
from app.services.collectors.weather import collect_and_store_weather

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
    """Job to collect river level data."""
    try:
        count = await collect_and_store_river_levels()
        print(f"[{datetime.now()}] Collected {count} river level data points")
    except Exception as e:
        print(f"[{datetime.now()}] Error collecting river data: {e}")


async def collect_weather_job():
    """Job to collect weather data."""
    try:
        count = await collect_and_store_weather()
        print(f"[{datetime.now()}] Collected {count} weather data points")
    except Exception as e:
        print(f"[{datetime.now()}] Error collecting weather data: {e}")


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
