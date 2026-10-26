"""InfluxDB client for time-series data storage.

Handles writing and querying flood monitoring data:
- River levels from Hub'Eau stations
- Tide heights and coefficients
- Rainfall observations from Netatmo/Infoclimat
- Risk scores

Safety / performance notes:
- The influxdb-client is synchronous. Every query/write goes through
  ``_run_query`` / ``_run_write`` which offload the blocking HTTP call to a
  worker thread (``asyncio.to_thread``) so the event loop keeps serving
  requests and ``asyncio.gather`` really runs queries in parallel.
- Bulk writes (backfills, collectors) go through ``write_points_batch``:
  one HTTP write per chunk instead of one per point.
- Queries use ``INFLUXDB_READ_TOKEN`` when set (read-only token), writes use
  ``INFLUXDB_TOKEN``.
- Flux has no bind parameters in this client version, so every value that is
  interpolated in a query is validated (whitelist from config, or a strict
  character set for free-form tag values coming from external APIs) and
  rendered with ``flux_string`` (proper escaping of ``\\``, ``"`` and ``$``).
  Invalid values raise ``InvalidQueryParameter`` (a ``ValueError``).
"""

from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional
import asyncio
import logging
import re

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from app.config import get_settings
from app.services.cache import TTLCache

logger = logging.getLogger(__name__)


def _flux_ts(dt: datetime) -> str:
    """Format datetime as RFC3339 (UTC) for Flux queries.

    Aware datetimes are converted to UTC first (a Paris-time datetime must not
    be shifted by 1-2h); naive datetimes are assumed to already be UTC. The tz
    is then stripped so we never produce an invalid '...+00:00Z'.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat() + "Z"


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_naive_utc(dt: datetime) -> datetime:
    """Return a naive UTC datetime (aware values are converted, not just stripped)."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# ---------------------------------------------------------------------------
# Flux query safety: validation + escaping
# ---------------------------------------------------------------------------


class InvalidQueryParameter(ValueError):
    """Raised when a value cannot be safely interpolated in a Flux query."""


# Free-form tag values (city names from Netatmo, station names...). Unicode
# letters/digits, spaces and a few punctuation marks seen in French place names.
_SAFE_TAG_VALUE_RE = re.compile(r"^[\w][\w \-'’.()]{0,79}$", re.UNICODE)
# Source tags: 'netatmo-lidoire', 'open-meteo', 'infoclimat-<station>'...
_SAFE_SOURCE_RE = re.compile(r"^[\w][\w\-.]{0,79}$", re.UNICODE)
# Measurement / field / tag-key identifiers.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# Flux duration literal used for aggregateWindow(every: ...).
VALID_RESOLUTIONS = frozenset({"5m", "15m", "30m", "1h", "3h", "6h", "12h", "1d", "24h"})
_SUB_HOUR_RESOLUTIONS = frozenset({"5m", "15m", "30m"})
VALID_AGG_FNS = frozenset({"mean", "sum", "max", "min", "last", "first", "median", "count"})


def flux_string(value: str) -> str:
    """Render ``value`` as a quoted Flux string literal with proper escaping."""
    if not isinstance(value, str):
        raise InvalidQueryParameter(f"Expected a string, got {type(value).__name__}")
    if any(ord(ch) < 0x20 for ch in value):
        raise InvalidQueryParameter("Control characters are not allowed in Flux strings")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return f'"{escaped}"'


def known_station_ids() -> set[str]:
    """Hub'Eau station ids configured in settings (source of truth)."""
    return {s["id"] for s in get_settings().hubeau_stations}


def known_tide_ports() -> set[str]:
    settings = get_settings()
    return {settings.tide_port_bordeaux, settings.tide_port_libourne}


def validate_station_ids(station_ids: Iterable[str]) -> list[str]:
    """Return station ids if they are all configured, else raise."""
    known = known_station_ids()
    ids = list(station_ids)
    unknown = [s for s in ids if s not in known]
    if unknown:
        raise InvalidQueryParameter(f"Unknown station id(s): {unknown}")
    return ids


def validate_tag_value(value: str, kind: str = "tag value") -> str:
    if not isinstance(value, str) or not _SAFE_TAG_VALUE_RE.match(value):
        raise InvalidQueryParameter(f"Invalid {kind}: {value!r}")
    return value


def validate_source(source: str) -> str:
    if not isinstance(source, str) or not _SAFE_SOURCE_RE.match(source):
        raise InvalidQueryParameter(f"Invalid source: {source!r}")
    return source


def validate_identifier(name: str, kind: str = "identifier") -> str:
    if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
        raise InvalidQueryParameter(f"Invalid {kind}: {name!r}")
    return name


def validate_port(port: str) -> str:
    if port not in known_tide_ports():
        raise InvalidQueryParameter(f"Unknown tide port: {port!r}")
    return port


def validate_resolution(resolution: str) -> str:
    if resolution not in VALID_RESOLUTIONS:
        raise InvalidQueryParameter(f"Invalid resolution: {resolution!r}")
    return resolution


def validate_agg_fn(agg_fn: str) -> str:
    if agg_fn not in VALID_AGG_FNS:
        raise InvalidQueryParameter(f"Invalid aggregate function: {agg_fn!r}")
    return agg_fn


def _or_filter(column: str, values: Iterable[str]) -> str:
    """Build ``|> filter(fn: (r) => r.col == "a" or r.col == "b")``.

    ``values`` must already be validated; they are escaped here anyway.
    """
    validate_identifier(column, "column")
    values = list(values)
    if not values:
        return ""
    conditions = " or ".join(f"r.{column} == {flux_string(v)}" for v in values)
    return f"|> filter(fn: (r) => {conditions})"


# ---------------------------------------------------------------------------
# Non-blocking execution helpers
# ---------------------------------------------------------------------------


def _bucket() -> str:
    """Bucket name as an escaped Flux string literal."""
    return flux_string(get_settings().influxdb_bucket)


async def _run_query(query: str):
    """Run a Flux query in a worker thread (the client is synchronous)."""
    settings = get_settings()
    query_api = get_query_api()
    return await asyncio.to_thread(query_api.query, query, org=settings.influxdb_org)


async def _run_write(record) -> None:
    """Write point(s) in a worker thread (the client is synchronous)."""
    settings = get_settings()
    write_api = get_write_api()
    await asyncio.to_thread(
        write_api.write,
        bucket=settings.influxdb_bucket,
        org=settings.influxdb_org,
        record=record,
    )


# Global client instances. Queries use ``influxdb_read_token`` when it is set
# (read-only token: limits the blast radius of a Flux injection); writes always
# use ``influxdb_token``. A single client is shared when both tokens are equal.
_client: Optional[InfluxDBClient] = None
_query_client: Optional[InfluxDBClient] = None
_write_api = None
_query_api = None


def _new_client(token: str) -> InfluxDBClient:
    settings = get_settings()
    return InfluxDBClient(
        url=settings.influxdb_url,
        token=token,
        org=settings.influxdb_org,
    )


def get_client() -> InfluxDBClient:
    """Get or create the InfluxDB (write) client."""
    global _client, _write_api

    if _client is None:
        _client = _new_client(get_settings().influxdb_token)
        _write_api = _client.write_api(write_options=SYNCHRONOUS)

    return _client


def get_query_client() -> InfluxDBClient:
    """Get or create the InfluxDB client used for queries (read token)."""
    global _query_client, _query_api

    if _query_client is None:
        settings = get_settings()
        read_token = settings.influxdb_read_token or settings.influxdb_token
        if read_token == settings.influxdb_token:
            _query_client = get_client()
        else:
            _query_client = _new_client(read_token)
        _query_api = _query_client.query_api()

    return _query_client


def get_write_api():
    """Get the write API (write token)."""
    get_client()
    return _write_api


def get_query_api():
    """Get the query API (read token, falls back to the write token)."""
    get_query_client()
    return _query_api


async def write_river_level(
    station_id: str,
    station_name: str,
    river: str,
    height_m: float,
    timestamp: datetime,
    flow_m3s: Optional[float] = None,
) -> bool:
    """Write a river level measurement to InfluxDB."""
    try:
        point = (
            Point("river_level")
            .tag("station_id", station_id)
            .tag("station_name", station_name)
            .tag("river", river)
            .field("height_m", height_m)
            .time(timestamp, WritePrecision.S)
        )

        if flow_m3s is not None:
            point = point.field("flow_m3s", flow_m3s)

        await _run_write(point)
        return True
    except Exception as e:
        logger.error("Error writing river level to InfluxDB: %s", e)
        return False


async def write_river_levels_batch(levels: list[dict]) -> int:
    """Write multiple river level measurements in a batch."""
    written = 0

    try:
        points = []
        for lv in levels:
            point = (
                Point("river_level")
                .tag("station_id", lv["station_id"])
                .tag("station_name", lv.get("station_name", "Unknown"))
                .tag("river", lv.get("river", "Unknown"))
                .field("height_m", lv["height_m"])
                .time(lv["timestamp"], WritePrecision.S)
            )
            if lv.get("flow_m3s") is not None:
                point = point.field("flow_m3s", lv["flow_m3s"])
            points.append(point)

        await _run_write(points)
        written = len(points)
    except Exception as e:
        logger.error("Error writing river levels batch to InfluxDB: %s", e)

    return written


def make_tide_point(
    port: str,
    height_m: float,
    timestamp: datetime,
    tide_type: Optional[str] = None,
    coefficient: Optional[int] = None,
) -> Point:
    """Build a ``tide`` point (same shape as :func:`write_tide`)."""
    point = (
        Point("tide")
        .tag("port", port)
        .field("height_m", height_m)
        .time(timestamp, WritePrecision.S)
    )
    if tide_type:
        point = point.tag("tide_type", tide_type)
    if coefficient is not None:
        point = point.field("coefficient", coefficient)
    return point


async def write_tide(
    port: str,
    height_m: float,
    timestamp: datetime,
    tide_type: Optional[str] = None,
    coefficient: Optional[int] = None,
) -> bool:
    """Write a tide measurement to InfluxDB."""
    try:
        await _run_write(make_tide_point(port, height_m, timestamp, tide_type, coefficient))
        return True
    except Exception as e:
        logger.error("Error writing tide to InfluxDB: %s", e)
        return False


def make_rainfall_point(
    source: str,
    precipitation_mm_h: float,
    timestamp: datetime,
    precipitation_mm_24h: Optional[float] = None,
    station_city: Optional[str] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
) -> Point:
    """Build a ``rainfall`` point (same shape as :func:`write_rainfall`)."""
    # Ensure numeric values are floats, not None
    precip_h = float(precipitation_mm_h) if precipitation_mm_h is not None else 0.0

    point = (
        Point("rainfall")
        .tag("source", source)
        .field("precipitation_mm_h", precip_h)
        .time(timestamp, WritePrecision.S)
    )

    if precipitation_mm_24h is not None:
        point = point.field("precipitation_mm_24h", float(precipitation_mm_24h))
    if station_city:
        point = point.tag("city", str(station_city))
    if lat is not None:
        point = point.field("lat", float(lat))
    if lon is not None:
        point = point.field("lon", float(lon))
    return point


async def write_rainfall(
    source: str,
    precipitation_mm_h: float,
    timestamp: datetime,
    precipitation_mm_24h: Optional[float] = None,
    station_city: Optional[str] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
) -> bool:
    """Write rainfall observation to InfluxDB."""
    try:
        await _run_write(make_rainfall_point(
            source, precipitation_mm_h, timestamp,
            precipitation_mm_24h, station_city, lat, lon,
        ))
        return True
    except Exception as e:
        logger.error("Error writing rainfall to InfluxDB for %s/%s: %s", station_city, source, e)
        return False


def make_point(
    measurement: str,
    tags: dict,
    fields: dict,
    timestamp: datetime,
) -> Point:
    """Build a generic point (numbers stored as float, None fields skipped)."""
    point = Point(measurement).time(timestamp, WritePrecision.S)

    for key, value in tags.items():
        point = point.tag(key, str(value))

    for key, value in fields.items():
        if isinstance(value, (int, float)):
            point = point.field(key, float(value))
        elif value is not None:
            point = point.field(key, str(value))
    return point


async def write_point(
    measurement: str,
    tags: dict,
    fields: dict,
    timestamp: datetime,
) -> bool:
    """Write a generic point to InfluxDB."""
    try:
        await _run_write(make_point(measurement, tags, fields, timestamp))
        return True
    except Exception as e:
        logger.error("Error writing point to InfluxDB: %s", e)
        return False


def point_as_dict(point: Point) -> dict:
    """``{"measurement", "tags", "fields", "timestamp"}`` view of a Point (tests, logs)."""
    return {
        "measurement": point._name,
        "tags": dict(point._tags),
        "fields": dict(point._fields),
        "timestamp": point._time,
    }


async def write_points_batch(points: list, chunk_size: int = 5000) -> int:
    """Write many points with one HTTP write per ``chunk_size`` points.

    Non-blocking (goes through ``_run_write``). Each item is either an
    ``influxdb_client.Point`` (see ``make_point`` / ``make_tide_point`` /
    ``make_rainfall_point``) or a dict ``{"measurement", "tags", "fields",
    "timestamp"}`` with the same semantics as :func:`write_point`.
    Invalid items are skipped (logged). Returns the number of points
    actually written (0 when the write fails).
    """
    built = []
    for item in points:
        try:
            built.append(item if isinstance(item, Point) else make_point(**item))
        except Exception as e:
            logger.warning("Skipping invalid InfluxDB point %r: %s", item, e)

    written = 0
    for i in range(0, len(built), max(1, chunk_size)):
        chunk = built[i:i + chunk_size]
        try:
            await _run_write(chunk)
            written += len(chunk)
        except Exception as e:
            logger.error("Error writing batch of %d points to InfluxDB: %s", len(chunk), e)
    return written


# ``source`` tag of risk_score points: the hourly realtime job and the
# historical backfill compute the score from different inputs (fused gauges,
# rain forecast, wind and soil are only available live), so they are kept in
# separate series and readers prefer the realtime value for an hour that has
# both. Points written before this tag existed have no ``source``: they are
# read as RISK_SOURCE_UNKNOWN and treated like realtime points.
RISK_SOURCE_REALTIME = "realtime"
RISK_SOURCE_BACKFILL = "backfill"
RISK_SOURCE_UNKNOWN = "unknown"


def make_risk_point(
    score: float,
    level: str,
    timestamp: datetime,
    factors: Optional[dict] = None,
    model_version: Optional[str] = None,
    source: Optional[str] = None,
) -> Point:
    """Build a ``risk_score`` point (same shape as :func:`write_risk_score`)."""
    point = Point("risk_score").field("score", float(score)).field("level", str(level))
    if model_version:
        point = point.tag("model_version", str(model_version))
    if source:
        point = point.tag("source", str(source))
    point = point.time(timestamp, WritePrecision.S)

    if factors:
        for key, value in factors.items():
            if isinstance(value, bool):
                point = point.field(f"factor_{key}", 1.0 if value else 0.0)
            elif isinstance(value, (int, float)):
                point = point.field(f"factor_{key}", float(value))
    return point


async def write_risk_score(
    score: float,
    level: str,
    timestamp: datetime,
    factors: Optional[dict] = None,
    model_version: Optional[str] = None,
    source: Optional[str] = None,
) -> bool:
    """Write risk score to InfluxDB.

    Series key = measurement + ``model_version`` + ``source`` tags: ``level``
    is a FIELD, so re-running a backfill with the same model version
    overwrites the points instead of creating a parallel series. Legacy
    points (before v3.3) carry ``level`` as a tag and no ``model_version``;
    readers below handle both.
    """
    try:
        await _run_write(make_risk_point(score, level, timestamp, factors, model_version, source))
        return True
    except Exception as e:
        logger.error("Error writing risk score to InfluxDB: %s", e)
        return False


async def query_river_levels(
    station_id: str,
    start: datetime,
    end: datetime,
) -> list[dict]:
    """Query river level data from InfluxDB."""
    validate_station_ids([station_id])

    query = f'''
    from(bucket: {_bucket()})
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) => r._measurement == "river_level")
        |> filter(fn: (r) => r.station_id == {flux_string(station_id)})
        |> filter(fn: (r) => r._field == "height_m")
        |> sort(columns: ["_time"])
    '''

    try:
        tables = await _run_query(query)
        results = []

        for table in tables:
            for record in table.records:
                results.append({
                    "timestamp": record.get_time(),
                    "height_m": record.get_value(),
                    "station_id": record.values.get("station_id"),
                    "station_name": record.values.get("station_name"),
                    "river": record.values.get("river"),
                })

        results.sort(key=lambda r: r["timestamp"])
        return results
    except Exception as e:
        logger.error("Error querying river levels from InfluxDB: %s", e)
        return []


async def query_all_stations_levels(
    start: datetime,
    end: datetime,
) -> dict[str, list[dict]]:
    """Query river level data for all stations."""
    query = f'''
    from(bucket: {_bucket()})
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) => r._measurement == "river_level")
        |> filter(fn: (r) => r._field == "height_m")
        |> sort(columns: ["_time"])
    '''

    try:
        tables = await _run_query(query)
        results: dict[str, list[dict]] = {}

        for table in tables:
            for record in table.records:
                station_id = record.values.get("station_id", "unknown")
                if station_id not in results:
                    results[station_id] = []

                results[station_id].append({
                    "timestamp": record.get_time(),
                    "height_m": record.get_value(),
                    "station_name": record.values.get("station_name"),
                    "river": record.values.get("river"),
                })

        # A station may span several series (e.g. renamed station_name tag):
        # restore a global time order per station.
        for points in results.values():
            points.sort(key=lambda r: r["timestamp"])
        return results
    except Exception as e:
        logger.error("Error querying all stations from InfluxDB: %s", e)
        return {}


async def query_tides(
    port: str,
    start: datetime,
    end: datetime,
) -> list[dict]:
    """Query tide data from InfluxDB."""
    validate_port(port)

    query = f'''
    from(bucket: {_bucket()})
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) => r._measurement == "tide")
        |> filter(fn: (r) => r.port == {flux_string(port)})
        |> sort(columns: ["_time"])
    '''

    try:
        tables = await _run_query(query)
        results = []

        for table in tables:
            for record in table.records:
                results.append({
                    "timestamp": record.get_time(),
                    "height_m": record.get_value() if record.get_field() == "height_m" else None,
                    "coefficient": record.get_value() if record.get_field() == "coefficient" else None,
                    "port": record.values.get("port"),
                    "tide_type": record.values.get("tide_type"),
                })

        return results
    except Exception as e:
        logger.error("Error querying tides from InfluxDB: %s", e)
        return []


async def query_rainfall(
    start: datetime,
    end: datetime,
    source: Optional[str] = None,
) -> list[dict]:
    """Query rainfall data from InfluxDB."""
    source_filter = _or_filter("source", [validate_source(source)]) if source else ""

    query = f'''
    from(bucket: {_bucket()})
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) => r._measurement == "rainfall")
        {source_filter}
        |> sort(columns: ["_time"])
    '''

    try:
        tables = await _run_query(query)
        results = []

        for table in tables:
            for record in table.records:
                results.append({
                    "timestamp": record.get_time(),
                    "precipitation_mm_h": record.get_value() if record.get_field() == "precipitation_mm_h" else None,
                    "precipitation_mm_24h": record.get_value() if record.get_field() == "precipitation_mm_24h" else None,
                    "source": record.values.get("source"),
                    "city": record.values.get("city"),
                })

        return results
    except Exception as e:
        logger.error("Error querying rainfall from InfluxDB: %s", e)
        return []


# Netatmo's precipitation_mm_24h is a rolling 24h counter. A cumulative total
# over N days is obtained by sampling that counter at N non-overlapping 24h
# boundaries (T, T-24h, T-48h...) and summing the samples. A reading is
# accepted for boundary B if it lies in [B - 3h, B + 1h) (never after now+5min).
_COUNTER_SEARCH_BEFORE = timedelta(hours=3)
_COUNTER_SEARCH_AFTER = timedelta(hours=1)
_COUNTER_FUTURE_SLACK = timedelta(minutes=5)

RAIN_CUMUL_CACHE_TTL_S = 300
_rain_cumul_cache = TTLCache(ttl_seconds=RAIN_CUMUL_CACHE_TTL_S, max_entries=64)


def build_rainfall_counter_query(
    cities: list[str],
    source: str,
    start: datetime,
    stop: datetime,
) -> str:
    """Flux query returning, per city, the last 24h-counter reading of each hour.

    ``window(every: 1h) |> last()`` is used rather than
    ``aggregateWindow(fn: last)`` on purpose: the selector keeps the original
    ``_time`` of the reading, which the 24h-boundary sampling relies on, while
    still reducing ~4 readings/hour to 1 server-side.
    """
    validate_source(source)
    for city in cities:
        validate_tag_value(city, "city")

    return f'''
    from(bucket: {_bucket()})
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(stop)})
        |> filter(fn: (r) => r._measurement == "rainfall")
        |> filter(fn: (r) => r._field == "precipitation_mm_24h")
        {_or_filter("source", [source])}
        {_or_filter("city", cities)}
        |> window(every: 1h)
        |> last()
        |> group(columns: ["city"])
        |> sort(columns: ["_time"])
    '''


def sum_daily_counters(
    samples: list[tuple[datetime, Optional[float]]],
    now: datetime,
    num_days: int,
) -> Optional[float]:
    """Sum 24h-counter samples taken at ``num_days`` 24h boundaries before ``now``.

    ``samples`` are (naive UTC timestamp, value) pairs sorted by time.
    Returns None when no boundary has a reading.
    """
    total = 0.0
    found = False
    limit = now + _COUNTER_FUTURE_SLACK

    for day_offset in range(max(1, num_days)):
        target = now - timedelta(hours=24 * day_offset)
        lo = target - _COUNTER_SEARCH_BEFORE
        hi = min(target + _COUNTER_SEARCH_AFTER, limit)
        selected = None
        for ts, value in samples:
            if lo <= ts < hi and value is not None:
                selected = value  # samples are sorted: keep the last one
        if selected is not None:
            total += selected
            found = True

    return round(total, 1) if found else None


async def _query_rainfall_counters(
    cities: list[str],
    source: str,
    num_days: int,
    now: datetime,
) -> dict[str, list[tuple[datetime, Optional[float]]]]:
    """One Flux query: hourly counter readings per city covering ``num_days``."""
    start = now - timedelta(hours=24 * (max(1, num_days) - 1)) - _COUNTER_SEARCH_BEFORE
    stop = now + _COUNTER_FUTURE_SLACK
    query = build_rainfall_counter_query(cities, source, start, stop)

    tables = await _run_query(query)
    samples: dict[str, list[tuple[datetime, Optional[float]]]] = {c: [] for c in cities}
    for table in tables:
        for record in table.records:
            city = record.values.get("city")
            if city not in samples:
                continue
            samples[city].append((to_naive_utc(record.get_time()), record.get_value()))
    for points in samples.values():
        points.sort(key=lambda p: p[0])
    return samples


async def query_station_rainfall_cumulative(
    city: str,
    hours: int,
    source: str = "netatmo-lidoire",
) -> Optional[float]:
    """
    Query cumulative rainfall for a specific station over a period.

    Uses the precipitation_mm_24h field (Netatmo's built-in 24h rain gauge counter)
    sampled at 24h intervals. Each reading covers the preceding 24h, so sampling
    at 24h boundaries yields non-overlapping periods that can be summed.

    For example, for 72h:
      - Value at T:     rain from T-24h to T
      - Value at T-24h: rain from T-48h to T-24h
      - Value at T-48h: rain from T-72h to T-48h
      Sum = total rain from T-72h to T

    Args:
        city: Station city name
        hours: Number of hours to look back
        source: Data source filter

    Returns:
        Cumulative rainfall in mm, or None if no data
    """
    now = _utcnow()
    num_days = max(1, hours // 24)

    try:
        samples = await _query_rainfall_counters([city], source, num_days, now)
    except InvalidQueryParameter:
        raise
    except Exception as e:
        logger.error("Error querying cumulative rainfall for %s: %s", city, e)
        return None

    return sum_daily_counters(samples.get(city, []), now, num_days)


async def query_all_stations_rainfall_cumulative(
    cities: list[str],
    source: str = "netatmo-lidoire",
    aliases: Optional[dict[str, list[str]]] = None,
) -> dict[str, dict[str, Optional[float]]]:
    """
    Query cumulative rainfall for multiple stations over 24h, 72h, and 7 days.

    A single Flux query covers all cities and the full 7-day lookback (instead
    of cities x 11 sequential queries); results are cached in-process for
    ``RAIN_CUMUL_CACHE_TTL_S`` seconds since the Dashboard polls this path
    from several endpoints.

    ``aliases`` maps a requested city to other ``city`` tag values whose
    samples are merged into it (e.g. legacy raw Netatmo spellings written
    before city tags were canonicalised to the config spelling).

    Returns:
        Dict mapping city -> {rain_24h, rain_72h, rain_7d}
    """
    empty = {"rain_24h_mm": None, "rain_72h_mm": None, "rain_7d_mm": None}
    validate_source(source)
    # City names come from the Netatmo API: skip (don't fail on) unsafe ones.
    unique_cities: list[str] = []
    rejected: list[str] = []
    for c in dict.fromkeys(c for c in cities if c):
        try:
            unique_cities.append(validate_tag_value(c, "city"))
        except InvalidQueryParameter:
            rejected.append(c)
    if rejected:
        logger.warning("Skipping cities with unsafe names for rain cumul: %r", rejected)
    if not unique_cities:
        return {c: dict(empty) for c in rejected}

    alias_map: dict[str, list[str]] = {}
    for city in unique_cities:
        extra = []
        for alias in (aliases or {}).get(city, []):
            if not alias or alias == city or alias in extra:
                continue
            try:
                extra.append(validate_tag_value(alias, "city"))
            except InvalidQueryParameter:
                logger.warning("Skipping unsafe city alias for rain cumul: %r", alias)
        if extra:
            alias_map[city] = extra
    query_cities = list(dict.fromkeys(
        [*unique_cities, *(a for extra in alias_map.values() for a in extra)]
    ))

    cache_key = (
        source,
        tuple(sorted(unique_cities + rejected)),
        tuple(sorted((c, tuple(a)) for c, a in alias_map.items())),
    )
    cached = _rain_cumul_cache.get(cache_key)
    if cached is not None:
        return {city: dict(values) for city, values in cached.items()}

    now = _utcnow()
    try:
        samples = await _query_rainfall_counters(query_cities, source, 7, now)
    except InvalidQueryParameter:
        raise
    except Exception as e:
        logger.error("Error querying cumulative rainfall for %s: %s", unique_cities, e)
        # Not cached: retry on next call.
        return {city: dict(empty) for city in [*unique_cities, *rejected]}

    results: dict[str, dict[str, Optional[float]]] = {c: dict(empty) for c in rejected}
    for city in unique_cities:
        city_samples = list(samples.get(city, []))
        for alias in alias_map.get(city, []):
            city_samples.extend(samples.get(alias, []))
        city_samples.sort(key=lambda sample: sample[0])
        results[city] = {
            "rain_24h_mm": sum_daily_counters(city_samples, now, 1),
            "rain_72h_mm": sum_daily_counters(city_samples, now, 3),
            "rain_7d_mm": sum_daily_counters(city_samples, now, 7),
        }

    _rain_cumul_cache.set(cache_key, results)
    return {city: dict(values) for city, values in results.items()}


LEGACY_RISK_VERSION = "legacy"


def _risk_version_rank(version: Optional[str], preferred: Optional[str]) -> tuple:
    """Sort key: preferred version first, then newest numeric version, legacy last."""
    if version and version == preferred:
        return (0, ())
    if not version or version == LEGACY_RISK_VERSION:
        return (2, ())
    try:
        parts = tuple(-int(x) for x in version.split("."))
    except ValueError:
        parts = ()
    return (1, parts)


def _current_risk_model_version() -> Optional[str]:
    try:
        from app.services.risk_core import MODEL_VERSION
        return MODEL_VERSION
    except Exception:
        return None


def select_risk_rows_by_version(
    rows: list[dict],
    preferred_version: Optional[str] = None,
) -> list[dict]:
    """Keep one row per timestamp, preferring ``preferred_version``.

    Rows have "timestamp" and "model_version" (None for legacy points).
    """
    best: dict = {}
    for row in rows:
        ts = row["timestamp"]
        rank = _risk_version_rank(row.get("model_version"), preferred_version)
        if ts not in best or rank < best[ts][0]:
            best[ts] = (rank, row)
    return [best[ts][1] for ts in sorted(best)]


def _risk_source_rank(source: Optional[str]) -> int:
    """Realtime first, then untagged (pre-``source``) points, backfill last."""
    if source == RISK_SOURCE_BACKFILL:
        return 2
    if source == RISK_SOURCE_REALTIME:
        return 0
    return 1


def _hour_bucket(ts: datetime) -> datetime:
    return to_naive_utc(ts).replace(minute=0, second=0, microsecond=0)


def prefer_realtime_risk_rows(rows: list[dict]) -> list[dict]:
    """Drop backfill rows of an hour that also has a realtime/untagged row.

    Rows have "timestamp", "model_version" and "source" (None = untagged).
    Hours are compared per model version, so a backfill of a new model is
    never hidden by realtime points of an older one. Rows without any
    ``source`` (all data written before the tag existed) are unchanged.
    """
    covered = {
        (_hour_bucket(r["timestamp"]), r.get("model_version"))
        for r in rows if r.get("source") != RISK_SOURCE_BACKFILL
    }
    return [
        r for r in rows
        if r.get("source") != RISK_SOURCE_BACKFILL
        or (_hour_bucket(r["timestamp"]), r.get("model_version")) not in covered
    ]


def select_risk_window_rows(rows: list[dict], preferred_version: Optional[str] = None) -> list[dict]:
    """One row per (aggregated) window: preferred version, then realtime source."""
    best: dict = {}
    for row in rows:
        ts = row["timestamp"]
        rank = (
            _risk_version_rank(row.get("model_version"), preferred_version),
            _risk_source_rank(row.get("source")),
        )
        if ts not in best or rank < best[ts][0]:
            best[ts] = (rank, row)
    return [best[ts][1] for ts in sorted(best)]


def _risk_rows_from_tables(tables) -> list[dict]:
    """Rows from a pivoted risk_score query (one record per point)."""
    results = []
    for table in tables:
        for record in table.records:
            values = record.values
            score = values.get("score")
            if score is None:
                continue
            row = {
                "timestamp": record.get_time(),
                "score": score,
                "level": values.get("level"),
                "model_version": values.get("model_version"),
                "source": values.get("source"),
            }
            for key, value in values.items():
                if isinstance(key, str) and key.startswith("factor_"):
                    row[key] = value
            results.append(row)
    return results


async def query_risk_scores(
    start: datetime,
    end: datetime,
    model_version: Optional[str] = None,
) -> list[dict]:
    """Query risk scores from InfluxDB, one row per timestamp.

    Returns dicts with timestamp, score, level, model_version and factor_*
    fields. Reads both v3.3+ points (level field, model_version tag) and
    legacy points (level tag). When several model versions exist for the
    same timestamp, ``model_version`` (default: current model) wins, then
    the newest version, then legacy points. Within an hour, realtime points
    win over backfilled ones of the same version (see
    :func:`prefer_realtime_risk_rows`).
    """
    settings = get_settings()
    preferred = model_version or _current_risk_model_version()

    query = f'''
    from(bucket: "{settings.influxdb_bucket}")
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) => r._measurement == "risk_score")
        |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
        |> sort(columns: ["_time"])
    '''

    try:
        tables = await _run_query(query)
        rows = select_risk_rows_by_version(_risk_rows_from_tables(tables), preferred)
        return prefer_realtime_risk_rows(rows)
    except Exception as e:
        logger.error("Error querying risk scores from InfluxDB: %s", e)
        return []


async def get_data_stats() -> dict:
    """Get statistics about stored data (point counts over the last 30 days)."""
    stats = {
        "river_levels": 0,
        "tides": 0,
        "rainfall": 0,
        "risk_scores": 0,
        "oldest_data": None,
        "newest_data": None,
    }
    measurement_keys = {
        "river_level": "river_levels",
        "tide": "tides",
        "rainfall": "rainfall",
        "risk_score": "risk_scores",
    }

    async def _count(measurement: str) -> int:
        query = f'''
        from(bucket: {_bucket()})
            |> range(start: -30d)
            |> filter(fn: (r) => r._measurement == {flux_string(measurement)})
            |> count()
        '''
        try:
            tables = await _run_query(query)
        except Exception as e:
            logger.warning("Error counting %s points in InfluxDB: %s", measurement, e)
            return 0
        # One table per series: sum them (previously the last series won).
        return sum(int(r.get_value() or 0) for t in tables for r in t.records)

    counts = await asyncio.gather(*(_count(m) for m in measurement_keys))
    for measurement, total in zip(measurement_keys, counts):
        stats[measurement_keys[measurement]] = total

    return stats


def _rain_range_hour_end(start: datetime, end: datetime) -> str:
    """``range`` + ``timeShift`` stages for hour-end labelled rain points.

    Convention (same as ``risk_core.aggregate_hourly_rain``): a rain point
    stamped ``t`` covers the hour ENDING at ``t`` (Open-Meteo hourly values,
    Netatmo ``hourly_slot``), i.e. rain in ``(t - 1h, t]``. ``aggregateWindow``
    uses ``[start, stop)`` windows stamped at their stop, which would put a
    13:00 point (rain of 12:00-13:00) in the window stamped 14:00, one hour
    after the river levels measured in [12:00, 13:00) (stamped 13:00).

    Shifting every point by -1s maps ``(t - 1h, t]`` onto ``[t - 1h, t)``, so
    the window stamped ``t`` holds exactly the rain of the hour ending at
    ``t`` (sub-minute points are unaffected: precision is 1s). The range is
    moved by +1s first so that, after the shift, the table bounds are again
    exactly ``[start, end)`` (rain of ``(start, end]``).
    """
    return (
        f"|> range(start: {_flux_ts(start + timedelta(seconds=1))}, "
        f"stop: {_flux_ts(end + timedelta(seconds=1))})\n"
        "        |> timeShift(duration: -1s)"
    )


def _rain_aggregation(resolution: str, agg_fn: str) -> str:
    """Flux aggregation stage(s) for the ``precipitation_mm_h`` rate field.

    ``precipitation_mm_h`` is a *rate*: Netatmo historically wrote its sliding
    60-min total every 15 min, Open-Meteo writes one hourly value (plus "last
    hour" points at arbitrary minutes). Summing raw points therefore over-counts
    (~4x for Netatmo). For totals ("sum") we always reduce to one value per hour
    with ``mean`` first (= mm fallen in that hour, whatever the sampling), then:
      - 1h: that hourly value,
      - coarser (6h, 12h, 1d...): sum of the hourly values (mm per period),
      - sub-hour (15m...): mean rate x window fraction of an hour (mm per window).
    Other functions (mean/max/...) apply directly at ``resolution``.
    Input points are expected to be shifted by :func:`_rain_range_hour_end`
    (hour-end labels), so every window stamped ``t`` ends at ``t``.
    """
    validate_resolution(resolution)
    validate_agg_fn(agg_fn)

    if agg_fn != "sum":
        return f"|> aggregateWindow(every: {resolution}, fn: {agg_fn}, createEmpty: false)"

    if resolution in _SUB_HOUR_RESOLUTIONS:
        fraction = int(resolution[:-1]) / 60.0
        return (
            f"|> aggregateWindow(every: {resolution}, fn: mean, createEmpty: false)\n"
            f"        |> map(fn: (r) => ({{r with _value: r._value * {fraction}}}))"
        )
    if resolution == "1h":
        return "|> aggregateWindow(every: 1h, fn: mean, createEmpty: false)"
    # Stamp hourly means at the hour *start* so each hour lands in the right
    # coarse window; the coarse window is then stamped at its stop like every
    # other aggregateWindow output.
    return (
        '|> aggregateWindow(every: 1h, fn: mean, createEmpty: false, timeSrc: "_start")\n'
        f"        |> aggregateWindow(every: {resolution}, fn: sum, createEmpty: false)"
    )


def build_rainfall_by_station_query(
    start: datetime,
    end: datetime,
    sources: list[str] | None = None,
    cities: list[str] | None = None,
    resolution: str = "1h",
    agg_fn: str = "mean",
) -> str:
    """Build the Flux query used by ``query_rainfall_by_station`` (validated)."""
    source_filter = _or_filter("source", [validate_source(s) for s in sources]) if sources else ""
    city_filter = _or_filter("city", [validate_tag_value(c, "city") for c in cities]) if cities else ""

    return f'''
    from(bucket: {_bucket()})
        {_rain_range_hour_end(start, end)}
        |> filter(fn: (r) => r._measurement == "rainfall")
        |> filter(fn: (r) => r._field == "precipitation_mm_h")
        {source_filter}
        {city_filter}
        {_rain_aggregation(resolution, agg_fn)}
        |> sort(columns: ["_time"])
    '''


async def query_rainfall_by_station(
    start: datetime,
    end: datetime,
    sources: list[str] | None = None,
    cities: list[str] | None = None,
    resolution: str = "1h",
    agg_fn: str = "mean",
) -> dict[str, list[dict]]:
    """
    Query rainfall grouped by source:city.

    Args:
        start: Start datetime
        end: End datetime
        sources: Optional list of sources to filter (e.g., ["netatmo-lidoire"])
        cities: Optional list of cities to filter
        resolution: Aggregation resolution (default: 1h)
        agg_fn: Aggregation function — "mean" for rate (mm/h), "sum" for total
            (mm/period, computed from hourly means, see ``_rain_aggregation``)

    Returns:
        Dict mapping "source:city" -> list of {timestamp, value} points

    Raises:
        InvalidQueryParameter: on unsafe source/city/resolution/agg_fn values.
    """
    query = build_rainfall_by_station_query(start, end, sources, cities, resolution, agg_fn)

    try:
        tables = await _run_query(query)
        results: dict[str, list[dict]] = {}

        for table in tables:
            for record in table.records:
                source = record.values.get("source", "unknown")
                city = record.values.get("city", "unknown")
                key = f"{source}:{city}"

                if key not in results:
                    results[key] = []

                results[key].append({
                    "timestamp": record.get_time(),
                    "value": record.get_value(),
                })

        for points in results.values():
            points.sort(key=lambda p: p["timestamp"])
        return results
    except Exception as e:
        logger.error("Error querying rainfall by station from InfluxDB: %s", e)
        return {}


def build_river_levels_multi_station_query(
    start: datetime,
    end: datetime,
    station_ids: list[str] | None = None,
    resolution: str = "1h",
) -> str:
    """Build the Flux query used by ``query_river_levels_multi_station`` (validated)."""
    validate_resolution(resolution)
    station_filter = _or_filter("station_id", validate_station_ids(station_ids)) if station_ids else ""

    return f'''
    from(bucket: {_bucket()})
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) => r._measurement == "river_level")
        |> filter(fn: (r) => r._field == "height_m")
        {station_filter}
        |> aggregateWindow(every: {resolution}, fn: mean, createEmpty: false)
        |> sort(columns: ["_time"])
    '''


async def query_river_levels_multi_station(
    start: datetime,
    end: datetime,
    station_ids: list[str] | None = None,
    resolution: str = "1h",
) -> dict[str, list[dict]]:
    """
    Query river levels for multiple stations with aggregation.

    Args:
        start: Start datetime
        end: End datetime
        station_ids: Optional list of station IDs to filter (must be configured)
        resolution: Aggregation resolution (default: 1h)

    Returns:
        Dict mapping station_id -> list of {timestamp, value, station_name, river} points
    """
    query = build_river_levels_multi_station_query(start, end, station_ids, resolution)

    try:
        tables = await _run_query(query)
        results: dict[str, list[dict]] = {}

        for table in tables:
            for record in table.records:
                station_id = record.values.get("station_id", "unknown")

                if station_id not in results:
                    results[station_id] = []

                results[station_id].append({
                    "timestamp": record.get_time(),
                    "value": record.get_value(),
                    "station_name": record.values.get("station_name"),
                    "river": record.values.get("river"),
                })

        for points in results.values():
            points.sort(key=lambda p: p["timestamp"])
        return results
    except Exception as e:
        logger.error("Error querying multi-station river levels from InfluxDB: %s", e)
        return {}


async def query_tides_timeline(
    start: datetime,
    end: datetime,
    port: str = "LIBOURNE",
    resolution: str = "1h",
) -> list[dict]:
    """Query tide height data with aggregation for timeline display."""
    validate_port(port)
    validate_resolution(resolution)

    query = f'''
    from(bucket: {_bucket()})
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) => r._measurement == "tide")
        |> filter(fn: (r) => r.port == {flux_string(port)})
        |> filter(fn: (r) => r._field == "height_m")
        |> aggregateWindow(every: {resolution}, fn: mean, createEmpty: false)
        |> sort(columns: ["_time"])
    '''

    try:
        tables = await _run_query(query)
        results = []

        for table in tables:
            for record in table.records:
                results.append({
                    "timestamp": record.get_time(),
                    "value": record.get_value(),
                })

        results.sort(key=lambda p: p["timestamp"])
        return results
    except Exception as e:
        logger.error("Error querying tides timeline from InfluxDB: %s", e)
        return []


# ---------------------------------------------------------------------------
# Coverage (density) checks used by the timeline cache-through
# ---------------------------------------------------------------------------


def build_coverage_query(
    measurement: str,
    start: datetime,
    end: datetime,
    field: Optional[str] = None,
    filters: Optional[dict[str, list[str]]] = None,
    window: Optional[str] = None,
) -> str:
    """Flux query yielding ``count`` and ``first`` per series, without raw points.

    With ``window`` set, points are first bucketed with
    ``aggregateWindow(fn: count)`` so the count is the number of *windows
    with data* (e.g. hours with at least one reading), independent of the
    sampling rate.
    """
    validate_identifier(measurement, "measurement")
    if field is not None:
        validate_identifier(field, "field")
    if window is not None:
        validate_resolution(window)

    filter_lines = [f"|> filter(fn: (r) => r._measurement == {flux_string(measurement)})"]
    if field is not None:
        filter_lines.append(f"|> filter(fn: (r) => r._field == {flux_string(field)})")
    for column, values in (filters or {}).items():
        if column == "station_id":
            validate_station_ids(values)
        elif column == "source":
            for v in values:
                validate_source(v)
        else:
            for v in values:
                validate_tag_value(v, column)
        filter_lines.append(_or_filter(column, values))
    if window is not None:
        filter_lines.append(f"|> aggregateWindow(every: {window}, fn: count, createEmpty: false)")
    body = "\n        ".join(line for line in filter_lines if line)

    return f'''
    data = from(bucket: {_bucket()})
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        {body}

    data |> count() |> yield(name: "count")
    data |> first() |> yield(name: "first")
    '''


async def query_coverage(
    measurement: str,
    start: datetime,
    end: datetime,
    field: Optional[str] = None,
    filters: Optional[dict[str, list[str]]] = None,
    group_tag: Optional[str] = None,
    window: Optional[str] = None,
) -> Optional[dict[str, dict]]:
    """Count points (or windows with data) per ``group_tag`` value in one query.

    Returns ``{tag_value: {"count": int, "first": datetime | None}}`` (key
    ``"_all"`` when ``group_tag`` is None), or ``None`` if InfluxDB failed —
    callers must not treat a failure as "no data" and trigger backfills.
    """
    if group_tag is not None:
        validate_identifier(group_tag, "group tag")
    query = build_coverage_query(measurement, start, end, field, filters, window)

    try:
        tables = await _run_query(query)
    except Exception as e:
        logger.error("Error querying %s coverage from InfluxDB: %s", measurement, e)
        return None

    coverage: dict[str, dict] = {}
    for table in tables:
        for record in table.records:
            key = str(record.values.get(group_tag)) if group_tag else "_all"
            entry = coverage.setdefault(key, {"count": 0, "first": None})
            result_name = record.values.get("result")
            if result_name == "count":
                entry["count"] += int(record.get_value() or 0)
            elif result_name == "first":
                ts = record.get_time()
                if ts is not None:
                    ts = to_naive_utc(ts)
                    if entry["first"] is None or ts < entry["first"]:
                        entry["first"] = ts
    return coverage


async def query_risk_scores_timeline(
    start: datetime,
    end: datetime,
    resolution: str = "1h",
    model_version: Optional[str] = None,
) -> list[dict]:
    """Query risk scores with aggregation for timeline display.

    Aggregates per model version and source (legacy points, split by their
    level tag, are merged into one "legacy" series; untagged points get the
    "unknown" source) and keeps, for each window, the value of the preferred
    version (default: current model), else the newest one, and within a
    version the realtime series over the backfilled one.
    """
    settings = get_settings()
    preferred = model_version or _current_risk_model_version()

    query = f'''
    from(bucket: "{settings.influxdb_bucket}")
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) => r._measurement == "risk_score")
        |> filter(fn: (r) => r._field == "score")
        |> map(fn: (r) => ({{r with model_version: if exists r.model_version then r.model_version else "{LEGACY_RISK_VERSION}"}}))
        |> map(fn: (r) => ({{r with source: if exists r.source then r.source else "{RISK_SOURCE_UNKNOWN}"}}))
        |> group(columns: ["model_version", "source"])
        |> aggregateWindow(every: {resolution}, fn: mean, createEmpty: false)
        |> sort(columns: ["_time"])
    '''

    try:
        tables = await _run_query(query)
        results = []

        for table in tables:
            for record in table.records:
                results.append({
                    "timestamp": record.get_time(),
                    "value": record.get_value(),
                    "model_version": record.values.get("model_version"),
                    "source": record.values.get("source"),
                })

        return [
            {"timestamp": r["timestamp"], "value": r["value"]}
            for r in select_risk_window_rows(results, preferred)
        ]
    except Exception as e:
        logger.error("Error querying risk scores timeline from InfluxDB: %s", e)
        return []


def build_basin_sparkline_query(
    start: datetime,
    end: datetime,
    source: str = "netatmo-lidoire",
) -> str:
    """Hourly basin-mean rainfall rate across all stations of ``source``.

    ``group()`` merges every city into one table *before* aggregateWindow, so
    each hour is the mean over all stations (previously each city produced its
    own series and the timestamp dedup kept only the first city).
    """
    validate_source(source)
    return f'''
    from(bucket: {_bucket()})
        {_rain_range_hour_end(start, end)}
        |> filter(fn: (r) => r._measurement == "rainfall")
        |> filter(fn: (r) => r.source == {flux_string(source)})
        |> filter(fn: (r) => r._field == "precipitation_mm_h")
        |> group()
        |> sort(columns: ["_time"])
        |> aggregateWindow(every: 1h, fn: mean, createEmpty: true)
        |> fill(value: 0.0)
        |> sort(columns: ["_time"])
    '''


async def query_basin_rainfall_sparkline(hours: int = 24) -> list[dict]:
    """Query hourly basin rainfall for sparkline display (mean of netatmo-lidoire)."""
    hours = max(1, min(int(hours), 24 * 14))
    end = _utcnow()
    start = end - timedelta(hours=hours)

    query = build_basin_sparkline_query(start, end)

    try:
        tables = await _run_query(query)
        results = []

        for table in tables:
            for record in table.records:
                results.append({
                    "timestamp": record.get_time().isoformat(),
                    "value": round(record.get_value() or 0.0, 1),
                })

        # Single table after group(); keep a defensive dedup on timestamp.
        seen = set()
        deduped = []
        for r in results:
            if r["timestamp"] not in seen:
                seen.add(r["timestamp"])
                deduped.append(r)

        return deduped
    except Exception as e:
        logger.error("Error querying basin rainfall sparkline from InfluxDB: %s", e)
        return []


async def query_historical_snapshot(target_time: datetime, window_hours: int = 1) -> dict:
    """Query InfluxDB data around a specific historical timestamp for TimeMachine.

    Uses a single combined Flux query (1 round-trip instead of 4) and runs the
    synchronous InfluxDB call in a thread to avoid blocking the event loop.
    """
    settings = get_settings()
    start = target_time - timedelta(hours=window_hours)
    end = target_time + timedelta(hours=window_hours)

    snapshot = {
        "river_levels": {},
        "rainfall": [],
        "tides": [],
        "risk_scores": [],
    }

    # Single combined query — 1 round-trip instead of 4
    query = f'''
    from(bucket: "{settings.influxdb_bucket}")
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) =>
            r._measurement == "river_level" or
            r._measurement == "rainfall" or
            r._measurement == "tide" or
            r._measurement == "risk_score"
        )
        |> sort(columns: ["_time"])
    '''

    try:
        query_api = get_query_api()
        tables = await asyncio.to_thread(
            query_api.query, query, org=settings.influxdb_org
        )
        for table in tables:
            for record in table.records:
                measurement = record.values.get("_measurement")

                if measurement == "river_level" and record.get_field() == "height_m":
                    station_id = record.values.get("station_id", "unknown")
                    if station_id not in snapshot["river_levels"]:
                        snapshot["river_levels"][station_id] = []
                    snapshot["river_levels"][station_id].append({
                        "timestamp": record.get_time().isoformat(),
                        "height_m": record.get_value(),
                        "station_name": record.values.get("station_name"),
                        "river": record.values.get("river"),
                    })
                elif measurement == "rainfall":
                    snapshot["rainfall"].append({
                        "timestamp": record.get_time().isoformat(),
                        "value": record.get_value(),
                        "field": record.get_field(),
                        "source": record.values.get("source"),
                        "city": record.values.get("city"),
                    })
                elif measurement == "tide":
                    snapshot["tides"].append({
                        "timestamp": record.get_time().isoformat(),
                        "value": record.get_value(),
                        "field": record.get_field(),
                        "port": record.values.get("port"),
                        "tide_type": record.values.get("tide_type"),
                    })
                elif measurement == "risk_score":
                    snapshot["risk_scores"].append({
                        "timestamp": record.get_time().isoformat(),
                        "score": record.get_value() if record.get_field() == "score" else None,
                        "level": record.values.get("level"),
                    })
    except Exception as e:
        logger.error("Error querying historical snapshot: %s", e)

    return snapshot


async def query_latest_point(
    measurement: str,
    location: str,
    max_age_hours: int = 1,
) -> Optional[dict]:
    """Get most recent data point if it's within max_age window.

    Args:
        measurement: InfluxDB measurement name (e.g. "soil_moisture", "wind")
        location: Location tag value (e.g. "moulin")
        max_age_hours: Maximum age in hours to consider data fresh

    Returns:
        Dict of field values or None if no recent data
    """
    return await query_latest_measurement(measurement, {"location": location}, max_age_hours)


async def query_latest_measurement(
    measurement: str,
    tags: dict,
    max_age_hours: int = 1,
) -> Optional[dict]:
    """Generic: get latest point matching tags within age window.

    Args:
        measurement: InfluxDB measurement name
        tags: Dict of tag key-value pairs to filter on
        max_age_hours: Maximum age in hours

    Returns:
        Dict of field values or None if no recent data
    """
    validate_identifier(measurement, "measurement")
    tag_filters = "\n        ".join(
        f"|> filter(fn: (r) => r.{validate_identifier(k, 'tag key')} == "
        f"{flux_string(validate_tag_value(str(v), k))})"
        for k, v in tags.items()
    )
    start = _utcnow() - timedelta(hours=max_age_hours)

    query = f'''
    from(bucket: {_bucket()})
        |> range(start: {_flux_ts(start)})
        |> filter(fn: (r) => r._measurement == {flux_string(measurement)})
        {tag_filters}
        |> last()
    '''

    try:
        tables = await _run_query(query)
        if not tables:
            return None

        result = {}
        for table in tables:
            for record in table.records:
                result[record.get_field()] = record.get_value()
                result["_time"] = record.get_time()

        return result if result else None
    except Exception as e:
        logger.error("Error querying latest %s from InfluxDB: %s", measurement, e)
        return None


_SOIL_FIELDS = ("moisture_shallow", "moisture_deep", "soil_temp", "et0_mm")
# Realtime field -> (measurement, timeline field)
_SOIL_REALTIME_FIELDS = {
    "moisture": ("soil_moisture", "moisture_shallow"),
    "moisture_deep": ("soil_agricultural", "moisture_deep"),
    "temperature_0cm": ("soil_agricultural", "soil_temp"),
    "et0_mm": ("soil_agricultural", "et0_mm"),
}


def _location_field_query(
    measurement: str,
    start: datetime,
    end: datetime,
    fields: list[str],
    resolution: str,
) -> str:
    validate_identifier(measurement, "measurement")
    validate_resolution(resolution)
    for f in fields:
        validate_identifier(f, "field")
    return f'''
    from(bucket: {_bucket()})
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) => r._measurement == {flux_string(measurement)})
        |> filter(fn: (r) => r.location == "moulin")
        {_or_filter("_field", fields)}
        |> aggregateWindow(every: {resolution}, fn: mean, createEmpty: false)
        |> sort(columns: ["_time"])
    '''


async def query_soil_timeline(
    start: datetime,
    end: datetime,
    resolution: str = "1h",
    fields: list[str] | None = None,
) -> dict[str, list[dict]]:
    """Query soil timeline data from InfluxDB.

    Queries both:
    - 'soil_timeline' measurement (historical backfilled data)
    - 'soil_moisture' + 'soil_agricultural' measurements (realtime accumulated data)

    Merges and deduplicates by timestamp.

    Args:
        start: Start datetime
        end: End datetime
        resolution: Aggregation resolution (e.g. '1h', '6h')
        fields: Optional list of field names to include
            (moisture_shallow, moisture_deep, soil_temp, et0_mm)

    Returns:
        Dict mapping field_name -> list of {timestamp, value} points
    """
    requested_fields = [f for f in (fields or _SOIL_FIELDS) if f in _SOIL_FIELDS]
    results: dict[str, list[dict]] = {f: [] for f in requested_fields}
    if not requested_fields:
        return results

    # Query 1: soil_timeline measurement (backfilled historical data)
    query_timeline = _location_field_query("soil_timeline", start, end, requested_fields, resolution)

    try:
        tables = await _run_query(query_timeline)
        for table in tables:
            for record in table.records:
                field_name = record.get_field()
                if field_name in results:
                    results[field_name].append({
                        "timestamp": record.get_time(),
                        "value": record.get_value(),
                    })
    except Exception as e:
        logger.error("Error querying soil_timeline from InfluxDB: %s", e)

    # Query 2: Fallback - realtime soil_moisture + soil_agricultural measurements,
    # only for fields the primary query returned nothing for (run in parallel).
    fallbacks = [
        (realtime_field, measurement, timeline_field)
        for realtime_field, (measurement, timeline_field) in _SOIL_REALTIME_FIELDS.items()
        if timeline_field in results and not results[timeline_field]
    ]

    async def _fallback(realtime_field: str, measurement: str):
        query_rt = _location_field_query(measurement, start, end, [realtime_field], resolution)
        try:
            return await _run_query(query_rt)
        except Exception as e:
            logger.error("Error querying realtime %s.%s from InfluxDB: %s", measurement, realtime_field, e)
            return []

    fallback_tables = await asyncio.gather(*(_fallback(rf, m) for rf, m, _ in fallbacks))
    for (_rf, _m, timeline_field), tables in zip(fallbacks, fallback_tables):
        for table in tables:
            for record in table.records:
                results[timeline_field].append({
                    "timestamp": record.get_time(),
                    "value": record.get_value(),
                })

    return results


async def query_wind_timeline(
    start: datetime,
    end: datetime,
    resolution: str = "1h",
) -> dict[str, list[dict]]:
    """Query wind timeline data from InfluxDB.

    Returns dict mapping field_name -> list of {timestamp, value} points.
    Fields: speed_kmh, direction_deg, westerly_kmh
    """
    requested_fields = ["speed_kmh", "direction_deg", "westerly_kmh"]
    results: dict[str, list[dict]] = {f: [] for f in requested_fields}

    query = _location_field_query("wind", start, end, requested_fields, resolution)

    try:
        tables = await _run_query(query)
        for table in tables:
            for record in table.records:
                field_name = record.get_field()
                if field_name in results:
                    results[field_name].append({
                        "timestamp": record.get_time(),
                        "value": record.get_value(),
                    })
    except Exception as e:
        logger.error("Error querying wind timeline from InfluxDB: %s", e)

    return results


def close_client():
    """Close the InfluxDB client(s)."""
    global _client, _query_client, _write_api, _query_api
    if _query_client is not None and _query_client is not _client:
        _query_client.close()
    if _client:
        _client.close()
    _client = None
    _query_client = None
    _write_api = None
    _query_api = None
