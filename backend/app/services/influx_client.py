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
        point = (
            Point("rainfall")
            .tag("source", source)
            .field("precipitation_mm_h", precipitation_mm_h)
            .time(timestamp, WritePrecision.S)
        )

        if precipitation_mm_24h is not None:
            point = point.field("precipitation_mm_24h", precipitation_mm_24h)
        if station_city:
            point = point.tag("city", station_city)
        if lat is not None:
            point = point.field("lat", lat)
        if lon is not None:
            point = point.field("lon", lon)

        get_write_api().write(
            bucket=settings.influxdb_bucket,
            org=settings.influxdb_org,
            record=point,
        )
        return True
    except Exception as e:
        print(f"Error writing rainfall to InfluxDB: {e}")
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
                    point = point.field(f"factor_{key}", value)

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
        |> range(start: {start.isoformat()}Z, stop: {end.isoformat()}Z)
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
        |> range(start: {start.isoformat()}Z, stop: {end.isoformat()}Z)
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
        |> range(start: {start.isoformat()}Z, stop: {end.isoformat()}Z)
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
        |> range(start: {start.isoformat()}Z, stop: {end.isoformat()}Z)
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


async def query_risk_scores(
    start: datetime,
    end: datetime,
) -> list[dict]:
    """Query risk scores from InfluxDB."""
    settings = get_settings()

    query = f'''
    from(bucket: "{settings.influxdb_bucket}")
        |> range(start: {start.isoformat()}Z, stop: {end.isoformat()}Z)
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


def close_client():
    """Close the InfluxDB client."""
    global _client, _write_api, _query_api
    if _client:
        _client.close()
        _client = None
        _write_api = None
        _query_api = None
