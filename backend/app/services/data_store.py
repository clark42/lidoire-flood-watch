"""Data storage service with cache-through pattern.

All data is read from InfluxDB. If not present, it's fetched from
external APIs and stored for future use.

Episodes are persisted in PostgreSQL via SQLAlchemy async.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, delete as sa_delete, inspect as sa_inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, AsyncConnection

from app.models.schemas import (
    EpisodeOutcome,
    FloodEpisode,
    FloodEpisodeCreate,
    TimelinePoint,
    TideData,
    RiverLevel,
    RainfallObservation,
)
from app.models.orm import FloodEpisodeRow
from app.db import get_session_factory
from app.services import influx_client
from app.services.labels import LEGACY_PREFIX_SOURCE, parse_legacy_outcome

_episode_logger = logging.getLogger(__name__ + ".episodes")

# Ground-truth columns added after the initial flood_episodes schema.
# create_all() never alters existing tables, so they are added at startup.
_EPISODE_GT_COLUMNS: dict[str, str] = {
    "outcome": "VARCHAR(32)",
    "observed_at": "TIMESTAMP",
    "water_height_cm": "DOUBLE PRECISION",
    "outcome_source": "VARCHAR(100)",
}

# Fields of FloodEpisodeCreate that are only updated when explicitly sent
_EPISODE_GT_FIELDS = ("outcome", "observed_at", "water_height_cm", "outcome_source")


def migrate_episode_schema_sync(conn: Connection) -> dict:
    """Idempotent startup migration of the flood_episodes table.

    1. Adds the ground-truth columns if missing
       (``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` on PostgreSQL, column
       inspection on other dialects).
    2. Converts legacy ``[CONFIRMED: ...]`` notes prefixes into ``outcome``
       for rows whose outcome is still null, and strips the prefixes.
    """
    table = FloodEpisodeRow.__tablename__
    added: list[str] = []
    if conn.dialect.name == "postgresql":
        for col, col_type in _EPISODE_GT_COLUMNS.items():
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}"))
    else:
        existing = {c["name"] for c in sa_inspect(conn).get_columns(table)}
        for col, col_type in _EPISODE_GT_COLUMNS.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
                added.append(col)

    rows = conn.execute(
        text(f"SELECT id, notes FROM {table} WHERE outcome IS NULL AND notes LIKE :pattern"),
        {"pattern": "%[CONFIRMED:%"},
    ).all()
    migrated = 0
    update_stmt = text(
        f"UPDATE {table} SET outcome = :outcome, notes = :notes, outcome_source = :source "
        "WHERE id = :id"
    )
    for row_id, notes in rows:
        outcome, cleaned = parse_legacy_outcome(notes)
        if outcome is None:
            continue
        conn.execute(
            update_stmt,
            {"outcome": outcome.value, "notes": cleaned, "source": LEGACY_PREFIX_SOURCE, "id": row_id},
        )
        migrated += 1

    if added or migrated:
        _episode_logger.info(
            "flood_episodes migration: columns added=%s, legacy outcomes migrated=%d",
            added, migrated,
        )
    return {"columns_added": added, "legacy_rows_migrated": migrated}


async def migrate_episode_schema(conn: AsyncConnection) -> dict:
    """Async wrapper of migrate_episode_schema_sync (call after create_all)."""
    return await conn.run_sync(migrate_episode_schema_sync)


def _parse_outcome(value: Optional[str]) -> Optional[EpisodeOutcome]:
    if value is None:
        return None
    try:
        return EpisodeOutcome(value)
    except ValueError:
        _episode_logger.warning("Ignoring invalid episode outcome %r", value)
        return None

import asyncio
import logging

logger = logging.getLogger(__name__)

# Max concurrent Hub'Eau / Hydroportail requests during river backfills.
HUBEAU_CONCURRENCY = 3


def _row_to_episode(row: FloodEpisodeRow) -> FloodEpisode:
    outcome = _parse_outcome(getattr(row, "outcome", None))
    outcome_source = getattr(row, "outcome_source", None)
    notes = row.notes
    if outcome is None:
        # Fallback for rows not migrated yet: derive outcome from the legacy prefix
        legacy_outcome, cleaned = parse_legacy_outcome(notes)
        if legacy_outcome is not None:
            outcome, notes, outcome_source = legacy_outcome, cleaned, LEGACY_PREFIX_SOURCE
    return FloodEpisode(
        id=row.id,
        start_time=row.start_time,
        peak_time=row.peak_time,
        end_time=row.end_time,
        severity=row.severity,
        description=row.description,
        notes=notes,
        created_at=row.created_at,
        outcome=outcome,
        observed_at=getattr(row, "observed_at", None),
        water_height_cm=getattr(row, "water_height_cm", None),
        outcome_source=outcome_source,
    )


def _apply_ground_truth(row: FloodEpisodeRow, episode: FloodEpisodeCreate, only_set: bool) -> None:
    """Copy ground-truth fields to the row (only explicitly sent ones if only_set)."""
    for field in _EPISODE_GT_FIELDS:
        if only_set and field not in episode.model_fields_set:
            continue
        value = getattr(episode, field)
        if field == "outcome" and value is not None:
            value = EpisodeOutcome(value).value
        elif field == "observed_at":
            value = _strip_tz(value)
        setattr(row, field, value)


async def get_all_episodes(
    limit: int = 50,
    offset: int = 0,
    min_severity: Optional[int] = None,
) -> list[FloodEpisode]:
    """Get all recorded flood episodes from PostgreSQL."""
    factory = get_session_factory()
    async with factory() as session:
        stmt = select(FloodEpisodeRow).order_by(FloodEpisodeRow.start_time.desc())
        if min_severity:
            stmt = stmt.where(FloodEpisodeRow.severity >= min_severity)
        stmt = stmt.offset(offset).limit(limit)
        result = await session.execute(stmt)
        rows = result.scalars().all()
        return [_row_to_episode(r) for r in rows]


async def get_episode_by_id(episode_id: int) -> Optional[FloodEpisode]:
    """Get a specific episode by ID."""
    factory = get_session_factory()
    async with factory() as session:
        row = await session.get(FloodEpisodeRow, episode_id)
        return _row_to_episode(row) if row else None


def _strip_tz(dt: Optional[datetime]) -> Optional[datetime]:
    """Strip timezone info for PostgreSQL naive datetime columns."""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


async def create_episode(episode: FloodEpisodeCreate) -> FloodEpisode:
    """Create a new flood episode in PostgreSQL."""
    factory = get_session_factory()
    async with factory() as session:
        row = FloodEpisodeRow(
            start_time=_strip_tz(episode.start_time),
            peak_time=_strip_tz(episode.peak_time),
            end_time=_strip_tz(episode.end_time),
            severity=episode.severity,
            description=episode.description,
            notes=episode.notes,
            created_at=datetime.utcnow(),
        )
        _apply_ground_truth(row, episode, only_set=False)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return _row_to_episode(row)


async def update_episode(episode_id: int, episode: FloodEpisodeCreate) -> FloodEpisode:
    """Update an existing episode.

    Ground-truth fields are only changed when present in the payload
    (``model_fields_set``), so partial/legacy clients keep existing labels.
    """
    factory = get_session_factory()
    async with factory() as session:
        row = await session.get(FloodEpisodeRow, episode_id)
        if not row:
            raise ValueError(f"Episode {episode_id} not found")
        row.start_time = _strip_tz(episode.start_time)
        row.peak_time = _strip_tz(episode.peak_time)
        row.end_time = _strip_tz(episode.end_time)
        row.severity = episode.severity
        row.description = episode.description
        row.notes = episode.notes
        _apply_ground_truth(row, episode, only_set=True)
        await session.commit()
        await session.refresh(row)
        return _row_to_episode(row)


async def delete_episode(episode_id: int) -> None:
    """Delete an episode."""
    factory = get_session_factory()
    async with factory() as session:
        stmt = sa_delete(FloodEpisodeRow).where(FloodEpisodeRow.id == episode_id)
        await session.execute(stmt)
        await session.commit()


async def get_episodes_in_range(start: datetime, end: datetime) -> list[FloodEpisode]:
    """Get episodes that occurred within a time range."""
    factory = get_session_factory()
    async with factory() as session:
        stmt = (
            select(FloodEpisodeRow)
            .where(FloodEpisodeRow.start_time >= start)
            .where(FloodEpisodeRow.start_time <= end)
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()
        return [_row_to_episode(r) for r in rows]


async def get_timeline_data(
    start: datetime,
    end: datetime,
    resolution: str = "1h",
) -> list[TimelinePoint]:
    """
    Get timeline data from InfluxDB.
    If no data exists, fetch from APIs and store.
    """
    from app.config import get_settings
    settings = get_settings()

    # Naive UTC datetimes (aware values are converted, not just stripped)
    start = influx_client.to_naive_utc(start)
    end = influx_client.to_naive_utc(end)

    # Resolution to timedelta
    resolution_map = {
        "15m": timedelta(minutes=15),
        "1h": timedelta(hours=1),
        "6h": timedelta(hours=6),
        "1d": timedelta(days=1),
    }
    delta = resolution_map.get(resolution, timedelta(hours=1))

    # Try to get data from InfluxDB first (queries run concurrently)
    river_data, rainfall_data, risk_data = await asyncio.gather(
        influx_client.query_all_stations_levels(start, end),
        influx_client.query_rainfall(start, end),
        influx_client.query_risk_scores(start, end),
    )

    # If no river data in InfluxDB, fetch from Hub'Eau and store
    if not river_data:
        logger.info("No river data in InfluxDB, fetching from Hub'Eau...")
        await _fetch_and_store_river_levels(start, end)
        river_data = await influx_client.query_all_stations_levels(start, end)

    # Get primary station data for timeline
    primary_station_id = settings.hubeau_primary_station
    primary_levels = river_data.get(primary_station_id, [])

    # Build timeline points
    points = []
    current = start

    # Create lookup dicts for faster access
    river_by_time = {
        _round_to_resolution(lv["timestamp"], delta): lv["height_m"]
        for lv in primary_levels
    }
    rain_by_time = {
        _round_to_resolution(r["timestamp"], delta): r.get("precipitation_mm_h", 0)
        for r in rainfall_data
    }
    risk_by_time = {
        _round_to_resolution(r["timestamp"], delta): r.get("score", 0)
        for r in risk_data
    }

    while current <= end:
        rounded = _round_to_resolution(current, delta)

        points.append(
            TimelinePoint(
                timestamp=current,
                tide_height_m=None,  # TODO: Add tide data
                river_level_m=river_by_time.get(rounded),
                rainfall_mm_h=rain_by_time.get(rounded),
                risk_score=risk_by_time.get(rounded),
                video_level=None,
            )
        )
        current += delta

    return points


def _round_to_resolution(dt: datetime, delta: timedelta) -> datetime:
    """Round datetime to nearest resolution bucket."""
    dt = influx_client.to_naive_utc(dt)

    if delta >= timedelta(days=1):
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    elif delta >= timedelta(hours=6):
        hour = (dt.hour // 6) * 6
        return dt.replace(hour=hour, minute=0, second=0, microsecond=0)
    elif delta >= timedelta(hours=1):
        return dt.replace(minute=0, second=0, microsecond=0)
    else:
        minute = (dt.minute // 15) * 15
        return dt.replace(minute=minute, second=0, microsecond=0)


def _selected_stations(station_ids: Optional[list[str]]) -> list[dict]:
    from app.config import get_settings

    stations = get_settings().hubeau_stations
    if not station_ids:
        return list(stations)
    return [s for s in stations if s["id"] in station_ids]


def _level_dicts(station: dict, levels) -> list[dict]:
    return [
        {
            "station_id": station["id"],
            "station_name": station["name"],
            "river": station["river"],
            "height_m": lv.height_m,
            "timestamp": lv.timestamp,
            "flow_m3s": lv.flow_m3s,
        }
        for lv in levels
    ]


async def _fetch_and_store_river_levels(
    start: datetime,
    end: datetime,
    station_ids: Optional[list[str]] = None,
) -> int:
    """Fetch river levels from Hub'Eau (realtime TR) and store in InfluxDB.

    Stations are fetched concurrently (bounded by ``HUBEAU_CONCURRENCY``).

    Args:
        start: Start datetime (TR fetch goes from ``start`` to now)
        end: End datetime (unused by the TR API, kept for symmetry)
        station_ids: Optional subset of configured station IDs (default: all)
    """
    from app.services.collectors.hubeau import fetch_realtime_observations

    now = datetime.utcnow()
    hours_back = max(1, int((now - influx_client.to_naive_utc(start)).total_seconds() / 3600))
    semaphore = asyncio.Semaphore(HUBEAU_CONCURRENCY)

    async def _one(station: dict) -> int:
        async with semaphore:
            logger.info("Fetching realtime data for station %s...", station["id"])
            levels = await fetch_realtime_observations(station["id"], hours_back)
        if not levels:
            return 0
        written = await influx_client.write_river_levels_batch(_level_dicts(station, levels))
        logger.info("  Stored %d points for %s", written, station["name"])
        return written

    results = await asyncio.gather(
        *(_one(s) for s in _selected_stations(station_ids)), return_exceptions=True
    )
    total_stored = 0
    for r in results:
        if isinstance(r, Exception):
            logger.warning("Realtime river fetch failed: %s", r)
        else:
            total_stored += r
    return total_stored


async def _fetch_and_store_river_levels_elab(
    start: datetime,
    end: datetime,
    station_ids: list[str] | None = None,
) -> int:
    """Fetch historical river levels and store in InfluxDB.

    Priority:
    1. Hydroportail (hydro.eaufrance.fr) — sub-hourly, full history
    2. Hub'Eau obs_elab HIXnJ — daily max
    3. Hub'Eau obs_elab HIXM — monthly max (last resort)

    Stations are processed concurrently (bounded by ``HUBEAU_CONCURRENCY``).

    Args:
        start: Start datetime
        end: End datetime
        station_ids: Optional list of station IDs to fetch (default: all configured)
    """
    from app.services.collectors.hubeau import fetch_historical_data, fetch_hydroportail_data

    semaphore = asyncio.Semaphore(HUBEAU_CONCURRENCY)

    async def _one(station: dict) -> int:
        station_id = station["id"]
        async with semaphore:
            # 1. Try Hydroportail (sub-hourly historical data)
            levels = await fetch_hydroportail_data(station_id, start, end)

            # 2. Fallback: Hub'Eau obs_elab daily max (HIXnJ)
            if not levels:
                levels = await fetch_historical_data(station_id, start, end, granularity="daily_max")

            # 3. Last resort: monthly max (HIXM)
            if not levels:
                levels = await fetch_historical_data(station_id, start, end, granularity="monthly_max")
                if levels:
                    logger.info("  obs_elab: using monthly max (HIXM) for %s — %d points",
                                station["name"], len(levels))

        if not levels:
            logger.info("  No data for %s (period may be too recent)", station["name"])
            return 0

        written = await influx_client.write_river_levels_batch(_level_dicts(station, levels))
        logger.info("  Stored %d points for %s", written, station["name"])
        return written

    results = await asyncio.gather(
        *(_one(s) for s in _selected_stations(station_ids)), return_exceptions=True
    )
    total_stored = 0
    for r in results:
        if isinstance(r, Exception):
            logger.warning("Historical river fetch failed: %s", r)
        else:
            total_stored += r
    return total_stored


async def store_tide_data(tides: list[TideData]) -> int:
    """Store tide data in InfluxDB."""
    stored = 0
    for tide in tides:
        success = await influx_client.write_tide(
            port=tide.port,
            height_m=tide.height_m,
            timestamp=tide.timestamp,
            tide_type=tide.tide_type.value if tide.tide_type else None,
            coefficient=tide.coefficient,
        )
        if success:
            stored += 1
    return stored


async def store_river_levels(levels: list[RiverLevel]) -> int:
    """Store river level data in InfluxDB."""
    level_dicts = [
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
    return await influx_client.write_river_levels_batch(level_dicts)


async def store_rainfall(observations: list[RainfallObservation]) -> int:
    """Store rainfall observations in InfluxDB."""
    stored = 0
    for obs in observations:
        success = await influx_client.write_rainfall(
            source=obs.source,
            precipitation_mm_h=obs.precipitation_mm_h,
            timestamp=obs.timestamp,
            precipitation_mm_24h=obs.precipitation_mm_24h,
        )
        if success:
            stored += 1
    return stored


async def get_historical_tides(start: datetime, end: datetime) -> list[TideData]:
    """Get historical tide data from InfluxDB."""
    data = await influx_client.query_tides("LIBOURNE", start, end)

    if not data:
        # Fetch from API and store
        from app.services.collectors.tide import get_tide_forecast
        forecast = await get_tide_forecast()
        if forecast and forecast.tides:
            await store_tide_data(forecast.tides)
            data = await influx_client.query_tides("LIBOURNE", start, end)

    return [
        TideData(
            timestamp=d["timestamp"],
            port=d.get("port", "LIBOURNE"),
            tide_type=d.get("tide_type"),
            height_m=d.get("height_m", 0),
            coefficient=d.get("coefficient"),
        )
        for d in data
        if d.get("height_m") is not None
    ]


async def get_historical_river_levels(
    station_id: str,
    start: datetime,
    end: datetime,
) -> list[RiverLevel]:
    """Get historical river level data from InfluxDB."""
    data = await influx_client.query_river_levels(station_id, start, end)

    if not data:
        # Fetch from Hub'Eau and store
        await _fetch_and_store_river_levels(start, end)
        data = await influx_client.query_river_levels(station_id, start, end)

    return [
        RiverLevel(
            timestamp=d["timestamp"],
            station_id=d.get("station_id", station_id),
            station_name=d.get("station_name", "Unknown"),
            river=d.get("river", "Unknown"),
            height_m=d["height_m"],
            flow_m3s=None,
        )
        for d in data
    ]


async def get_historical_rainfall(start: datetime, end: datetime) -> list[RainfallObservation]:
    """Get historical rainfall data from InfluxDB."""
    data = await influx_client.query_rainfall(start, end)

    if not data:
        # Fetch from Netatmo and store
        from app.services.collectors.netatmo import get_rainfall_observations
        observations = await get_rainfall_observations()
        if observations:
            await store_rainfall(observations)
            data = await influx_client.query_rainfall(start, end)

    return [
        RainfallObservation(
            timestamp=d["timestamp"],
            source=d.get("source", "unknown"),
            precipitation_mm_h=d.get("precipitation_mm_h", 0),
            precipitation_mm_24h=d.get("precipitation_mm_24h"),
            basin_avg=False,
        )
        for d in data
    ]


async def get_multi_station_timeline(
    start: datetime,
    end: datetime,
    station_ids: Optional[list[str]] = None,
) -> dict[str, list[dict]]:
    """
    Get timeline data for multiple stations from InfluxDB.
    If data is missing or incomplete, fetch from Hub'Eau and store.
    """
    from app.config import get_settings
    settings = get_settings()

    # Naive UTC datetimes (aware values are converted, not just stripped)
    start = influx_client.to_naive_utc(start)
    end = influx_client.to_naive_utc(end)

    # Try InfluxDB first
    all_data = await influx_client.query_all_stations_levels(start, end)

    # Check if we need to fetch more data
    need_fetch = False
    if not all_data:
        need_fetch = True
        logger.info("No data in InfluxDB, fetching from Hub'Eau...")
    else:
        # Check coverage - do we have data for the full period?
        for station_id, levels in all_data.items():
            if station_ids and station_id not in station_ids:
                continue
            if levels:
                first_ts = levels[0]["timestamp"]
                if isinstance(first_ts, datetime):
                    first_ts = influx_client.to_naive_utc(first_ts)
                # If first data point is more than 1 hour after start, we have a gap
                if first_ts > start + timedelta(hours=1):
                    need_fetch = True
                    logger.info("Gap detected for %s: data starts at %s, requested %s",
                                station_id, first_ts, start)
                    break

    if need_fetch:
        await _fetch_and_store_river_levels(start, end, station_ids)
        all_data = await influx_client.query_all_stations_levels(start, end)

    # Filter to requested stations
    if station_ids:
        all_data = {k: v for k, v in all_data.items() if k in station_ids}

    # Add station metadata
    station_configs = {s["id"]: s for s in settings.hubeau_stations}
    result = {}

    for station_id, levels in all_data.items():
        config = station_configs.get(station_id, {})
        result[station_id] = {
            "name": config.get("name", "Unknown"),
            "river": config.get("river", "Unknown"),
            "distance_km": config.get("distance_km", 0),
            "thresholds": config.get("thresholds", {}),
            "data": [
                {
                    "timestamp": lv["timestamp"].isoformat() if hasattr(lv["timestamp"], "isoformat") else str(lv["timestamp"]),
                    "height_m": lv["height_m"],
                }
                for lv in levels
            ],
        }

    return result
