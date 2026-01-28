"""InfluxDB client for time-series data storage.

Handles writing and querying flood monitoring data:
- River levels from Hub'Eau stations
- Tide heights and coefficients
- Rainfall observations from Netatmo/Infoclimat
- Risk scores
"""

from datetime import datetime, timedelta
from typing import Optional
import asyncio

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from app.config import get_settings


def _flux_ts(dt: datetime) -> str:
    """Format datetime as RFC3339 for Flux queries.

    Strips timezone info and appends Z to avoid
    producing invalid '...+00:00Z' when datetime is tz-aware.
    """
    naive = dt.replace(tzinfo=None) if dt.tzinfo else dt
    return naive.isoformat() + "Z"


# Global client instance
_client: Optional[InfluxDBClient] = None
_write_api = None
_query_api = None


def get_client() -> InfluxDBClient:
    """Get or create InfluxDB client."""
    global _client, _write_api, _query_api

    if _client is None:
        settings = get_settings()
        _client = InfluxDBClient(
            url=settings.influxdb_url,
            token=settings.influxdb_token,
            org=settings.influxdb_org,
        )
        _write_api = _client.write_api(write_options=SYNCHRONOUS)
        _query_api = _client.query_api()

    return _client


def get_write_api():
    """Get the write API."""
    get_client()
    return _write_api


def get_query_api():
    """Get the query API."""
    get_client()
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
    settings = get_settings()

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

        get_write_api().write(
            bucket=settings.influxdb_bucket,
            org=settings.influxdb_org,
            record=point,
        )
        return True
    except Exception as e:
        print(f"Error writing river level to InfluxDB: {e}")
        return False


async def write_river_levels_batch(levels: list[dict]) -> int:
    """Write multiple river level measurements in a batch."""
    settings = get_settings()
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

        get_write_api().write(
            bucket=settings.influxdb_bucket,
            org=settings.influxdb_org,
            record=points,
        )
        written = len(points)
    except Exception as e:
        print(f"Error writing river levels batch to InfluxDB: {e}")

    return written


async def write_tide(
    port: str,
    height_m: float,
    timestamp: datetime,
    tide_type: Optional[str] = None,
    coefficient: Optional[int] = None,
) -> bool:
    """Write a tide measurement to InfluxDB."""
    settings = get_settings()

    try:
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

        get_write_api().write(
            bucket=settings.influxdb_bucket,
            org=settings.influxdb_org,
            record=point,
        )
        return True
    except Exception as e:
        print(f"Error writing tide to InfluxDB: {e}")
        return False


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
    settings = get_settings()

    try:
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

        get_write_api().write(
            bucket=settings.influxdb_bucket,
            org=settings.influxdb_org,
            record=point,
        )
        return True
    except Exception as e:
        print(f"Error writing rainfall to InfluxDB for {station_city}/{source}: {e}")
        return False


async def write_point(
    measurement: str,
    tags: dict,
    fields: dict,
    timestamp: datetime,
) -> bool:
    """Write a generic point to InfluxDB."""
    settings = get_settings()

    try:
        point = Point(measurement).time(timestamp, WritePrecision.S)

        for key, value in tags.items():
            point = point.tag(key, str(value))

        for key, value in fields.items():
            if isinstance(value, (int, float)):
                point = point.field(key, float(value))
            elif value is not None:
                point = point.field(key, str(value))

        get_write_api().write(
            bucket=settings.influxdb_bucket,
            org=settings.influxdb_org,
            record=point,
        )
        return True
    except Exception as e:
        print(f"Error writing point to InfluxDB: {e}")
        return False


async def write_risk_score(
    score: float,
    level: str,
    timestamp: datetime,
    factors: Optional[dict] = None,
) -> bool:
    """Write risk score to InfluxDB."""
    settings = get_settings()

    try:
        point = (
            Point("risk_score")
            .tag("level", level)
            .field("score", score)
            .time(timestamp, WritePrecision.S)
        )

        if factors:
            for key, value in factors.items():
                if isinstance(value, (int, float)):
                    point = point.field(f"factor_{key}", float(value))

        get_write_api().write(
            bucket=settings.influxdb_bucket,
            org=settings.influxdb_org,
            record=point,
        )
        return True
    except Exception as e:
        print(f"Error writing risk score to InfluxDB: {e}")
        return False


async def query_river_levels(
    station_id: str,
    start: datetime,
    end: datetime,
) -> list[dict]:
    """Query river level data from InfluxDB."""
    settings = get_settings()

    query = f'''
    from(bucket: "{settings.influxdb_bucket}")
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) => r._measurement == "river_level")
        |> filter(fn: (r) => r.station_id == "{station_id}")
        |> filter(fn: (r) => r._field == "height_m")
        |> sort(columns: ["_time"])
    '''

    try:
        tables = get_query_api().query(query, org=settings.influxdb_org)
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

        return results
    except Exception as e:
        print(f"Error querying river levels from InfluxDB: {e}")
        return []


async def query_all_stations_levels(
    start: datetime,
    end: datetime,
) -> dict[str, list[dict]]:
    """Query river level data for all stations."""
    settings = get_settings()

    query = f'''
    from(bucket: "{settings.influxdb_bucket}")
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) => r._measurement == "river_level")
        |> filter(fn: (r) => r._field == "height_m")
        |> sort(columns: ["_time"])
    '''

    try:
        tables = get_query_api().query(query, org=settings.influxdb_org)
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

        return results
    except Exception as e:
        print(f"Error querying all stations from InfluxDB: {e}")
        return {}


async def query_tides(
    port: str,
    start: datetime,
    end: datetime,
) -> list[dict]:
    """Query tide data from InfluxDB."""
    settings = get_settings()

    query = f'''
    from(bucket: "{settings.influxdb_bucket}")
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) => r._measurement == "tide")
        |> filter(fn: (r) => r.port == "{port}")
        |> sort(columns: ["_time"])
    '''

    try:
        tables = get_query_api().query(query, org=settings.influxdb_org)
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
        print(f"Error querying tides from InfluxDB: {e}")
        return []


async def query_rainfall(
    start: datetime,
    end: datetime,
    source: Optional[str] = None,
) -> list[dict]:
    """Query rainfall data from InfluxDB."""
    settings = get_settings()

    source_filter = f'|> filter(fn: (r) => r.source == "{source}")' if source else ""

    query = f'''
    from(bucket: "{settings.influxdb_bucket}")
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) => r._measurement == "rainfall")
        {source_filter}
        |> sort(columns: ["_time"])
    '''

    try:
        tables = get_query_api().query(query, org=settings.influxdb_org)
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
        print(f"Error querying rainfall from InfluxDB: {e}")
        return []


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
    settings = get_settings()
    now = datetime.utcnow()

    # Break the total period into 24h chunks
    num_days = max(1, hours // 24)

    total_rain = 0.0
    data_found = False

    for day_offset in range(num_days):
        # For each 24h boundary, find the closest precipitation_mm_24h reading.
        # Search in a window around the target time.
        target = now - timedelta(hours=24 * day_offset)
        search_start = target - timedelta(hours=3)
        search_end = target + timedelta(hours=1)

        # Clamp to not go into the future
        if search_end > now + timedelta(minutes=5):
            search_end = now + timedelta(minutes=5)

        query = f'''
        from(bucket: "{settings.influxdb_bucket}")
            |> range(start: {_flux_ts(search_start)}, stop: {_flux_ts(search_end)})
            |> filter(fn: (r) => r._measurement == "rainfall")
            |> filter(fn: (r) => r.source == "{source}")
            |> filter(fn: (r) => r.city == "{city}")
            |> filter(fn: (r) => r._field == "precipitation_mm_24h")
            |> last()
        '''

        try:
            tables = get_query_api().query(query, org=settings.influxdb_org)

            for table in tables:
                for record in table.records:
                    value = record.get_value()
                    if value is not None:
                        total_rain += value
                        data_found = True
        except Exception as e:
            print(f"Error querying rainfall for {city} day-{day_offset}: {e}")

    return round(total_rain, 1) if data_found else None


async def query_all_stations_rainfall_cumulative(
    cities: list[str],
    source: str = "netatmo-lidoire",
) -> dict[str, dict[str, Optional[float]]]:
    """
    Query cumulative rainfall for multiple stations over 24h, 72h, and 7 days.

    Returns:
        Dict mapping city -> {rain_24h, rain_72h, rain_7d}
    """
    results = {}

    for city in cities:
        results[city] = {
            "rain_24h_mm": await query_station_rainfall_cumulative(city, 24, source),
            "rain_72h_mm": await query_station_rainfall_cumulative(city, 72, source),
            "rain_7d_mm": await query_station_rainfall_cumulative(city, 7 * 24, source),  # 7 days in hours
        }

    return results


async def query_risk_scores(
    start: datetime,
    end: datetime,
) -> list[dict]:
    """Query risk scores from InfluxDB."""
    settings = get_settings()

    query = f'''
    from(bucket: "{settings.influxdb_bucket}")
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) => r._measurement == "risk_score")
        |> sort(columns: ["_time"])
    '''

    try:
        tables = get_query_api().query(query, org=settings.influxdb_org)
        results = []

        for table in tables:
            for record in table.records:
                results.append({
                    "timestamp": record.get_time(),
                    "score": record.get_value() if record.get_field() == "score" else None,
                    "level": record.values.get("level"),
                })

        return results
    except Exception as e:
        print(f"Error querying risk scores from InfluxDB: {e}")
        return []


async def get_data_stats() -> dict:
    """Get statistics about stored data."""
    settings = get_settings()

    stats = {
        "river_levels": 0,
        "tides": 0,
        "rainfall": 0,
        "risk_scores": 0,
        "oldest_data": None,
        "newest_data": None,
    }

    for measurement in ["river_level", "tide", "rainfall", "risk_score"]:
        query = f'''
        from(bucket: "{settings.influxdb_bucket}")
            |> range(start: -30d)
            |> filter(fn: (r) => r._measurement == "{measurement}")
            |> count()
        '''

        try:
            tables = get_query_api().query(query, org=settings.influxdb_org)
            for table in tables:
                for record in table.records:
                    key = measurement.replace("_", "_") + "s"
                    if measurement == "river_level":
                        key = "river_levels"
                    elif measurement == "risk_score":
                        key = "risk_scores"
                    stats[key] = record.get_value()
        except Exception:
            pass

    return stats


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
        agg_fn: Aggregation function — "mean" for rate (mm/h), "sum" for total (mm/period)

    Returns:
        Dict mapping "source:city" -> list of {timestamp, value} points
    """
    settings = get_settings()

    # Build filters
    source_filter = ""
    if sources:
        source_conditions = " or ".join([f'r.source == "{s}"' for s in sources])
        source_filter = f"|> filter(fn: (r) => {source_conditions})"

    city_filter = ""
    if cities:
        city_conditions = " or ".join([f'r.city == "{c}"' for c in cities])
        city_filter = f"|> filter(fn: (r) => {city_conditions})"

    query = f'''
    from(bucket: "{settings.influxdb_bucket}")
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) => r._measurement == "rainfall")
        |> filter(fn: (r) => r._field == "precipitation_mm_h")
        {source_filter}
        {city_filter}
        |> aggregateWindow(every: {resolution}, fn: {agg_fn}, createEmpty: false)
        |> sort(columns: ["_time"])
    '''

    try:
        tables = get_query_api().query(query, org=settings.influxdb_org)
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

        return results
    except Exception as e:
        print(f"Error querying rainfall by station from InfluxDB: {e}")
        return {}


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
        station_ids: Optional list of station IDs to filter
        resolution: Aggregation resolution (default: 1h)

    Returns:
        Dict mapping station_id -> list of {timestamp, value, station_name, river} points
    """
    settings = get_settings()

    # Build station filter
    station_filter = ""
    if station_ids:
        station_conditions = " or ".join([f'r.station_id == "{s}"' for s in station_ids])
        station_filter = f"|> filter(fn: (r) => {station_conditions})"

    query = f'''
    from(bucket: "{settings.influxdb_bucket}")
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) => r._measurement == "river_level")
        |> filter(fn: (r) => r._field == "height_m")
        {station_filter}
        |> aggregateWindow(every: {resolution}, fn: mean, createEmpty: false)
        |> sort(columns: ["_time"])
    '''

    try:
        tables = get_query_api().query(query, org=settings.influxdb_org)
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

        return results
    except Exception as e:
        print(f"Error querying multi-station river levels from InfluxDB: {e}")
        return {}


async def query_tides_timeline(
    start: datetime,
    end: datetime,
    port: str = "LIBOURNE",
    resolution: str = "1h",
) -> list[dict]:
    """Query tide height data with aggregation for timeline display."""
    settings = get_settings()

    query = f'''
    from(bucket: "{settings.influxdb_bucket}")
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) => r._measurement == "tide")
        |> filter(fn: (r) => r.port == "{port}")
        |> filter(fn: (r) => r._field == "height_m")
        |> aggregateWindow(every: {resolution}, fn: mean, createEmpty: false)
        |> sort(columns: ["_time"])
    '''

    try:
        tables = get_query_api().query(query, org=settings.influxdb_org)
        results = []

        for table in tables:
            for record in table.records:
                results.append({
                    "timestamp": record.get_time(),
                    "value": record.get_value(),
                })

        return results
    except Exception as e:
        print(f"Error querying tides timeline from InfluxDB: {e}")
        return []


async def query_risk_scores_timeline(
    start: datetime,
    end: datetime,
    resolution: str = "1h",
) -> list[dict]:
    """Query risk scores with aggregation for timeline display."""
    settings = get_settings()

    query = f'''
    from(bucket: "{settings.influxdb_bucket}")
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) => r._measurement == "risk_score")
        |> filter(fn: (r) => r._field == "score")
        |> aggregateWindow(every: {resolution}, fn: mean, createEmpty: false)
        |> sort(columns: ["_time"])
    '''

    try:
        tables = get_query_api().query(query, org=settings.influxdb_org)
        results = []

        for table in tables:
            for record in table.records:
                results.append({
                    "timestamp": record.get_time(),
                    "value": record.get_value(),
                })

        return results
    except Exception as e:
        print(f"Error querying risk scores timeline from InfluxDB: {e}")
        return []


async def query_basin_rainfall_sparkline(hours: int = 24) -> list[dict]:
    """Query hourly basin rainfall for sparkline display (mean of netatmo-lidoire)."""
    settings = get_settings()
    end = datetime.utcnow()
    start = end - timedelta(hours=hours)

    query = f'''
    from(bucket: "{settings.influxdb_bucket}")
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) => r._measurement == "rainfall")
        |> filter(fn: (r) => r.source == "netatmo-lidoire")
        |> filter(fn: (r) => r._field == "precipitation_mm_h")
        |> aggregateWindow(every: 1h, fn: mean, createEmpty: true)
        |> fill(value: 0.0)
        |> sort(columns: ["_time"])
    '''

    try:
        tables = get_query_api().query(query, org=settings.influxdb_org)
        results = []

        for table in tables:
            for record in table.records:
                results.append({
                    "timestamp": record.get_time().isoformat(),
                    "value": round(record.get_value() or 0.0, 1),
                })

        # Deduplicate by timestamp (multiple stations may produce multiple rows per hour)
        seen = set()
        deduped = []
        for r in results:
            if r["timestamp"] not in seen:
                seen.add(r["timestamp"])
                deduped.append(r)

        return deduped
    except Exception as e:
        print(f"Error querying basin rainfall sparkline from InfluxDB: {e}")
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
        print(f"Error querying historical snapshot: {e}")

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
    settings = get_settings()
    start = datetime.utcnow() - timedelta(hours=max_age_hours)

    query = f'''
    from(bucket: "{settings.influxdb_bucket}")
        |> range(start: {_flux_ts(start)})
        |> filter(fn: (r) => r._measurement == "{measurement}")
        |> filter(fn: (r) => r.location == "{location}")
        |> last()
    '''

    try:
        tables = get_query_api().query(query, org=settings.influxdb_org)
        if not tables:
            return None

        result = {}
        for table in tables:
            for record in table.records:
                result[record.get_field()] = record.get_value()
                result["_time"] = record.get_time()

        return result if result else None
    except Exception as e:
        print(f"Error querying latest {measurement} from InfluxDB: {e}")
        return None


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
    settings = get_settings()
    start = datetime.utcnow() - timedelta(hours=max_age_hours)

    tag_filters = "\n        ".join(
        f'|> filter(fn: (r) => r.{k} == "{v}")' for k, v in tags.items()
    )

    query = f'''
    from(bucket: "{settings.influxdb_bucket}")
        |> range(start: {_flux_ts(start)})
        |> filter(fn: (r) => r._measurement == "{measurement}")
        {tag_filters}
        |> last()
    '''

    try:
        tables = get_query_api().query(query, org=settings.influxdb_org)
        if not tables:
            return None

        result = {}
        for table in tables:
            for record in table.records:
                result[record.get_field()] = record.get_value()
                result["_time"] = record.get_time()

        return result if result else None
    except Exception as e:
        print(f"Error querying latest {measurement} from InfluxDB: {e}")
        return None


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
    settings = get_settings()

    requested_fields = fields or ["moisture_shallow", "moisture_deep", "soil_temp", "et0_mm"]

    results: dict[str, list[dict]] = {f: [] for f in requested_fields}

    # Query 1: soil_timeline measurement (backfilled historical data)
    field_conditions = " or ".join([f'r._field == "{f}"' for f in requested_fields])
    query_timeline = f'''
    from(bucket: "{settings.influxdb_bucket}")
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) => r._measurement == "soil_timeline")
        |> filter(fn: (r) => r.location == "moulin")
        |> filter(fn: (r) => {field_conditions})
        |> aggregateWindow(every: {resolution}, fn: mean, createEmpty: false)
        |> sort(columns: ["_time"])
    '''

    try:
        tables = get_query_api().query(query_timeline, org=settings.influxdb_org)
        for table in tables:
            for record in table.records:
                field_name = record.get_field()
                if field_name in results:
                    results[field_name].append({
                        "timestamp": record.get_time(),
                        "value": record.get_value(),
                    })
    except Exception as e:
        print(f"Error querying soil_timeline from InfluxDB: {e}")

    # Query 2: Fallback - realtime soil_moisture + soil_agricultural measurements
    # Map realtime field names to timeline field names
    realtime_field_map = {
        "moisture": "moisture_shallow",
        "moisture_deep": "moisture_deep",
        "temperature_0cm": "soil_temp",
        "et0_mm": "et0_mm",
    }

    for realtime_field, timeline_field in realtime_field_map.items():
        if timeline_field not in requested_fields:
            continue
        # Only fill if the primary query returned no data for this field
        if results[timeline_field]:
            continue

        measurement = "soil_agricultural" if realtime_field != "moisture" else "soil_moisture"

        query_rt = f'''
        from(bucket: "{settings.influxdb_bucket}")
            |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
            |> filter(fn: (r) => r._measurement == "{measurement}")
            |> filter(fn: (r) => r.location == "moulin")
            |> filter(fn: (r) => r._field == "{realtime_field}")
            |> aggregateWindow(every: {resolution}, fn: mean, createEmpty: false)
            |> sort(columns: ["_time"])
        '''

        try:
            tables = get_query_api().query(query_rt, org=settings.influxdb_org)
            for table in tables:
                for record in table.records:
                    results[timeline_field].append({
                        "timestamp": record.get_time(),
                        "value": record.get_value(),
                    })
        except Exception as e:
            print(f"Error querying realtime {measurement}.{realtime_field} from InfluxDB: {e}")

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
    settings = get_settings()

    requested_fields = ["speed_kmh", "direction_deg", "westerly_kmh"]
    results: dict[str, list[dict]] = {f: [] for f in requested_fields}

    field_conditions = " or ".join([f'r._field == "{f}"' for f in requested_fields])
    query = f'''
    from(bucket: "{settings.influxdb_bucket}")
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) => r._measurement == "wind")
        |> filter(fn: (r) => r.location == "moulin")
        |> filter(fn: (r) => {field_conditions})
        |> aggregateWindow(every: {resolution}, fn: mean, createEmpty: false)
        |> sort(columns: ["_time"])
    '''

    try:
        tables = get_query_api().query(query, org=settings.influxdb_org)
        for table in tables:
            for record in table.records:
                field_name = record.get_field()
                if field_name in results:
                    results[field_name].append({
                        "timestamp": record.get_time(),
                        "value": record.get_value(),
                    })
    except Exception as e:
        print(f"Error querying wind timeline from InfluxDB: {e}")

    return results


def close_client():
    """Close the InfluxDB client."""
    global _client, _write_api, _query_api
    if _client:
        _client.close()
        _client = None
        _write_api = None
        _query_api = None
