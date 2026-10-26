"""Timeline data endpoints.

Cache-through design (``/unified``):
- Requests are bounded (``MAX_RANGE_DAYS``) and every station / rainfall id is
  validated against config before reaching InfluxDB (422 otherwise).
- ``_ensure_*`` helpers check data density with a Flux ``count()`` (no raw
  points loaded). When data is sparse they *schedule* the backfill as a
  background task instead of blocking the GET; the request waits at most
  ``BACKFILL_INLINE_WAIT_S`` for it and lists still-running backfills in the
  ``X-Backfill-Pending`` response header.
- Results are memoised in-process: "enough data" for ``ENSURE_OK_TTL_S`` and
  "backfill attempted" for ``ENSURE_ATTEMPT_TTL_S`` (1h), so an archive gap
  that external APIs cannot fill no longer re-triggers a backfill on every
  request.
"""

import asyncio
import logging
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from fastapi import APIRouter, HTTPException, Query, Response

from app.config import get_settings
from app.models.schemas import (
    AvailableSources,
    RiverStationInfo,
    RainfallStationInfo,
    UnifiedTimelineData,
    TimelineSeries,
    TimelineDataPoint,
)
from app.services.cache import TTLCache
from app.services.influx_client import (
    query_coverage,
    query_rainfall_by_station,
    query_river_levels_multi_station,
    query_tides_timeline,
    query_risk_scores_timeline,
    query_soil_timeline,
    query_wind_timeline,
    to_naive_utc,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


router = APIRouter()

MAX_RANGE_DAYS = 400
DEFAULT_RANGE_HOURS = 48
VALID_RESOLUTIONS = ["15m", "1h", "6h", "12h", "1d"]
COARSE_RESOLUTIONS = ("6h", "12h", "1d")

ENSURE_OK_TTL_S = 600
ENSURE_ATTEMPT_TTL_S = 3600
NETATMO_CURRENT_TTL_S = 600
BACKFILL_INLINE_WAIT_S = 5.0

OPEN_METEO_SOURCE = "open-meteo"
OPEN_METEO_CITY = "Moulin-Lidoire"
OPEN_METEO_KEY = f"{OPEN_METEO_SOURCE}:{OPEN_METEO_CITY}"
NETATMO_SOURCE = "netatmo-lidoire"


# ---------------------------------------------------------------------------
# Station catalogue (config is the source of truth)
# ---------------------------------------------------------------------------

_RIVER_COLORS = {
    "P577001001": "#0891b2",
    "P555001001": "#0ea5e9",
    "P514001001": "#06b6d4",
    "P726151001": "#8b5cf6",
    "P704151001": "#a855f7",
    "P846251001": "#f59e0b",
    "P816251001": "#fbbf24",
}
_NETATMO_COLORS = ["#22c55e", "#16a34a", "#15803d", "#166534", "#14532d", "#052e16"]
_DEFAULT_COLOR = "#6b7280"


def _fold(value: str) -> str:
    """Accent/case-insensitive comparison key ('Saint-Rémy' == 'saint-remy')."""
    stripped = unicodedata.normalize("NFD", value).encode("ascii", "ignore").decode()
    return stripped.replace("’", "'").strip().lower()


def _strip_accents(value: str) -> str:
    return unicodedata.normalize("NFD", value).encode("ascii", "ignore").decode()


def _build_river_stations() -> dict[str, dict]:
    settings = get_settings()
    return {
        s["id"]: {
            "name": s["name"],
            "river": s["river"],
            "color": _RIVER_COLORS.get(s["id"], _DEFAULT_COLOR),
            "distance_km": s.get("distance_km", 0),
            "delay_hours": s.get("delay_hours", 0),
        }
        for s in settings.hubeau_stations
    }


def _build_rainfall_stations() -> dict[str, dict]:
    settings = get_settings()
    stations = {
        OPEN_METEO_KEY: {"name": "Open-Meteo (Moulin)", "position": "milieu", "color": "#3b82f6"},
    }
    for idx, s in enumerate(settings.lidoire_netatmo_priority):
        stations[f"{NETATMO_SOURCE}:{s['city']}"] = {
            "name": s["city"],
            "position": s.get("position", "unknown"),
            "color": _NETATMO_COLORS[idx % len(_NETATMO_COLORS)],
        }
    return stations


RIVER_STATIONS = _build_river_stations()
RAINFALL_STATIONS = _build_rainfall_stations()

# Ids used by older frontends -> canonical config key.
_LEGACY_RAINFALL_ALIASES = {
    "netatmo-lidoire:Saint-Antoine-sur-l'Isle": "netatmo-lidoire:Saint-Antoine-de-Breuilh",
}


def _rainfall_lookup() -> dict[str, str]:
    lookup = {_fold(key): key for key in RAINFALL_STATIONS}
    for alias, canonical in _LEGACY_RAINFALL_ALIASES.items():
        if canonical in RAINFALL_STATIONS:
            lookup[_fold(alias)] = canonical
    return lookup


def normalize_rainfall_sources(raw: list[str]) -> list[str]:
    """Map requested 'source:city' ids to canonical config keys, 422 on unknown."""
    lookup = _rainfall_lookup()
    canonical: list[str] = []
    unknown: list[str] = []
    for item in raw:
        key = lookup.get(_fold(item))
        if key is None:
            unknown.append(item)
        elif key not in canonical:
            canonical.append(key)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unknown rainfall source(s)",
                "unknown": unknown,
                "valid": list(RAINFALL_STATIONS),
            },
        )
    return canonical


def validate_river_stations(raw: list[str]) -> list[str]:
    """Keep configured Hub'Eau station ids, 422 on unknown ones."""
    unknown = [s for s in raw if s not in RIVER_STATIONS]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unknown river station(s)",
                "unknown": unknown,
                "valid": list(RIVER_STATIONS),
            },
        )
    return list(dict.fromkeys(raw))


def resolve_time_range(
    start: Optional[datetime],
    end: Optional[datetime],
    now: Optional[datetime] = None,
) -> tuple[datetime, datetime]:
    """Default, normalise (naive UTC) and bound the requested period."""
    now = now or _utcnow()
    end = to_naive_utc(end) if end is not None else now
    start = to_naive_utc(start) if start is not None else end - timedelta(hours=DEFAULT_RANGE_HOURS)

    if start >= end:
        raise HTTPException(status_code=422, detail="start must be before end")
    if end - start > timedelta(days=MAX_RANGE_DAYS):
        raise HTTPException(
            status_code=422,
            detail=f"requested period exceeds {MAX_RANGE_DAYS} days",
        )
    return start, end


def _netatmo_tag_variants(canonical_city: str) -> list[str]:
    """City tag values a canonical city may be stored under.

    Live collection and the historical backfill now both write the
    canonical config city (``netatmo.canonical_city``, accents kept). Older
    backfilled points may carry an accent-stripped spelling ('Saint-Remy'),
    so both are read. (Older live points tagged with a raw Netatmo API name
    that differs from the config are only merged by the collector's own
    readers, see ``netatmo._legacy_city_tags``.)
    """
    return list(dict.fromkeys([canonical_city, _strip_accents(canonical_city)]))


# ---------------------------------------------------------------------------
# Cache-through machinery
# ---------------------------------------------------------------------------

_ensure_cache = TTLCache(ttl_seconds=ENSURE_ATTEMPT_TTL_S, max_entries=1024)
_inflight: dict[tuple, asyncio.Task] = {}
_background: set[asyncio.Task] = set()
_TASK_PREFIX = "timeline-backfill:"


def _task_label(task: asyncio.Task) -> str:
    name = task.get_name()
    return name[len(_TASK_PREFIX):] if name.startswith(_TASK_PREFIX) else "backfill"


def _hour_floor(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _cache_key(kind: str, start: datetime, end: datetime, *extra) -> tuple:
    """Hour-granular key so 'now'-relative requests share one entry per hour."""
    return (kind, _hour_floor(start), _hour_floor(end), tuple(extra))


def _expected_points(start: datetime, end: datetime, hours_per_point: float, minimum: int) -> int:
    period_hours = (end - start).total_seconds() / 3600
    return max(minimum, int(period_hours / hours_per_point))


def _cached_state(key: tuple) -> Optional[asyncio.Task | str]:
    """Return the in-flight task, a cached state string, or None (not cached)."""
    task = _inflight.get(key)
    if task is not None and not task.done():
        return task
    return _ensure_cache.get(key)


def _schedule_backfill(
    key: tuple,
    label: str,
    job: Callable[[], Awaitable],
    after: tuple[asyncio.Task, ...] = (),
    attempt_ttl: float = ENSURE_ATTEMPT_TTL_S,
) -> asyncio.Task:
    """Run ``job`` in the background (once per key), after ``after`` tasks."""
    existing = _inflight.get(key)
    if existing is not None and not existing.done():
        return existing

    async def _runner():
        try:
            if after:
                await asyncio.gather(*after, return_exceptions=True)
            result = await job()
            logger.info("Backfill %s done: %s", label, result)
            return result
        except Exception as e:
            logger.warning("Backfill %s failed: %s", label, e)
            return None
        finally:
            _inflight.pop(key, None)

    task = asyncio.create_task(_runner(), name=f"{_TASK_PREFIX}{label}")
    _inflight[key] = task
    # Negative cache: whatever the outcome, don't re-trigger for attempt_ttl.
    _ensure_cache.set(key, "attempted", attempt_ttl)
    return task


def _result(state) -> Optional[asyncio.Task]:
    return state if isinstance(state, asyncio.Task) else None


async def _ensure_river_data(start: datetime, end: datetime, station_ids: list[str]) -> Optional[asyncio.Task]:
    """Check if InfluxDB has river data for the period; if not, fetch from Hub'Eau.

    Uses realtime observations (TR) for recent data (< 30 days old).
    Falls back to Hydroportail / elaborated data (obs_elab) for older periods.
    Returns the scheduled backfill task, if any.
    """
    s, e = to_naive_utc(start), to_naive_utc(end)
    key = _cache_key("river", s, e, *sorted(station_ids))
    state = _cached_state(key)
    if state is not None:
        return _result(state)

    coverage = await query_coverage(
        "river_level", s, e, field="height_m",
        filters={"station_id": station_ids}, group_tag="station_id",
    )
    if coverage is None:
        return None  # InfluxDB error: neither backfill nor cache

    now = _utcnow()
    thirty_days_ago = now - timedelta(days=30)
    # TR (5-15 min) and Hydroportail (sub-hourly) both give >= 1 point / 2h.
    expected = _expected_points(s, e, 2, 4)

    need_fetch = False
    for sid in station_ids:
        info = coverage.get(sid, {"count": 0, "first": None})
        if info["count"] < expected:
            logger.info("Insufficient river data for %s: %d points, need ~%d", sid, info["count"], expected)
            need_fetch = True
            break
        # Coverage gap: data starts much later than requested start
        if s < thirty_days_ago and info["first"] and info["first"] > s + timedelta(days=2):
            logger.info("Coverage gap for %s: data starts at %s, requested from %s", sid, info["first"], s)
            need_fetch = True
            break

    if not need_fetch:
        _ensure_cache.set(key, "ok", ENSURE_OK_TTL_S)
        return None

    async def _job():
        from app.services.data_store import (
            _fetch_and_store_river_levels,
            _fetch_and_store_river_levels_elab,
        )

        stored = 0
        if s < thirty_days_ago:
            elab_end = min(e, thirty_days_ago)
            stored += await _fetch_and_store_river_levels_elab(s, elab_end, station_ids)
            if e > thirty_days_ago:
                stored += await _fetch_and_store_river_levels(thirty_days_ago, e, station_ids)
        else:
            stored += await _fetch_and_store_river_levels(s, e, station_ids)
        return stored

    return _schedule_backfill(key, "river", _job)


async def _ensure_tide_data(start: datetime, end: datetime) -> Optional[asyncio.Task]:
    """Check if InfluxDB has tide data for the period; if not, fetch from APIs."""
    settings = get_settings()
    s, e = to_naive_utc(start), to_naive_utc(end)
    key = _cache_key("tide", s, e)
    state = _cached_state(key)
    if state is not None:
        return _result(state)

    ports = [settings.tide_port_libourne, settings.tide_port_bordeaux]
    coverage = await query_coverage(
        "tide", s, e, field="height_m", filters={"port": ports}, group_tag="port",
    )
    if coverage is None:
        return None

    # LIBOURNE first, BORDEAUX otherwise (Open-Meteo writes to BORDEAUX)
    count = coverage.get(settings.tide_port_libourne, {}).get("count", 0)
    if not count:
        count = coverage.get(settings.tide_port_bordeaux, {}).get("count", 0)
    expected = _expected_points(s, e, 2, 4)

    if count >= expected:
        _ensure_cache.set(key, "ok", ENSURE_OK_TTL_S)
        return None

    logger.info("Insufficient tide data (%d points, need ~%d) for %s -> %s, backfilling", count, expected, s, e)

    async def _job():
        from app.services.collectors.tide import fetch_and_store_tides_range
        return await fetch_and_store_tides_range(s, e)

    return _schedule_backfill(key, "tide", _job)


async def _ensure_open_meteo_rainfall(start: datetime, end: datetime) -> Optional[asyncio.Task]:
    """Ensure Open-Meteo historical rainfall data exists in InfluxDB."""
    s, e = to_naive_utc(start), to_naive_utc(end)
    key = _cache_key("open-meteo", s, e)
    state = _cached_state(key)
    if state is not None:
        return _result(state)

    coverage = await query_coverage(
        "rainfall", s, e, field="precipitation_mm_h",
        filters={"source": [OPEN_METEO_SOURCE], "city": [OPEN_METEO_CITY]},
        window="1h",
    )
    if coverage is None:
        return None

    hours_with_data = coverage.get("_all", {}).get("count", 0)
    expected = _expected_points(s, e, 3, 4)
    if hours_with_data >= expected:
        _ensure_cache.set(key, "ok", ENSURE_OK_TTL_S)
        return None

    logger.info("Insufficient Open-Meteo rainfall (%d hours, need ~%d) for %s -> %s, backfilling",
                hours_with_data, expected, s, e)

    async def _job():
        from app.services.collectors.weather import fetch_and_store_historical_rainfall
        return await fetch_and_store_historical_rainfall(s, e)

    return _schedule_backfill(key, "open-meteo", _job)


async def _ensure_netatmo_rainfall(start: datetime, end: datetime, cities: list[str]) -> Optional[asyncio.Task]:
    """Cache-through for Netatmo rainfall data.

    Pattern: count hours with data in InfluxDB -> if sparse, backfill via the
    getmeasure API in background. Also triggers a current collection if the
    period includes recent time and nothing arrived in the last 30 minutes.
    ``cities`` are canonical config city names.
    """
    s, e = to_naive_utc(start), to_naive_utc(end)
    now = _utcnow()
    tasks: list[asyncio.Task] = []

    # Current collection (independent short-lived negative cache)
    if e >= now - timedelta(hours=1):
        current_key = ("netatmo-current",)
        state = _cached_state(current_key)
        if state is None:
            variants = [v for c in cities for v in _netatmo_tag_variants(c)]
            recent = await query_coverage(
                "rainfall", now - timedelta(minutes=30), now, field="precipitation_mm_h",
                filters={"source": [NETATMO_SOURCE], "city": variants},
            )
            if recent is not None:
                if recent.get("_all", {}).get("count", 0) > 0:
                    _ensure_cache.set(current_key, "ok", NETATMO_CURRENT_TTL_S)
                else:
                    async def _collect():
                        from app.services.scheduler import collect_netatmo_job
                        return await collect_netatmo_job()

                    tasks.append(_schedule_backfill(
                        current_key, "netatmo-current", _collect, attempt_ttl=NETATMO_CURRENT_TTL_S,
                    ))
        elif isinstance(state, asyncio.Task):
            tasks.append(state)

    key = _cache_key("netatmo", s, e, *sorted(cities))
    state = _cached_state(key)
    if state is not None:
        if isinstance(state, asyncio.Task):
            tasks.append(state)
        return _combine(tasks)

    variant_to_city = {_fold(v): c for c in cities for v in _netatmo_tag_variants(c)}
    coverage = await query_coverage(
        "rainfall", s, e, field="precipitation_mm_h",
        filters={"source": [NETATMO_SOURCE], "city": [v for c in cities for v in _netatmo_tag_variants(c)]},
        group_tag="city", window="1h",
    )
    if coverage is None:
        return _combine(tasks)

    hours_by_city: dict[str, int] = {c: 0 for c in cities}
    for tag_value, info in coverage.items():
        city = variant_to_city.get(_fold(tag_value))
        if city is not None:
            # Variants overlap in time: max, not sum
            hours_by_city[city] = max(hours_by_city[city], info["count"])

    expected = _expected_points(s, e, 3, 4)
    cities_needing_backfill = [c for c, n in hours_by_city.items() if n < expected * 0.5]

    if not cities_needing_backfill:
        _ensure_cache.set(key, "ok", ENSURE_OK_TTL_S)
        return _combine(tasks)

    logger.info("Netatmo: %s sparse (need ~%d hours) for %s -> %s, backfilling via getmeasure",
                cities_needing_backfill, expected, s, e)

    async def _job():
        from app.services.collectors.netatmo import fetch_and_store_netatmo_historical_range
        return await fetch_and_store_netatmo_historical_range(s, e, cities_needing_backfill)

    tasks.append(_schedule_backfill(key, "netatmo", _job))
    return _combine(tasks)


def _combine(tasks: list[asyncio.Task]) -> Optional[asyncio.Task]:
    """Return a single awaitable task for several backfills (or None)."""
    tasks = [t for t in tasks if t is not None]
    if not tasks:
        return None
    if len(tasks) == 1:
        return tasks[0]

    async def _all():
        return await asyncio.gather(*tasks, return_exceptions=True)

    combined = asyncio.create_task(_all(), name=_TASK_PREFIX + "+".join(_task_label(t) for t in tasks))
    # Keep a strong reference until done (the request may stop waiting first).
    _background.add(combined)
    combined.add_done_callback(_background.discard)
    return combined


async def _ensure_risk_data(
    start: datetime,
    end: datetime,
    after: tuple[asyncio.Task, ...] = (),
) -> Optional[asyncio.Task]:
    """Cache-through for risk scores.

    If risk score data is sparse for the period, backfill by recalculating
    from historical river, rainfall, and tide data already in InfluxDB.

    The backfill waits for the input backfills passed in ``after`` so it
    computes on complete inputs.
    """
    s, e = to_naive_utc(start), to_naive_utc(end)
    now = _utcnow()

    # Don't backfill future periods
    if s > now:
        return None
    backfill_end = min(e, now)

    key = _cache_key("risk", s, backfill_end)
    state = _cached_state(key)
    if state is not None:
        return _result(state)

    coverage = await query_coverage(
        "risk_score", s, backfill_end, field="score", window="1h",
    )
    if coverage is None:
        return None
    hours_with_data = coverage.get("_all", {}).get("count", 0)

    period_hours = (backfill_end - s).total_seconds() / 3600
    # Adaptive expected density
    if period_hours > 30 * 24:
        expected = max(4, int(period_hours / 12))  # ~2 per day for long periods
    elif period_hours > 7 * 24:
        expected = max(4, int(period_hours / 6))   # ~4 per day
    else:
        expected = max(4, int(period_hours / 2))   # ~12 per day

    if hours_with_data >= expected * 0.5:
        _ensure_cache.set(key, "ok", ENSURE_OK_TTL_S)
        return None

    logger.info("Insufficient risk data (%d hours, need ~%d) for %s -> %s, backfilling from historical inputs",
                hours_with_data, expected, s, backfill_end)

    async def _job():
        from app.services.risk_v2 import backfill_risk_scores
        # Anonymous GET: only fill hours without a realtime score, and tag the
        # points source=backfill (readers prefer realtime for a shared hour),
        # so the two computations never alternate within the same hour.
        return await backfill_risk_scores(s, backfill_end, skip_live_hours=True)

    return _schedule_backfill(key, "risk", _job, after=after)


async def _ensure_infoclimat_rainfall(start: datetime, end: datetime) -> Optional[asyncio.Task]:
    """Cache-through for Infoclimat historical rainfall data.

    Uses the Infoclimat OpenData API (chunked 7-day requests).
    Fails gracefully if API key is missing or IP not authorized (and the
    negative cache prevents hammering it on every request).
    """
    s, e = to_naive_utc(start), to_naive_utc(end)
    key = _cache_key("infoclimat", s, e)
    state = _cached_state(key)
    if state is not None:
        return _result(state)

    coverage = await query_coverage(
        "rainfall", s, e, field="precipitation_mm_h",
        filters={"source": ["infoclimat"]}, group_tag="city", window="1h",
    )
    if coverage is None:
        return None

    total_hours = sum(info["count"] for info in coverage.values())
    expected = _expected_points(s, e, 3, 4)
    if total_hours >= expected * 0.5:
        _ensure_cache.set(key, "ok", ENSURE_OK_TTL_S)
        return None

    logger.info("Insufficient Infoclimat data (%d hours, need ~%d) for %s -> %s, backfilling",
                total_hours, expected, s, e)

    async def _job():
        from app.services.collectors.infoclimat import fetch_and_store_infoclimat_historical_range
        return await fetch_and_store_infoclimat_historical_range(s, e)

    return _schedule_backfill(key, "infoclimat", _job)


async def _ensure_soil_data(start: datetime, end: datetime) -> Optional[asyncio.Task]:
    """Check if InfluxDB has soil timeline data for the period; if not, backfill from Open-Meteo."""
    s, e = to_naive_utc(start), to_naive_utc(end)
    key = _cache_key("soil", s, e)
    state = _cached_state(key)
    if state is not None:
        return _result(state)

    coverage = await query_coverage(
        "soil_timeline", s, e, field="moisture_shallow",
        filters={"location": ["moulin"]}, window="6h",
    )
    if coverage is None:
        return None

    windows = coverage.get("_all", {}).get("count", 0)
    # Expect ~1 point per 6h window, tolerate half
    expected = _expected_points(s, e, 12, 2)
    if windows >= expected:
        _ensure_cache.set(key, "ok", ENSURE_OK_TTL_S)
        return None

    logger.info("Insufficient soil data (%d windows, need ~%d) for %s -> %s, backfilling from Open-Meteo",
                windows, expected, s, e)

    async def _job():
        from app.services.collectors.weather import fetch_and_store_soil_historical
        return await fetch_and_store_soil_historical(s, e)

    return _schedule_backfill(key, "soil", _job)


async def _ensure_wind_data(start: datetime, end: datetime) -> Optional[asyncio.Task]:
    """Check if InfluxDB has wind data for the period; if not, backfill from Open-Meteo."""
    s, e = to_naive_utc(start), to_naive_utc(end)
    key = _cache_key("wind", s, e)
    state = _cached_state(key)
    if state is not None:
        return _result(state)

    coverage = await query_coverage(
        "wind", s, e, field="speed_kmh", filters={"location": ["moulin"]}, window="6h",
    )
    if coverage is None:
        return None

    windows = coverage.get("_all", {}).get("count", 0)
    expected = _expected_points(s, e, 12, 2)
    if windows >= expected:
        _ensure_cache.set(key, "ok", ENSURE_OK_TTL_S)
        return None

    logger.info("Insufficient wind data (%d windows, need ~%d) for %s -> %s, backfilling from Open-Meteo",
                windows, expected, s, e)

    async def _job():
        from app.services.collectors.weather import fetch_and_store_wind_historical
        return await fetch_and_store_wind_historical(s, e)

    return _schedule_backfill(key, "wind", _job)


async def _wait_for_backfills(tasks: list[asyncio.Task], timeout: float) -> list[str]:
    """Wait up to ``timeout`` for backfills; return labels still running."""
    tasks = [t for t in tasks if t is not None]
    if not tasks:
        return []
    _done, pending = await asyncio.wait(tasks, timeout=timeout)
    labels: set[str] = set()
    for t in pending:
        labels.update(_task_label(t).split("+"))
    return sorted(labels)


# ---------------------------------------------------------------------------
# Series helpers
# ---------------------------------------------------------------------------


def _points(raw: list[dict]) -> list[TimelineDataPoint]:
    return [
        TimelineDataPoint(timestamp=p["timestamp"], value=p["value"])
        for p in raw
        if p.get("value") is not None
    ]


def _merge_points(series_list: list[list[dict]]) -> list[dict]:
    """Merge several point lists, averaging values that share a timestamp."""
    by_ts: dict[datetime, list[float]] = {}
    for points in series_list:
        for p in points:
            if p.get("value") is None:
                continue
            by_ts.setdefault(p["timestamp"], []).append(p["value"])
    return [
        {"timestamp": ts, "value": sum(vals) / len(vals)}
        for ts, vals in sorted(by_ts.items())
    ]


def _canonicalize_rainfall(raw: dict[str, list[dict]], wanted: list[str]) -> dict[str, list[dict]]:
    """Map raw 'source:tag' series to canonical keys (accent-insensitive)."""
    lookup = {_fold(k): k for k in wanted}
    grouped: dict[str, list[list[dict]]] = {}
    for raw_key, points in raw.items():
        canonical = lookup.get(_fold(raw_key))
        if canonical is not None:
            grouped.setdefault(canonical, []).append(points)
    return {
        key: (lists[0] if len(lists) == 1 else _merge_points(lists))
        for key, lists in grouped.items()
    }


def compute_cumulative_rainfall(hourly_by_station: dict[str, list[dict]]) -> list[dict]:
    """Running rainfall total for the selected stations (reference policy).

    Input: hourly totals (mm in each hour) per 'source:city' key.

    Policy, per hour:
      1. observed gauges first: arithmetic mean of the hourly totals of the
         stations (any source except Open-Meteo) that reported that hour —
         an areal average that neither double-counts co-located gauges nor
         biases upward like the previous max-across-sources;
      2. if no gauge reported that hour, the Open-Meteo model value fills the
         gap.
    Hours with no value at all add nothing. Values are rounded to 0.1 mm.
    """
    gauges: dict[datetime, list[float]] = {}
    model: dict[datetime, float] = {}
    for key, points in hourly_by_station.items():
        is_model = key.startswith(f"{OPEN_METEO_SOURCE}:")
        for p in points:
            value = p.get("value")
            if value is None:
                continue
            ts = p["timestamp"]
            if is_model:
                model[ts] = max(value, 0.0)
            else:
                gauges.setdefault(ts, []).append(max(value, 0.0))

    running = 0.0
    cumul: list[dict] = []
    for ts in sorted(set(gauges) | set(model)):
        if ts in gauges:
            running += sum(gauges[ts]) / len(gauges[ts])
        else:
            running += model[ts]
        cumul.append({"timestamp": ts, "value": round(running, 1)})
    return cumul


def _split_rainfall_request(source_keys: list[str]) -> tuple[list[str], list[str]]:
    """Return (sources, city tag values) to query for canonical keys."""
    sources: list[str] = []
    cities: list[str] = []
    for key in source_keys:
        source, city = key.split(":", 1)
        if source not in sources:
            sources.append(source)
        variants = _netatmo_tag_variants(city) if source == NETATMO_SOURCE else [city]
        for v in variants:
            if v not in cities:
                cities.append(v)
    return sources, cities


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/sources", response_model=AvailableSources)
async def get_available_sources():
    """
    Get available data sources for timeline comparison.

    Returns information about all river stations (Hub'Eau) and
    rainfall stations (Netatmo Lidoire) that can be selected. Ids come from
    config (canonical names, e.g. "netatmo-lidoire:Saint-Rémy").
    """
    river_stations = [
        RiverStationInfo(
            id=station_id,
            name=info["name"],
            river=info["river"],
            color=info["color"],
            distance_km=info["distance_km"],
            delay_hours=info["delay_hours"],
        )
        for station_id, info in RIVER_STATIONS.items()
    ]

    rainfall_stations = [
        RainfallStationInfo(
            id=source_id,
            name=info["name"],
            position=info["position"],
            color=info["color"],
        )
        for source_id, info in RAINFALL_STATIONS.items()
    ]

    return AvailableSources(
        river_stations=river_stations,
        rainfall_stations=rainfall_stations,
        has_tide=True,
        has_risk_score=True,
        has_infoclimat=True,
        has_wind=True,
        has_soil=True,
    )


@router.get("/unified", response_model=UnifiedTimelineData)
async def get_unified_timeline(
    response: Response,
    start: Optional[datetime] = Query(None, description="Start datetime (default: 48h before end)"),
    end: Optional[datetime] = Query(None, description="End datetime (default: now)"),
    river_stations: Optional[str] = Query(None, description="Comma-separated Hub'Eau station IDs"),
    rainfall_sources: Optional[str] = Query(None, description="Comma-separated rainfall sources (format: source:city)"),
    include_tide: bool = Query(False, description="Include tide data (Libourne)"),
    include_risk: bool = Query(False, description="Include risk score data"),
    include_soil: bool = Query(False, description="Include soil conditions data (moisture, temperature, ET0)"),
    include_infoclimat: bool = Query(True, description="Include Infoclimat rainfall data"),
    include_wind: bool = Query(False, description="Include wind speed/direction data"),
    include_forecast: bool = Query(False, description="Include 7-day risk forecast overlay"),
    resolution: str = Query("1h", description="Data resolution: 15m, 1h, 6h, 12h, 1d"),
):
    """
    Get unified timeline data combining river levels, rainfall, tides, and risk scores.

    This endpoint allows flexible comparison of any combination of:
    - River level stations (Hub'Eau)
    - Rainfall stations (Netatmo Lidoire, Open-Meteo)
    - Tide data (Libourne port)
    - Risk score history

    Constraints: start < end, period <= 400 days, ids must come from
    ``/timeline/sources`` (422 otherwise). Missing data is backfilled in
    background; still-running backfills are listed in the
    ``X-Backfill-Pending`` response header (retry later to get them).

    Rainfall units: "mm/h" (hourly rate) at 15m/1h, "mm" per period at
    6h/12h/1d (sum of hourly means). ``rainfall_cumul`` is the running total
    defined by ``compute_cumulative_rainfall``.

    Example:
    - river_stations=P577001001,P555001001
    - rainfall_sources=netatmo-lidoire:Saint-Rémy,netatmo-lidoire:Flaujagues
    - include_tide=true
    - include_risk=true
    """
    start, end = resolve_time_range(start, end)

    if resolution not in VALID_RESOLUTIONS:
        resolution = "1h"

    station_ids = (
        validate_river_stations([s.strip() for s in river_stations.split(",") if s.strip()])
        if river_stations else []
    )
    requested_rain = (
        normalize_rainfall_sources([s.strip() for s in rainfall_sources.split(",") if s.strip()])
        if rainfall_sources else []
    )
    netatmo_cities = [k.split(":", 1)[1] for k in requested_rain if k.startswith(f"{NETATMO_SOURCE}:")]

    use_sum = resolution in COARSE_RESOLUTIONS
    rain_agg_fn = "sum" if use_sum else "mean"
    rain_unit = "mm" if use_sum else "mm/h"

    # --- 1. Cache-through checks (concurrent, backfills run in background) ---
    ensure_jobs: dict[str, Awaitable] = {}
    if station_ids:
        ensure_jobs["river"] = _ensure_river_data(start, end, station_ids)
    if include_tide:
        ensure_jobs["tide"] = _ensure_tide_data(start, end)
    if OPEN_METEO_KEY in requested_rain:
        ensure_jobs["open-meteo"] = _ensure_open_meteo_rainfall(start, end)
    if netatmo_cities:
        ensure_jobs["netatmo"] = _ensure_netatmo_rainfall(start, end, netatmo_cities)
    if include_soil:
        ensure_jobs["soil"] = _ensure_soil_data(start, end)
    if include_infoclimat:
        ensure_jobs["infoclimat"] = _ensure_infoclimat_rainfall(start, end)
    if include_wind:
        ensure_jobs["wind"] = _ensure_wind_data(start, end)

    ensure_results = dict(zip(ensure_jobs, await asyncio.gather(*ensure_jobs.values())))
    backfills: list[asyncio.Task] = [t for t in ensure_results.values() if t is not None]

    if include_risk:
        # Risk backfill recomputes from inputs: run it after input backfills.
        risk_inputs = tuple(
            t for k, t in ensure_results.items()
            if t is not None and k in ("river", "tide", "open-meteo", "netatmo")
        )
        risk_task = await _ensure_risk_data(start, end, after=risk_inputs)
        if risk_task is not None:
            backfills.append(risk_task)

    pending = await _wait_for_backfills(backfills, BACKFILL_INLINE_WAIT_S)
    if pending:
        response.headers["X-Backfill-Pending"] = ",".join(pending)

    # --- 2. Series queries (concurrent) ---
    async def river_series() -> dict[str, TimelineSeries]:
        if not station_ids:
            return {}
        river_data = await query_river_levels_multi_station(
            start=start, end=end, station_ids=station_ids, resolution=resolution,
        )
        out = {}
        for station_id, points in river_data.items():
            info = RIVER_STATIONS.get(station_id, {"name": station_id, "color": _DEFAULT_COLOR})
            out[f"river:{station_id}"] = TimelineSeries(
                type="river_level", name=info["name"], unit="m",
                color=info["color"], data=_points(points),
            )
        return out

    async def rainfall_series() -> dict[str, TimelineSeries]:
        if not requested_rain:
            return {}
        wanted = list(requested_rain)
        sources, cities = _split_rainfall_request(wanted)
        raw = await query_rainfall_by_station(
            start=start, end=end, sources=sources, cities=cities,
            resolution=resolution, agg_fn=rain_agg_fn,
        )
        rainfall_data = _canonicalize_rainfall(raw, wanted)

        # Auto-add Open-Meteo when a requested Netatmo station is sparse
        expected = _expected_points(start, end, 3, 4)
        if netatmo_cities and OPEN_METEO_KEY not in wanted:
            sparse = [
                k for k in wanted
                if k.startswith(f"{NETATMO_SOURCE}:") and len(rainfall_data.get(k, [])) < expected * 0.5
            ]
            if sparse:
                logger.info("Netatmo %s sparse (need ~%d points), adding Open-Meteo fallback", sparse, expected)
                om_backfill = await _ensure_open_meteo_rainfall(start, end)
                if om_backfill is not None:
                    still = await _wait_for_backfills([om_backfill], BACKFILL_INLINE_WAIT_S)
                    if still:
                        existing = response.headers.get("X-Backfill-Pending")
                        labels = sorted(set((existing.split(",") if existing else []) + still))
                        response.headers["X-Backfill-Pending"] = ",".join(labels)
                om_raw = await query_rainfall_by_station(
                    start=start, end=end, sources=[OPEN_METEO_SOURCE], cities=[OPEN_METEO_CITY],
                    resolution=resolution, agg_fn=rain_agg_fn,
                )
                rainfall_data.update(_canonicalize_rainfall(om_raw, [OPEN_METEO_KEY]))
                wanted.append(OPEN_METEO_KEY)

        out: dict[str, TimelineSeries] = {}
        for key in wanted:
            points = rainfall_data.get(key)
            if not points:
                continue
            info = RAINFALL_STATIONS.get(key, {"name": key, "color": _DEFAULT_COLOR})
            out[f"rainfall:{key}"] = TimelineSeries(
                type="rainfall", name=info["name"], unit=rain_unit,
                color=info["color"], data=_points(points),
            )

        # Cumulative series from hourly totals (reuse display data at 1h:
        # same Flux as "sum" at 1h = hourly mean).
        if resolution == "1h":
            hourly = rainfall_data
        else:
            c_sources, c_cities = _split_rainfall_request(wanted)
            hourly = _canonicalize_rainfall(
                await query_rainfall_by_station(
                    start=start, end=end, sources=c_sources, cities=c_cities,
                    resolution="1h", agg_fn="sum",
                ),
                wanted,
            )
        cumul = compute_cumulative_rainfall({k: v for k, v in hourly.items() if k in wanted})
        if cumul:
            out["rainfall_cumul"] = TimelineSeries(
                type="rainfall_cumul", name="Cumul pluie", unit="mm",
                color="#1e40af", data=_points(cumul),
            )
        return out

    async def tide_series() -> dict[str, TimelineSeries]:
        if not include_tide:
            return {}
        settings = get_settings()
        tide_data = await query_tides_timeline(
            start=start, end=end, port=settings.tide_port_libourne, resolution=resolution,
        )
        # Also try BORDEAUX port (Open-Meteo writes with port=BORDEAUX)
        if not tide_data:
            tide_data = await query_tides_timeline(
                start=start, end=end, port=settings.tide_port_bordeaux, resolution=resolution,
            )
        if not tide_data:
            return {}
        return {
            "tide:LIBOURNE": TimelineSeries(
                type="tide", name="Maree Libourne", unit="m",
                color="#3b82f6", data=_points(tide_data),
            )
        }

    async def soil_series() -> dict[str, TimelineSeries]:
        if not include_soil:
            return {}
        soil_data = await query_soil_timeline(start=start, end=end, resolution=resolution)
        soil_series_config = {
            "moisture_shallow": {
                "type": "soil_moisture",
                "name": "Humidite sol (0-7cm)",
                "unit": "m³/m³",
                "color": "#7c3aed",
            },
            "moisture_deep": {
                "type": "soil_moisture",
                "name": "Humidite sol (7-100cm)",
                "unit": "m³/m³",
                "color": "#6366f1",
            },
            "soil_temp": {
                "type": "soil_temperature",
                "name": "Temperature sol",
                "unit": "°C",
                "color": "#f97316",
            },
            "et0_mm": {
                "type": "evapotranspiration",
                "name": "Evapotranspiration (ET0)",
                "unit": "mm/j",
                "color": "#22c55e",
            },
        }
        out = {}
        for field_name, config in soil_series_config.items():
            points = soil_data.get(field_name, [])
            if points:
                out[f"soil:{field_name}"] = TimelineSeries(data=_points(points), **config)
        return out

    async def infoclimat_series() -> dict[str, TimelineSeries]:
        if not include_infoclimat:
            return {}
        infoclimat_data = await query_rainfall_by_station(
            start=start, end=end, sources=["infoclimat"],
            resolution=resolution, agg_fn=rain_agg_fn,
        )
        out = {}
        for source_city, points in infoclimat_data.items():
            if points:
                station_name = source_city.replace("infoclimat:", "")
                out[f"rainfall:{source_city}"] = TimelineSeries(
                    type="rainfall", name=f"Infoclimat ({station_name})", unit=rain_unit,
                    color="#dc2626", data=_points(points),
                )
        return out

    async def wind_series() -> dict[str, TimelineSeries]:
        if not include_wind:
            return {}
        wind_data = await query_wind_timeline(start=start, end=end, resolution=resolution)
        wind_series_config = {
            "speed_kmh": {
                "type": "wind_speed",
                "name": "Vent moyen",
                "unit": "km/h",
                "color": "#64748b",
            },
            "westerly_kmh": {
                "type": "wind_westerly",
                "name": "Composante Ouest (maree)",
                "unit": "km/h",
                "color": "#0ea5e9",
            },
        }
        out = {}
        for field_name, config in wind_series_config.items():
            points = wind_data.get(field_name, [])
            if points:
                out[f"wind:{field_name}"] = TimelineSeries(data=_points(points), **config)
        return out

    async def risk_series() -> dict[str, TimelineSeries]:
        if not include_risk:
            return {}
        risk_data = await query_risk_scores_timeline(start=start, end=end, resolution=resolution)
        if not risk_data:
            return {}
        return {
            "risk:score": TimelineSeries(
                type="risk_score", name="Score de risque", unit="/100",
                color="#f59e0b", data=_points(risk_data),
            )
        }

    async def forecast_series() -> dict[str, TimelineSeries]:
        if not include_forecast:
            return {}
        try:
            from app.services.forecast import get_risk_forecast_v2
            forecast_data = await get_risk_forecast_v2(hours=168)
        except Exception as e_fc:
            logger.warning("Forecast overlay failed: %s", e_fc)
            return {}

        forecast_points = []
        forecast_rain_points = []
        forecast_level_points = []
        for fp in forecast_data.get("forecasts", []):
            ts = fp["timestamp"]
            if fp.get("score") is not None:
                forecast_points.append(TimelineDataPoint(timestamp=ts, value=fp["score"]))
            if fp.get("rain_1h_mm") is not None:
                forecast_rain_points.append(TimelineDataPoint(timestamp=ts, value=fp["rain_1h_mm"]))
            if fp.get("projected_level_m") is not None:
                forecast_level_points.append(TimelineDataPoint(timestamp=ts, value=fp["projected_level_m"]))

        if not forecast_points:
            return {}
        return {
            "forecast:risk_score": TimelineSeries(
                type="forecast_risk_score", name="Prevision risque (7j)", unit="/100",
                color="#8b5cf6", data=forecast_points,
            ),
            "forecast:rain_1h": TimelineSeries(
                type="forecast_rainfall", name="Prevision pluie (1h)", unit="mm/h",
                color="#a78bfa", data=forecast_rain_points,
            ),
            "forecast:projected_level": TimelineSeries(
                type="forecast_level", name="Niveau projete", unit="m",
                color="#c4b5fd", data=forecast_level_points,
            ),
        }

    parts = await asyncio.gather(
        river_series(),
        rainfall_series(),
        tide_series(),
        soil_series(),
        infoclimat_series(),
        wind_series(),
        risk_series(),
        forecast_series(),
    )
    series: dict[str, TimelineSeries] = {}
    for part in parts:
        series.update(part)

    return UnifiedTimelineData(
        start=start,
        end=end,
        resolution=resolution,
        series=series,
    )
