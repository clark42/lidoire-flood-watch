"""GloFAS v4 river discharge at the Lidoire cell (Open-Meteo Flood API).

The GloFAS 0.05 deg river network has a Lidoire channel: the cell
(44.875, -0.025) drains the Lidoire just above the Dordogne cell (module
about 1.5 m3/s, reanalysis max about 20 m3/s). It is a modelled discharge
proxy, there is no gauge on the Lidoire.

- Forecast: ``models=forecast_v4&ensemble=true``, 51 members (control + 50),
  daily, 30 days. Each day is turned into quantiles (p10/p50/p90, min/max,
  mean) and the probability that the members exceed the p95 / p99 of the
  cell's own consolidated reanalysis (climatology).
- Climatology: ``models=consolidated_v4`` daily history (1997 -> about a year
  ago), fetched once, percentiles stored as ``glofas_climatology``.
- Open-Meteo keeps no forecast archive: the scheduler stores every run in
  ``glofas_forecast`` (tags ``cell``, ``run`` = init date, ``lead`` = days)
  so the skill can be evaluated later. The run date comes from the dataset
  ``meta.json`` (``last_run_initialisation_time``).

Read by ``GET /api/forecast/ensemble`` and the timeline only; never by the
risk model.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx
import numpy as np

from app.config import get_settings
from app.services.cache import EXTERNAL_TIMEOUT_S, CircuitBreaker, describe_error, is_upstream_failure
from app.services.probabilistic_store import (
    GLOFAS_CELL,
    GLOFAS_CLIMATOLOGY_MEASUREMENT,
    GLOFAS_MEASUREMENT,
)

logger = logging.getLogger(__name__)

FLOOD_API_URL = "https://flood-api.open-meteo.com/v1/flood"
FLOOD_META_URL = "https://flood-api.open-meteo.com/data/glofas_forecast_v4/static/meta.json"
FORECAST_MODEL = "forecast_v4"
CLIMATOLOGY_MODEL = "consolidated_v4"
CLIMATOLOGY_START = date(1984, 1, 1)
HISTORY_TIMEOUT_S = 60.0
ATTRIBUTION = "Debits : Copernicus GloFAS v4 (CEMS) via Open-Meteo Flood API, CC BY 4.0"

# In-process cache of the latest summary (route reads are cache-through)
CACHE_TTL = timedelta(hours=3)
_latest: Optional[dict] = None
_latest_at: Optional[datetime] = None
_climatology: Optional[dict] = None
_climatology_stored = False

_breaker = CircuitBreaker("Open-Meteo Flood (GloFAS)")


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Parsing and statistics (pure)
# ---------------------------------------------------------------------------

def parse_flood_members(payload: dict) -> tuple[list[date], np.ndarray]:
    """Days and member matrix (n_days, n_members) of an ``ensemble=true`` answer.

    ``river_discharge`` is the control, ``river_discharge_memberNN`` the
    perturbed members; missing values are NaN.
    """
    daily = (payload or {}).get("daily") or {}
    times = daily.get("time") or []
    keys = [k for k in daily if k == "river_discharge" or k.startswith("river_discharge_member")]
    keys.sort(key=lambda k: (k != "river_discharge", k))
    if not times or not keys:
        return [], np.empty((0, 0))
    columns = []
    for key in keys:
        values = daily.get(key) or []
        col = [np.nan if v is None else float(v) for v in values[: len(times)]]
        col += [np.nan] * (len(times) - len(col))
        columns.append(col)
    days = [date.fromisoformat(str(t)[:10]) for t in times]
    return days, np.array(columns, dtype=float).T


def parse_flood_history(payload: dict) -> tuple[np.ndarray, Optional[date], Optional[date]]:
    """Non-null daily values of a single-model answer, with their first/last day."""
    daily = (payload or {}).get("daily") or {}
    pairs = [
        (str(t)[:10], float(v))
        for t, v in zip(daily.get("time") or [], daily.get("river_discharge") or [])
        if v is not None
    ]
    if not pairs:
        return np.empty(0), None, None
    return (
        np.array([v for _, v in pairs]),
        date.fromisoformat(pairs[0][0]),
        date.fromisoformat(pairs[-1][0]),
    )


def climatology_from_values(values: np.ndarray, first: Optional[date], last: Optional[date]) -> Optional[dict]:
    values = np.asarray(values, dtype=float)
    values = values[~np.isnan(values)]
    if values.size < 365:
        return None
    p50, p90, p95, p99 = np.percentile(values, [50, 90, 95, 99])
    return {
        "p50": round(float(p50), 3),
        "p90": round(float(p90), 3),
        "p95": round(float(p95), 3),
        "p99": round(float(p99), 3),
        "max": round(float(values.max()), 3),
        "n_days": int(values.size),
        "first_day": first.isoformat() if first else None,
        "last_day": last.isoformat() if last else None,
        "model": CLIMATOLOGY_MODEL,
    }


def summarize_members(row: np.ndarray, clim: Optional[dict]) -> Optional[dict]:
    """Quantiles and exceedance probabilities of one day (None if < half the members)."""
    row = np.asarray(row, dtype=float)
    valid = row[~np.isnan(row)]
    if valid.size == 0 or valid.size < row.size / 2:
        return None
    p10, p50, p90 = np.percentile(valid, [10, 50, 90])
    out = {
        "p10": round(float(p10), 3),
        "p50": round(float(p50), 3),
        "p90": round(float(p90), 3),
        "min": round(float(valid.min()), 3),
        "max": round(float(valid.max()), 3),
        "mean": round(float(valid.mean()), 3),
        "members": int(valid.size),
        "prob_gt_p95": None,
        "prob_gt_p99": None,
    }
    if clim:
        out["prob_gt_p95"] = round(float((valid > clim["p95"]).mean()), 3)
        out["prob_gt_p99"] = round(float((valid > clim["p99"]).mean()), 3)
    return out


def run_date_from_meta(meta: Optional[dict], now: datetime) -> Optional[date]:
    """Init date of the latest run from ``meta.json`` (None when absent or stale)."""
    try:
        init = datetime.fromtimestamp(int(meta["last_run_initialisation_time"]), tz=timezone.utc)
    except (TypeError, KeyError, ValueError, OverflowError):
        return None
    init = init.replace(tzinfo=None)
    if not (now - timedelta(days=3) <= init <= now + timedelta(hours=1)):
        return None
    return init.date()


def build_summary(
    days: list[date],
    members: np.ndarray,
    clim: Optional[dict],
    run_date: Optional[date],
    now: datetime,
) -> dict:
    """``glofas`` block of ``/api/forecast/ensemble`` (days with lead >= 0)."""
    settings = get_settings()
    run_known = run_date is not None
    if run_date is None:
        run_date = now.date()  # unknown init: anchored on the collection day
    out_days = []
    for day, row in zip(days, members):
        lead = (day - run_date).days
        if lead < 0:
            continue
        stats = summarize_members(row, clim)
        if stats is None:
            continue
        out_days.append({"date": day.isoformat(), "lead_days": lead, **stats})

    def peak(key: str, max_lead: int) -> Optional[dict]:
        cands = [d for d in out_days if d.get(key) is not None and d["lead_days"] <= max_lead]
        if not cands:
            return None
        best = max(cands, key=lambda d: (d[key], -d["lead_days"]))
        return {"date": best["date"], "value": best[key]}

    return {
        "available": bool(out_days),
        "cell": {"id": GLOFAS_CELL, "lat": settings.glofas_lat, "lon": settings.glofas_lon},
        "model": FORECAST_MODEL,
        "run": run_date.isoformat(),
        "run_known": run_known,
        "members": int(members.shape[1]) if members.size else 0,
        "climatology": clim,
        "days": out_days,
        "peak_p50": peak("p50", 30),
        "max_prob_gt_p95_10d": peak("prob_gt_p95", 10),
        "max_prob_gt_p99_10d": peak("prob_gt_p99", 10),
        "attribution": ATTRIBUTION,
    }


def make_glofas_points(summary: dict) -> list[dict]:
    """One ``glofas_forecast`` point per valid day of the run."""
    clim = summary.get("climatology") or {}
    points = []
    for day in summary.get("days", []):
        fields = {k: day.get(k) for k in ("p10", "p50", "p90", "min", "max", "mean",
                                          "prob_gt_p95", "prob_gt_p99", "members")}
        fields["clim_p95"] = clim.get("p95")
        fields["clim_p99"] = clim.get("p99")
        fields["run_known"] = 1 if summary.get("run_known") else 0
        points.append({
            "measurement": GLOFAS_MEASUREMENT,
            "tags": {"cell": GLOFAS_CELL, "run": summary["run"], "lead": str(day["lead_days"])},
            "fields": fields,
            "timestamp": datetime.fromisoformat(day["date"]),
        })
    return points


def make_climatology_point(clim: dict, now: datetime) -> dict:
    fields = {k: clim.get(k) for k in ("p50", "p90", "p95", "p99", "max", "n_days", "first_day", "last_day")}
    return {
        "measurement": GLOFAS_CLIMATOLOGY_MEASUREMENT,
        "tags": {"cell": GLOFAS_CELL, "model": CLIMATOLOGY_MODEL},
        "fields": fields,
        "timestamp": now.replace(minute=0, second=0, microsecond=0),
    }


def climatology_from_point(point: Optional[dict]) -> Optional[dict]:
    if not point or point.get("p95") is None or point.get("p99") is None:
        return None
    clim = {k: point.get(k) for k in ("p50", "p90", "p95", "p99", "max", "n_days", "first_day", "last_day")}
    clim["model"] = CLIMATOLOGY_MODEL
    if clim.get("n_days") is not None:
        clim["n_days"] = int(clim["n_days"])
    return clim


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

async def fetch_forecast(client: httpx.AsyncClient) -> dict:
    settings = get_settings()
    resp = await client.get(
        FLOOD_API_URL,
        params={
            "latitude": settings.glofas_lat,
            "longitude": settings.glofas_lon,
            "daily": "river_discharge",
            "models": FORECAST_MODEL,
            "ensemble": "true",
            "past_days": 1,
            "forecast_days": settings.glofas_forecast_days,
        },
        timeout=EXTERNAL_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()


async def fetch_meta(client: httpx.AsyncClient) -> Optional[dict]:
    try:
        resp = await client.get(FLOOD_META_URL, timeout=EXTERNAL_TIMEOUT_S)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.info("GloFAS meta.json unavailable: %s", describe_error(e))
        return None


async def fetch_history(client: httpx.AsyncClient, now: datetime) -> dict:
    settings = get_settings()
    resp = await client.get(
        FLOOD_API_URL,
        params={
            "latitude": settings.glofas_lat,
            "longitude": settings.glofas_lon,
            "daily": "river_discharge",
            "models": CLIMATOLOGY_MODEL,
            "start_date": CLIMATOLOGY_START.isoformat(),
            "end_date": now.date().isoformat(),
        },
        timeout=HISTORY_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()


async def get_climatology(client: httpx.AsyncClient, store: bool = False) -> Optional[dict]:
    """Climatology: memory, then the stored point, then the reanalysis (once).

    Only the scheduler passes ``store=True`` (routes never write); a
    climatology fetched by a route is written by the next job run.
    """
    global _climatology, _climatology_stored
    if _climatology is None:
        try:
            from app.services.probabilistic_store import query_glofas_climatology

            clim = climatology_from_point(await query_glofas_climatology())
        except Exception as e:
            logger.debug("GloFAS climatology not readable from InfluxDB: %s", describe_error(e))
            clim = None
        if clim is not None:
            _climatology, _climatology_stored = clim, True
        else:
            try:
                values, first, last = parse_flood_history(await fetch_history(client, _utcnow()))
            except Exception as e:
                logger.warning("GloFAS reanalysis unavailable: %s", describe_error(e))
                return None
            clim = climatology_from_values(values, first, last)
            if clim is None:
                return None
            _climatology, _climatology_stored = clim, False
    if store and not _climatology_stored:
        from app.services import influx_client

        if await influx_client.write_points_batch([make_climatology_point(_climatology, _utcnow())]):
            _climatology_stored = True
    return _climatology


async def _fetch_summary(store_climatology: bool) -> Optional[dict]:
    if not _breaker.allow():
        return None
    now = _utcnow()
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            payload = await fetch_forecast(client)
            meta = await fetch_meta(client)
            clim = await get_climatology(client, store=store_climatology)
        _breaker.record_success()
    except Exception as e:
        if is_upstream_failure(e):
            _breaker.record_failure(e)
        else:
            logger.warning("GloFAS forecast failed: %s", describe_error(e))
        return None
    days, members = parse_flood_members(payload)
    return build_summary(days, members, clim, run_date_from_meta(meta, now), now)


def _set_cache(summary: dict) -> None:
    global _latest, _latest_at
    _latest, _latest_at = summary, _utcnow()


async def get_glofas_forecast() -> dict:
    """Latest summary (cached ``CACHE_TTL``, fetched on a miss, never stored)."""
    settings = get_settings()
    if not settings.glofas_enabled:
        return {"available": False, "note": "desactive (GLOFAS_ENABLED=false)", "attribution": ATTRIBUTION}
    if _latest is not None and _latest_at and _utcnow() - _latest_at < CACHE_TTL:
        return _latest
    summary = await _fetch_summary(store_climatology=False)
    if summary is None:
        if _latest is not None:
            return {**_latest, "stale": True}
        return {"available": False, "note": "GloFAS indisponible", "attribution": ATTRIBUTION}
    _set_cache(summary)
    return summary


async def collect_and_store_glofas() -> int:
    """Scheduler job: fetch the run and store every day (``glofas_forecast``)."""
    if not get_settings().glofas_enabled:
        return 0
    summary = await _fetch_summary(store_climatology=True)
    if summary is None:
        return 0
    _set_cache(summary)
    if not summary["available"]:
        return 0
    from app.services import influx_client

    return await influx_client.write_points_batch(make_glofas_points(summary))
