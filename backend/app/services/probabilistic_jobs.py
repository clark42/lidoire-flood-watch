"""Scheduler jobs of the probabilistic / observed-rain series.

- ``radar_collector`` (15 min): OPERA ACRR basin rain -> ``radar_rain``.
- ``glofas_collector`` (6 h): GloFAS v4 Lidoire run -> ``glofas_forecast``
  (+ ``glofas_climatology`` once).
- ``ensemble_rain_collector`` (6 h): Open-Meteo Ensemble basin rain ->
  ``ensemble_rain``.

Each job is registered only when its setting is enabled. First runs are
delayed by ``FIRST_RUN_DELAY`` (startup collections of the risk inputs go
first). None of them touches the risk score.
"""

import logging
from datetime import datetime, timedelta

from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings

logger = logging.getLogger(__name__)

FIRST_RUN_DELAY = timedelta(minutes=1)

RADAR_JOB_ID = "radar_collector"
GLOFAS_JOB_ID = "glofas_collector"
ENSEMBLE_JOB_ID = "ensemble_rain_collector"


async def collect_radar_job():
    try:
        from app.services.collectors.opera_radar import collect_and_store_radar

        count = await collect_and_store_radar()
        logger.info("Collected %s OPERA radar basin rain points", count)
    except Exception as e:
        logger.error("Error collecting OPERA radar: %r", e)


async def collect_glofas_job():
    try:
        from app.services.collectors.glofas import collect_and_store_glofas

        count = await collect_and_store_glofas()
        logger.info("Collected %s GloFAS forecast points", count)
    except Exception as e:
        logger.error("Error collecting GloFAS: %r", e)


async def collect_ensemble_rain_job():
    try:
        from app.services.collectors.ensemble_rain import collect_and_store_ensemble_rain

        count = await collect_and_store_ensemble_rain()
        logger.info("Collected %s ensemble rain points", count)
    except Exception as e:
        logger.error("Error collecting ensemble rain: %r", e)


def schedule_probabilistic_jobs(scheduler, now: datetime) -> list[str]:
    """Register the enabled jobs on ``scheduler``; returns their ids."""
    settings = get_settings()
    first = now + FIRST_RUN_DELAY
    jobs = [
        (settings.radar_enabled, collect_radar_job, IntervalTrigger(minutes=15), RADAR_JOB_ID,
         "Collect OPERA radar basin rain"),
        (settings.glofas_enabled, collect_glofas_job, IntervalTrigger(hours=6), GLOFAS_JOB_ID,
         "Collect GloFAS Lidoire forecast"),
        (settings.ensemble_rain_enabled, collect_ensemble_rain_job, IntervalTrigger(hours=6),
         ENSEMBLE_JOB_ID, "Collect ensemble basin rain"),
    ]
    added = []
    for enabled, func, trigger, job_id, name in jobs:
        if not enabled:
            logger.info("%s disabled by configuration", name)
            continue
        scheduler.add_job(
            func, trigger=trigger, id=job_id, name=name, next_run_time=first, replace_existing=True,
        )
        added.append(job_id)
    return added
