"""InfluxDB reads of the probabilistic / observed-rain series.

Measurements (written by the scheduler jobs only, never read by the risk
model):

- ``radar_rain``: OPERA ACRR basin rain, tags ``source=opera``, ``basin``,
  ``product``; one point per image (end of the 1 h accumulation).
- ``glofas_forecast``: GloFAS v4 Lidoire cell, tags ``cell``, ``run``
  (init date, ``YYYY-MM-DD``), ``lead`` (days); timestamp = valid day.
- ``glofas_climatology``: percentiles of the consolidated reanalysis, tag ``cell``.
- ``ensemble_rain``: Open-Meteo Ensemble basin rain, tags ``model``, ``run``
  (``YYYY-MM-DDTHHZ``), ``window`` (``24h`` / ``72h`` / ``run``), ``lead``
  (hours from the run to the window end); timestamp = window end. The
  ``window=run`` row (timestamp = run) indexes the collected runs.

Query builders are pure (tests check the Flux); every interpolated value is
validated or comes from constants.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from app.services.basin import BASIN_ID
from app.services.influx_client import (
    _bucket,
    _flux_ts,
    _run_query,
    flux_string,
    to_naive_utc,
    validate_identifier,
    validate_resolution,
    validate_tag_value,
)

logger = logging.getLogger(__name__)

RADAR_MEASUREMENT = "radar_rain"
GLOFAS_MEASUREMENT = "glofas_forecast"
GLOFAS_CLIMATOLOGY_MEASUREMENT = "glofas_climatology"
ENSEMBLE_MEASUREMENT = "ensemble_rain"
GLOFAS_CELL = BASIN_ID

RADAR_FIELDS = ("basin_mean_mm", "max_pixel_mm", "coverage", "quality", "pixels")


def build_radar_query(start: datetime, end: datetime, source: str = "opera", basin: str = BASIN_ID) -> str:
    return f'''
    from(bucket: {_bucket()})
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) => r._measurement == {flux_string(RADAR_MEASUREMENT)})
        |> filter(fn: (r) => r.source == {flux_string(validate_tag_value(source, "source"))})
        |> filter(fn: (r) => r.basin == {flux_string(validate_tag_value(basin, "basin"))})
        |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
        |> group()
        |> sort(columns: ["_time"])
    '''


def radar_rows_from_tables(tables) -> list[dict]:
    rows = []
    for table in tables:
        for record in table.records:
            values = record.values
            ts = record.get_time()
            if ts is None:
                continue
            row = {"timestamp": to_naive_utc(ts)}
            for key in RADAR_FIELDS:
                row[key] = values.get(key)
            rows.append(row)
    rows.sort(key=lambda r: r["timestamp"])
    return rows


async def query_radar_points(start: datetime, end: datetime) -> list[dict]:
    """Stored OPERA basin rain points in [start, end) (errors propagate)."""
    tables = await _run_query(build_radar_query(start, end))
    return radar_rows_from_tables(tables)


async def query_radar_times(start: datetime, end: datetime) -> set[datetime]:
    return {r["timestamp"] for r in await query_radar_points(start, end)}


def aggregate_radar_series(rows: list[dict], resolution: str) -> list[dict]:
    """Timeline points of the basin radar rain.

    ``15m``: every image (1 h accumulation ending at the point, read as mm/h).
    ``1h`` and coarser: only the images on the hour (non-overlapping 1 h
    accumulations), summed per window for 6h/12h/1d (mm per period, like the
    other rain series at coarse resolutions).
    """
    resolution = validate_resolution(resolution)
    pts = [r for r in rows if r.get("basin_mean_mm") is not None]
    if resolution in ("5m", "15m"):
        return [{"timestamp": r["timestamp"], "value": r["basin_mean_mm"]} for r in pts]
    hourly = [r for r in pts if r["timestamp"].minute == 0 and r["timestamp"].second == 0]
    if resolution in ("1h", "30m"):
        return [{"timestamp": r["timestamp"], "value": r["basin_mean_mm"]} for r in hourly]
    hours = {"3h": 3, "6h": 6, "12h": 12, "1d": 24, "24h": 24}[resolution]
    sums: dict[datetime, float] = {}
    for r in hourly:
        # The accumulation covers (t - 1 h, t]: window of its first hour,
        # labelled by the window end (like Flux aggregateWindow).
        first = r["timestamp"] - timedelta(hours=1)
        day = first.replace(hour=0, minute=0, second=0, microsecond=0)
        label = day + timedelta(hours=(first.hour // hours + 1) * hours)
        sums[label] = sums.get(label, 0.0) + float(r["basin_mean_mm"])
    return [{"timestamp": k, "value": round(v, 2)} for k, v in sorted(sums.items())]


# ---------------------------------------------------------------------------
# GloFAS
# ---------------------------------------------------------------------------

GLOFAS_FIELDS = ("p10", "p50", "p90", "min", "max", "mean", "prob_gt_p95", "prob_gt_p99", "members")


def build_glofas_query(start: datetime, end: datetime, fields: tuple[str, ...] = GLOFAS_FIELDS) -> str:
    field_filter = " or ".join(
        f"r._field == {flux_string(validate_identifier(f, 'field'))}" for f in fields
    )
    return f'''
    from(bucket: {_bucket()})
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) => r._measurement == {flux_string(GLOFAS_MEASUREMENT)})
        |> filter(fn: (r) => r.cell == {flux_string(GLOFAS_CELL)})
        |> filter(fn: (r) => {field_filter})
        |> pivot(rowKey: ["_time", "run", "lead"], columnKey: ["_field"], valueColumn: "_value")
        |> group()
        |> sort(columns: ["_time"])
    '''


def glofas_rows_from_tables(tables, fields: tuple[str, ...] = GLOFAS_FIELDS) -> list[dict]:
    rows = []
    for table in tables:
        for record in table.records:
            values = record.values
            ts = record.get_time()
            if ts is None or not values.get("run"):
                continue
            row = {"timestamp": to_naive_utc(ts), "run": values.get("run"), "lead": values.get("lead")}
            for key in fields:
                row[key] = values.get(key)
            rows.append(row)
    return rows


async def query_glofas_rows(start: datetime, end: datetime, fields: tuple[str, ...] = GLOFAS_FIELDS) -> list[dict]:
    """Stored GloFAS run rows whose valid day is in [start, end) (errors propagate)."""
    tables = await _run_query(build_glofas_query(start, end, fields))
    return glofas_rows_from_tables(tables, fields)


def latest_run_per_day(rows: list[dict], field: str = "p50") -> list[dict]:
    """For each valid day, the value of the most recent run (shortest lead)."""
    best: dict[datetime, dict] = {}
    for r in rows:
        if r.get(field) is None:
            continue
        cur = best.get(r["timestamp"])
        if cur is None or str(r["run"]) > str(cur["run"]):
            best[r["timestamp"]] = r
    return [{"timestamp": t, "value": best[t][field]} for t in sorted(best)]


def latest_run_rows(rows: list[dict]) -> list[dict]:
    """Rows of the newest run only, sorted by valid day."""
    runs = [str(r["run"]) for r in rows if r.get("run")]
    if not runs:
        return []
    newest = max(runs)
    return sorted((r for r in rows if str(r.get("run")) == newest), key=lambda r: r["timestamp"])


async def query_glofas_climatology(max_age_days: int = 400) -> Optional[dict]:
    """Latest stored climatology point of the Lidoire cell, or None."""
    from app.services.influx_client import query_latest_measurement

    return await query_latest_measurement(
        GLOFAS_CLIMATOLOGY_MEASUREMENT, {"cell": GLOFAS_CELL}, max_age_hours=24 * max_age_days,
    )
