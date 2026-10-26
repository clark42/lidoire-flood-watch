"""Comprehensive data backfill service.

Fetches historical data from all sources since November 2022:
- River levels from Hydroportail (7 stations, sub-hourly)
- Rainfall from Open-Meteo Archive (hourly)
- Tides from Open-Meteo Marine (hourly)
- Soil moisture from Open-Meteo (hourly)

Then recalculates risk scores with the current model (risk_core, v3.4):
features at each step only use observed points at or before that step.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import get_settings
from app.services import influx_client
from app.services.collectors.hubeau import fetch_hydroportail_data, fetch_historical_data
from app.services.collectors.weather import (
    fetch_and_store_historical_rainfall,
    fetch_and_store_soil_historical,
)
from app.services.collectors.tide import fetch_and_store_tides_range

logger = logging.getLogger(__name__)


def _update_progress(step: str):
    """Update the shared progress status (read by /backfill/status endpoint)."""
    try:
        from app.api.episodes import _backfill_status
        _backfill_status["progress"] = step
    except Exception:
        pass


async def backfill_river_levels(
    start_date: datetime,
    end_date: datetime,
    batch_days: int = 30,
) -> dict:
    """
    Backfill river levels for all stations from Hydroportail.

    Args:
        start_date: Start of backfill period
        end_date: End of backfill period
        batch_days: Days per batch to avoid API limits

    Returns:
        Dict with station stats
    """
    settings = get_settings()
    results = {}

    for station in settings.hubeau_stations:
        station_id = station["id"]
        station_name = station["name"]
        river = station["river"]

        logger.info("Backfilling river levels: %s (%s)", station_name, station_id)
        total_points = 0

        # Process in batches
        current_start = start_date
        while current_start < end_date:
            current_end = min(current_start + timedelta(days=batch_days), end_date)

            logger.info("  Fetching %s -> %s", current_start.date(), current_end.date())

            # Try Hydroportail first (sub-hourly data)
            levels = await fetch_hydroportail_data(station_id, current_start, current_end)

            if not levels:
                # Fallback to Hub'Eau obs_elab (daily max)
                logger.info("  Hydroportail failed, trying Hub'Eau obs_elab")
                levels = await fetch_historical_data(
                    station_id, current_start, current_end, granularity="daily_max"
                )

            if levels:
                # Convert to batch format and write
                batch = [
                    {
                        "station_id": lv.station_id,
                        "station_name": lv.station_name,
                        "river": lv.river,
                        "height_m": lv.height_m,
                        "timestamp": lv.timestamp,
                        "flow_m3s": lv.flow_m3s,
                    }
                    for lv in levels
                ]

                written = await influx_client.write_river_levels_batch(batch)
                total_points += written
                logger.info("  Stored %d points for %s -> %s", written, current_start.date(), current_end.date())
            else:
                logger.info("  No data available for this period")

            current_start = current_end

            # Small delay to avoid overwhelming APIs
            await asyncio.sleep(0.5)

        results[station_id] = {
            "name": station_name,
            "river": river,
            "points": total_points,
        }

    return results


async def backfill_rainfall(
    start_date: datetime,
    end_date: datetime,
    batch_days: int = 90,
) -> int:
    """
    Backfill rainfall data from Open-Meteo Archive.

    Args:
        start_date: Start of backfill period
        end_date: End of backfill period
        batch_days: Days per batch (Open-Meteo archive limit ~90 days)

    Returns:
        Total points stored
    """
    logger.info("Backfilling rainfall data")
    total = 0

    current_start = start_date
    while current_start < end_date:
        current_end = min(current_start + timedelta(days=batch_days), end_date)

        stored = await fetch_and_store_historical_rainfall(current_start, current_end)
        total += stored

        current_start = current_end
        await asyncio.sleep(0.5)

    logger.info("Total rainfall points stored: %d", total)
    return total


async def backfill_tides(
    start_date: datetime,
    end_date: datetime,
    batch_days: int = 30,
) -> int:
    """
    Backfill tide data from Open-Meteo Marine.

    Note: Open-Meteo Marine has limited historical data (~2-3 months back).
    For older data, only predictions can be regenerated.
    """
    logger.info("Backfilling tide data")
    total = 0

    current_start = start_date
    while current_start < end_date:
        current_end = min(current_start + timedelta(days=batch_days), end_date)

        stored = await fetch_and_store_tides_range(current_start, current_end)
        total += stored

        current_start = current_end
        await asyncio.sleep(0.5)

    logger.info("Total tide points stored: %d", total)
    return total


async def backfill_soil(
    start_date: datetime,
    end_date: datetime,
    batch_days: int = 90,
) -> int:
    """
    Backfill soil moisture and temperature from Open-Meteo.
    """
    logger.info("Backfilling soil data")
    total = 0

    current_start = start_date
    while current_start < end_date:
        current_end = min(current_start + timedelta(days=batch_days), end_date)

        stored = await fetch_and_store_soil_historical(current_start, current_end)
        total += stored

        current_start = current_end
        await asyncio.sleep(0.5)

    logger.info("Total soil points stored: %d", total)
    return total


async def recalculate_risk_scores(
    start_date: datetime,
    end_date: datetime,
) -> int:
    """
    Recalculate risk scores with the current model for the backfilled period.

    Uses risk_v2.backfill_risk_scores which, for each time step:
    - builds features from non-tidal stations (latest level <= t, max age
      3h; rate by regression on points <= t), hourly rain windows ending
      at t (mean per hour across sources), tide predictions;
    - calls risk_core.compute_risk;
    - stores the scores in one batched write, tagged model_version and
      source=backfill (re-runs overwrite; realtime points are preferred on
      read for an hour that has both).
    """
    from app.services.risk_v2 import backfill_risk_scores

    logger.info("Recalculating risk scores %s -> %s", start_date, end_date)

    total = await backfill_risk_scores(start_date, end_date)

    logger.info("Total risk scores stored: %d", total)
    return total


async def run_full_backfill(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    skip_rivers: bool = False,
    skip_rainfall: bool = False,
    skip_tides: bool = False,
    skip_soil: bool = False,
    skip_risk: bool = False,
) -> dict:
    """
    Run complete backfill of all data sources.

    Default period: November 2022 to now.

    Args:
        start_date: Start of backfill (default: 2022-11-01)
        end_date: End of backfill (default: now)
        skip_*: Skip specific data sources

    Returns:
        Summary of all backfill operations
    """
    if start_date is None:
        start_date = datetime(2022, 11, 1)
    if end_date is None:
        end_date = datetime.now(timezone.utc).replace(tzinfo=None)

    logger.info("Starting full backfill: %s -> %s", start_date.date(), end_date.date())

    results = {
        "period": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
        },
        "rivers": None,
        "rainfall": None,
        "tides": None,
        "soil": None,
        "risk_scores": None,
    }

    # 1. River levels (most important)
    if not skip_rivers:
        _update_progress("rivers: backfilling 7 stations...")
        results["rivers"] = await backfill_river_levels(start_date, end_date)

    # 2. Rainfall
    if not skip_rainfall:
        _update_progress("rainfall: backfilling Open-Meteo archive...")
        results["rainfall"] = await backfill_rainfall(start_date, end_date)

    # 3. Tides
    if not skip_tides:
        _update_progress("tides: backfilling Open-Meteo Marine...")
        results["tides"] = await backfill_tides(start_date, end_date)

    # 4. Soil moisture
    if not skip_soil:
        _update_progress("soil: backfilling soil moisture/temp...")
        results["soil"] = await backfill_soil(start_date, end_date)

    # 5. Recalculate risk scores
    if not skip_risk:
        _update_progress("risk: recalculating v3.4 scores (slowest step)...")
        results["risk_scores"] = await recalculate_risk_scores(start_date, end_date)

    _update_progress("done")

    logger.info("Full backfill complete")

    return results
