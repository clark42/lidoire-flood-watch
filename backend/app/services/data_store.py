"""Data storage service with cache-through pattern.

All data is read from InfluxDB. If not present, it's fetched from
external APIs and stored for future use.

Episodes are persisted in PostgreSQL via SQLAlchemy async.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, delete as sa_delete, inspect as sa_inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection

from app.models.schemas import (
    EpisodeOutcome,
    FloodEpisode,
    FloodEpisodeCreate,
    TideData,
    TideType,
)
from app.models.orm import FloodEpisodeRow
from app.db import get_session_factory
from app.services import influx_client
from app.services.labels import LEGACY_PREFIX_SOURCE, parse_legacy_outcome

_episode_logger = logging.getLogger(__name__ + ".episodes")


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

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
            created_at=_utcnow(),
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

    now = _utcnow()
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


async def _query_tide_events(start: datetime, end: datetime) -> list[TideData]:
    """PM/BM events stored for [start, end] (same port rule as the risk backfill).

    PM/BM-tagged events are used when present, else high tides detected as
    local maxima of the hourly heights (coefficient from the nearest stored
    coefficient record, if any).
    """
    from app.config import get_settings
    from app.services.collectors.tide import tide_events_from_rows
    from app.services.risk_v2 import load_tide_rows, tide_inputs_from_records

    rows = await load_tide_rows(start, end)
    if not rows:
        return []
    port = next((r.get("port") for r in rows if r.get("port")), None) or get_settings().tide_port_confluence
    events = tide_events_from_rows(rows, port)
    if events:
        return events

    heights = {
        influx_client.to_naive_utc(r["timestamp"]): float(r["height_m"])
        for r in rows if r.get("height_m") is not None and r.get("timestamp") is not None
    }
    highs, _ = tide_inputs_from_records(rows)
    return [
        TideData(
            timestamp=ts,
            port=port,
            tide_type=TideType.HIGH,
            height_m=heights.get(ts, 0.0),
            coefficient=coef,
            source="influxdb",
        )
        for ts, coef in highs
    ]


async def get_historical_tides(start: datetime, end: datetime) -> list[TideData]:
    """Get historical tide events (PM/BM) from InfluxDB.

    Every collector stores tides at the Lidoire confluence (source times +
    phase lag, ``settings.tide_port_confluence``). When nothing is stored for
    the period, the hourly sea level of the period (plus 1 day of margin)
    is fetched from Open-Meteo Marine, stored, and read back.
    """
    start = influx_client.to_naive_utc(start)
    end = influx_client.to_naive_utc(end)
    events = await _query_tide_events(start, end)

    if not events:
        from app.services.collectors.tide import fetch_and_store_tides_range

        stored = await fetch_and_store_tides_range(start - timedelta(days=1), end + timedelta(days=1))
        if stored:
            events = await _query_tide_events(start, end)

    return events
