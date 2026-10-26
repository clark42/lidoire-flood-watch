"""Hydrological indicators: series catalogue, InfluxDB reads, /api/indicators.

Indicator series (``river_flow``, ``groundwater``, ``tide_observed``, see
``collectors/hydro_indicators.py``) are for display, evaluation and a future
v4. The risk model v3.4 never reads them: nothing in risk_core, risk_v2,
forecast or risk_shadow imports this module (enforced by a test).

Reads only InfluxDB (never an external API): the scheduler and the explicit
backfill fill the measurements.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException

from app.config import get_settings
from app.models.schemas import utc_isoformat
from app.services import climatology
from app.services.cache import TTLCache
from app.services.collectors.hydro_indicators import (
    FLOW_MEASUREMENT,
    GROUNDWATER_MEASUREMENT,
    TIDE_OBSERVED_MEASUREMENT,
)
from app.services.influx_client import (
    _bucket,
    _flux_ts,
    _run_query,
    flux_string,
    to_naive_utc,
    validate_agg_fn,
    validate_identifier,
    validate_resolution,
    validate_tag_value,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Climatology reference period start (same as the episodes / backfill)
HISTORY_START = datetime(2022, 11, 1)
HISTORY_CACHE_TTL_S = 6 * 3600
PAYLOAD_CACHE_TTL_S = 120
RECENT_HOURS = 48

_FLOW_COLORS = {
    "EYRAUD": "#16a34a",
    "CAUDEAU": "#65a30d",
    "PESSAC": "#0284c7",
    "LAMONZIE": "#0369a1",
    "ARGENTAT": "#7c3aed",
}
_GW_COLORS = ["#a16207", "#854d0e"]
_TIDE_COLORS = {"BORDEAUX": "#1d4ed8", "BEC_AMBES": "#4338ca"}
_DEFAULT_COLOR = "#6b7280"

# Dordogne mechanism: every flow role except the runoff analogs
RUNOFF_ROLES = frozenset({"runoff_analog"})

_history_cache = TTLCache(ttl_seconds=HISTORY_CACHE_TTL_S, max_entries=64)
_payload_cache = TTLCache(ttl_seconds=PAYLOAD_CACHE_TTL_S, max_entries=2)


# ---------------------------------------------------------------------------
# Catalogue (config is the source of truth)
# ---------------------------------------------------------------------------


def series_catalogue() -> dict[str, dict]:
    """Timeline id -> spec (measurement, tag key, field, display, daily agg)."""
    settings = get_settings()
    catalogue: dict[str, dict] = {}
    for s in settings.indicator_flow_stations:
        tributary = s.get("role") in RUNOFF_ROLES
        catalogue[f"flow:{s['key']}"] = {
            "kind": "flow",
            "type": "flow_tributary" if tributary else "flow_dordogne",
            "measurement": FLOW_MEASUREMENT,
            "key": s["key"],
            "field": "q_m3s",
            "daily_fn": "mean",
            "name": f"Debit {s['name']}",
            "unit": "m³/s",
            "color": _FLOW_COLORS.get(s["key"], _DEFAULT_COLOR),
            "station": s,
        }
    for idx, s in enumerate(settings.indicator_groundwater_stations):
        catalogue[f"gw:{s['key']}"] = {
            "kind": "groundwater",
            "type": "groundwater",
            "measurement": GROUNDWATER_MEASUREMENT,
            "key": s["key"],
            "field": "level_m_ngf",
            "daily_fn": "mean",
            "name": f"Nappe {s['name']}",
            "unit": "m NGF",
            "color": _GW_COLORS[idx % len(_GW_COLORS)],
            "station": s,
        }
    for g in settings.indicator_tide_gauges:
        catalogue[f"tide_obs:{g['key']}"] = {
            "kind": "tide_observed",
            "type": "tide_observed",
            "measurement": TIDE_OBSERVED_MEASUREMENT,
            "key": g["key"],
            "field": "height_m",
            "daily_fn": "max",
            "name": f"Maree observee {g['name']}",
            "unit": "m",
            "color": _TIDE_COLORS.get(g["key"], _DEFAULT_COLOR),
            "station": g,
        }
    return catalogue


def series_infos() -> list[dict]:
    """Public description of every indicator series (``/timeline/sources``)."""
    out = []
    for sid, spec in series_catalogue().items():
        station = spec["station"]
        out.append({
            "id": sid,
            "kind": spec["kind"],
            "type": spec["type"],
            "name": spec["name"],
            "unit": spec["unit"],
            "color": spec["color"],
            "role": station.get("role"),
            "distance_km": station.get("distance_km", 0.0),
        })
    return out


def validate_series_ids(raw: list[str]) -> list[str]:
    """Keep known indicator ids (deduplicated), 422 ``{error, unknown, valid}`` otherwise."""
    catalogue = series_catalogue()
    unknown = [s for s in raw if s not in catalogue]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail={"error": "unknown indicator series", "unknown": unknown, "valid": list(catalogue)},
        )
    return list(dict.fromkeys(raw))


# ---------------------------------------------------------------------------
# InfluxDB reads
# ---------------------------------------------------------------------------


def build_series_query(spec: dict, start: datetime, end: datetime,
                       every: Optional[str] = None, fn: str = "mean") -> str:
    """Flux query for one indicator series; raw points when ``every`` is None."""
    measurement = validate_identifier(spec["measurement"], "measurement")
    field = validate_identifier(spec["field"], "field")
    key = validate_tag_value(spec["key"], "indicator key")
    window = ""
    if every is not None:
        validate_resolution(every)
        validate_agg_fn(fn)
        window = f"|> aggregateWindow(every: {every}, fn: {fn}, createEmpty: false)"
    return f'''
    from(bucket: {_bucket()})
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) => r._measurement == {flux_string(measurement)})
        |> filter(fn: (r) => r._field == {flux_string(field)})
        |> filter(fn: (r) => r.key == {flux_string(key)})
        |> group(columns: ["_measurement", "_field", "key"])
        {window}
        |> sort(columns: ["_time"])
    '''


async def query_series(spec: dict, start: datetime, end: datetime,
                       every: Optional[str] = None, fn: str = "mean") -> list[dict]:
    """``[{timestamp, value}]`` sorted; [] on InfluxDB error (logged)."""
    query = build_series_query(spec, start, end, every, fn)
    try:
        tables = await _run_query(query)
    except Exception as e:
        logger.error("Error querying indicator %s from InfluxDB: %s", spec.get("key"), e)
        return []
    points = [
        {"timestamp": record.get_time(), "value": record.get_value()}
        for table in tables for record in table.records
        if record.get_value() is not None
    ]
    points.sort(key=lambda p: p["timestamp"])
    return points


async def daily_history(series_id: str, spec: dict, now: datetime) -> list[float]:
    """Daily aggregates (mean, or max for the tide) since HISTORY_START, cached 6 h."""
    cached = _history_cache.get(series_id)
    if cached is not None:
        return cached
    points = await query_series(spec, HISTORY_START, now, every="1d", fn=spec["daily_fn"])
    values = [float(p["value"]) for p in points]
    if values:
        _history_cache.set(series_id, values)
    return values


# ---------------------------------------------------------------------------
# /api/indicators payload
# ---------------------------------------------------------------------------


def _age_entry(last: Optional[tuple[datetime, float]], now: datetime, max_age_hours: float) -> dict:
    if last is None:
        return {"available": False, "last_timestamp": None, "age_hours": None, "stale": None}
    age = round((now - last[0]).total_seconds() / 3600, 2)
    return {
        "available": True,
        "last_timestamp": utc_isoformat(last[0]),
        "age_hours": age,
        "stale": age > max_age_hours,
    }


def _rounded(value: Optional[float], digits: int = 3) -> Optional[float]:
    return None if value is None else round(value, digits)


def build_flow_indicator(series_id: str, spec: dict, recent: list[dict],
                         history: list[float], now: datetime) -> dict:
    """Flow entry: Q now, 24 h mean, 3 h trend, percentile and threshold of the 24 h mean."""
    station = spec["station"]
    last = climatology.latest(recent, now)
    q_now = last[1] if last else None
    q_24h = climatology.trailing_mean(recent, now, 24) if last else None
    slope = climatology.slope_per_hour(recent, now, window_h=3)
    band = max(0.01, 0.02 * abs(q_now)) if q_now is not None else 0.01
    percentile = climatology.percentile_of(q_24h, history)
    thresholds = station.get("thresholds_daily_m3s", {})
    return {
        "id": series_id,
        "key": station["key"],
        "station_id": station["id"],
        "name": station["name"],
        "river": station.get("river"),
        "role": station.get("role"),
        "basin_km2": station.get("basin_km2"),
        "distance_km": station.get("distance_km"),
        **_age_entry(last, now, station.get("max_age_hours", 3.0)),
        "q_m3s": _rounded(q_now),
        "q_mean_24h_m3s": _rounded(q_24h),
        "trend_m3s_per_h": _rounded(slope, 4),
        "trend": climatology.trend_label(slope, band),
        "percentile": percentile,
        "percentile_label": climatology.percentile_label(percentile),
        "percentile_basis": "moyenne 24 h vs moyennes journalieres depuis 2022-11",
        "history_days": len(history),
        "thresholds_daily_m3s": thresholds,
        "threshold": climatology.threshold_status(q_24h, thresholds),
    }


def build_groundwater_indicator(series_id: str, spec: dict, recent: list[dict],
                                history: list[float], now: datetime) -> dict:
    station = spec["station"]
    last = climatology.latest(recent, now)
    level = last[1] if last else None
    before = climatology.latest(recent, now - timedelta(hours=24))
    change_24h = (level - before[1]) if (last and before) else None
    slope = climatology.slope_per_hour(recent, now, window_h=24, min_span_h=6)
    percentile = climatology.percentile_of(level, history)
    thresholds = station.get("thresholds_m_ngf", {})
    return {
        "id": series_id,
        "key": station["key"],
        "code_bss": station["code_bss"],
        "name": station["name"],
        "aquifer": station.get("aquifer"),
        "distance_km": station.get("distance_km"),
        **_age_entry(last, now, station.get("max_age_hours", 36.0)),
        "level_m_ngf": _rounded(level),
        "change_24h_m": _rounded(change_24h),
        "trend": climatology.trend_label(slope, 0.002),
        "percentile": percentile,
        "percentile_label": climatology.percentile_label(percentile),
        "percentile_basis": "niveau vs moyennes journalieres depuis 2022-11",
        "history_days": len(history),
        "thresholds_m_ngf": thresholds,
        "threshold": climatology.threshold_status(level, thresholds),
    }


def build_tide_indicator(series_id: str, spec: dict, recent: list[dict],
                         history: list[float], now: datetime) -> dict:
    gauge = spec["station"]
    last = climatology.latest(recent, now)
    window = [p for p in recent if p.get("value") is not None
              and now - timedelta(hours=24) < to_naive_utc(p["timestamp"]) <= now]
    max_24h = max((float(p["value"]) for p in window), default=None)
    percentile = climatology.percentile_of(max_24h, history)
    return {
        "id": series_id,
        "key": gauge["key"],
        "name": gauge["name"],
        "source": gauge.get("source"),
        "datum": gauge.get("datum"),
        "distance_km": gauge.get("distance_km"),
        **_age_entry(last, now, gauge.get("max_age_hours", 3.0)),
        "height_m": _rounded(last[1] if last else None),
        "max_24h_m": _rounded(max_24h),
        "percentile": percentile,
        "percentile_label": climatology.percentile_label(percentile),
        "percentile_basis": "max 24 h vs max journaliers depuis 2022-11",
        "history_days": len(history),
    }


_BUILDERS = {
    "flow": build_flow_indicator,
    "groundwater": build_groundwater_indicator,
    "tide_observed": build_tide_indicator,
}


async def get_indicators(now: Optional[datetime] = None) -> dict:
    """``/api/indicators`` payload, grouped by mechanism (cached 2 min)."""
    use_cache = now is None
    if use_cache:
        cached = _payload_cache.get("payload")
        if cached is not None:
            return cached
    now = now or _utcnow()
    catalogue = series_catalogue()

    async def one(series_id: str, spec: dict) -> tuple[str, dict]:
        recent, history = await asyncio.gather(
            query_series(spec, now - timedelta(hours=RECENT_HOURS), now + timedelta(minutes=1)),
            daily_history(series_id, spec, now),
        )
        return series_id, _BUILDERS[spec["kind"]](series_id, spec, recent, history, now)

    entries = dict(await asyncio.gather(*(one(sid, spec) for sid, spec in catalogue.items())))

    def by(kind: str, runoff: Optional[bool] = None) -> list[dict]:
        out = []
        for sid, spec in catalogue.items():
            if spec["kind"] != kind:
                continue
            if runoff is not None and (spec["station"].get("role") in RUNOFF_ROLES) != runoff:
                continue
            out.append(entries[sid])
        return out

    payload = {
        "timestamp": utc_isoformat(now),
        "model_input": False,
        "note": "Indicateurs hors modele v3.4 (affichage, evaluation, futur v4)",
        "runoff": by("flow", runoff=True),
        "dordogne": by("flow", runoff=False),
        "groundwater": by("groundwater"),
        "tide_observed": by("tide_observed"),
    }
    if use_cache:
        _payload_cache.set("payload", payload)
    return payload


def status_specs() -> dict[str, dict]:
    """Informative ``/api/status.sources`` entries (never critical)."""
    specs = {}
    for sid, spec in series_catalogue().items():
        station = spec["station"]
        name = "indicator_" + sid.replace(":", "_").lower()
        specs[name] = {
            "label": f"Indicateur {spec['name']}",
            "measurement": spec["measurement"],
            "tags": {"key": [spec["key"]]},
            "field": spec["field"],
            "max_age_hours": float(station.get("max_age_hours", 3.0)),
            "critical": False,
        }
    return specs
