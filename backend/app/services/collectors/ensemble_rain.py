"""Ensemble basin rain (Open-Meteo Ensemble API) and exceedance probabilities.

Models (``ensemble_rain_models``): ICON-D2-EPS (20 members, ~2.5 days),
ICON-EU-EPS (40, ~5.5 days), ECMWF IFS ENS 0.25 deg (51, 15 days). Each
member's hourly rain is averaged over a few basin points (sub-basin
centroids of the BD TOPAGE polygon, weighted by their area), then summed
over 24 h and 72 h windows. For each window: quantiles and the probability
(share of members) above the v3.4 rain thresholds: 24 h > 25 / 40 / 60 mm and
72 h > 50 / 80 / 120 mm (``risk_core.get_model_params``, read only).

Open-Meteo hourly ``precipitation`` is the sum of the PRECEDING hour, so a
window (start, end] uses the values labelled start + 1 h ... end.

There is no ensemble archive upstream: the scheduler stores every run in
``ensemble_rain`` (tags ``model``, ``run``, ``window``, ``lead``), windows
anchored on the run initialisation (``meta.json``) so the skill can be
evaluated later. When ``meta.json`` is stale (DWD models at the time of
writing), the run is anchored on the collection hour (``run_init_known=0``).

Never read by the risk model (v3.4 scoring is unchanged).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

import httpx
import numpy as np

from app.config import get_settings
from app.services.cache import EXTERNAL_TIMEOUT_S, CircuitBreaker, describe_error, is_upstream_failure
from app.services.probabilistic_store import ENSEMBLE_MEASUREMENT

logger = logging.getLogger(__name__)

ENSEMBLE_API_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
META_URL = "https://ensemble-api.open-meteo.com/data/{meta}/static/meta.json"
ATTRIBUTION = (
    "Pluie d'ensemble : Open-Meteo Ensemble API (CC BY 4.0) - ECMWF IFS ENS, "
    "DWD ICON-EU-EPS et ICON-D2-EPS"
)
WINDOWS = (24, 72)
CACHE_TTL = timedelta(hours=3)

_breaker = CircuitBreaker("Open-Meteo Ensemble")
# model id -> {"times", "basin", "init", "init_known", "fetched_at"}
_runs: dict[str, dict] = {}
_last_stored_run: dict[str, str] = {}


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def rain_thresholds() -> dict[int, tuple[float, ...]]:
    """v3.4 thresholds per window length (hours), from the model parameters."""
    from app.services.risk_core import get_model_params

    params = get_model_params()
    return {24: tuple(params.rain_24h_thresholds), 72: tuple(params.rain_72h_thresholds)}


def threshold_key(value: float) -> str:
    return f"{value:g}"


def run_tag(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%HZ")


# ---------------------------------------------------------------------------
# Parsing and statistics (pure)
# ---------------------------------------------------------------------------

def parse_ensemble(payload) -> tuple[list[datetime], np.ndarray]:
    """Hours and cube (n_points, n_hours, n_members) of an Ensemble API answer.

    ``payload`` is one location (dict) or several (list). ``precipitation`` is
    the control member, ``precipitation_memberNN`` the others; null = NaN.
    """
    locations = payload if isinstance(payload, list) else [payload]
    times: list[datetime] = []
    cubes = []
    for loc in locations:
        hourly = (loc or {}).get("hourly") or {}
        loc_times = [datetime.fromisoformat(str(t)) for t in hourly.get("time") or []]
        if not times:
            times = loc_times
        elif loc_times != times:
            raise ValueError("ensemble locations have different time axes")
        keys = [k for k in hourly if k == "precipitation" or k.startswith("precipitation_member")]
        keys.sort(key=lambda k: (k != "precipitation", k))
        cols = []
        for key in keys:
            values = hourly.get(key) or []
            col = [np.nan if v is None else float(v) for v in values[: len(times)]]
            col += [np.nan] * (len(times) - len(col))
            cols.append(col)
        cubes.append(np.array(cols, dtype=float).T if cols else np.empty((len(times), 0)))
    if not cubes or not times:
        return [], np.empty((0, 0, 0))
    members = min(c.shape[1] for c in cubes)
    return times, np.stack([c[:, :members] for c in cubes])


def basin_average(cube: np.ndarray, weights: Sequence[float]) -> np.ndarray:
    """Weighted mean over the points -> (n_hours, n_members); NaN only if no point has a value."""
    w = np.asarray(weights, dtype=float).reshape(-1, 1, 1)
    if cube.shape[0] != w.shape[0]:
        raise ValueError("one weight per point is required")
    valid = ~np.isnan(cube)
    wsum = (w * valid).sum(axis=0)
    total = (np.where(valid, cube, 0.0) * w).sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(wsum > 0, total / np.where(wsum > 0, wsum, 1.0), np.nan)


def window_stats(
    times: list[datetime],
    basin: np.ndarray,
    start: datetime,
    hours: int,
    thresholds: Sequence[float],
) -> Optional[dict]:
    """Stats of the members' rain over (start, start + hours]; None if not covered.

    A member counts only when all its hours are present; the window needs at
    least half of the members.
    """
    end = start + timedelta(hours=hours)
    idx = [i for i, t in enumerate(times) if start < t <= end]
    if len(idx) != hours or basin.size == 0:
        return None
    sums = basin[idx, :]
    complete = ~np.isnan(sums).any(axis=0)
    n_members = basin.shape[1]
    if complete.sum() == 0 or complete.sum() < n_members / 2:
        return None
    totals = sums[:, complete].sum(axis=0)
    p10, p50, p90 = np.percentile(totals, [10, 50, 90])
    return {
        "start": start,
        "end": end,
        "p10": round(float(p10), 2),
        "p50": round(float(p50), 2),
        "p90": round(float(p90), 2),
        "mean": round(float(totals.mean()), 2),
        "max": round(float(totals.max()), 2),
        "members": int(complete.sum()),
        "prob": {threshold_key(th): round(float((totals > th).mean()), 3) for th in thresholds},
    }


def run_init_from_meta(meta: Optional[dict], now: datetime) -> Optional[datetime]:
    """Run init from ``meta.json`` when it is current (data ends in the future)."""
    try:
        init = datetime.fromtimestamp(int(meta["last_run_initialisation_time"]), tz=timezone.utc)
        data_end = datetime.fromtimestamp(int(meta["data_end_time"]), tz=timezone.utc)
    except (TypeError, KeyError, ValueError, OverflowError):
        return None
    init = init.replace(tzinfo=None)
    data_end = data_end.replace(tzinfo=None)
    if data_end <= now or not (now - timedelta(days=2) <= init <= now + timedelta(hours=1)):
        return None
    return init


def last_valid_time(times: list[datetime], basin: np.ndarray) -> Optional[datetime]:
    """Last hour where at least half of the members have a value."""
    if basin.size == 0:
        return None
    ok = (~np.isnan(basin)).sum(axis=1) >= basin.shape[1] / 2
    idx = np.nonzero(ok)[0]
    return times[int(idx[-1])] if idx.size else None


def run_points(
    model_id: str,
    times: list[datetime],
    basin: np.ndarray,
    init: datetime,
    init_known: bool,
    thresholds: dict[int, Sequence[float]],
) -> list[dict]:
    """``ensemble_rain`` points of one run: one per covered window + a run row.

    24 h windows end at init + 24, 48, ...; 72 h windows at init + 72, 96, ...
    """
    tag_run = run_tag(init)
    points = []
    for hours in WINDOWS:
        end = init + timedelta(hours=hours)
        while True:
            stats = window_stats(times, basin, end - timedelta(hours=hours), hours, thresholds[hours])
            if stats is None:
                break
            fields = {k: stats[k] for k in ("p10", "p50", "p90", "mean", "max", "members")}
            for key, prob in stats["prob"].items():
                fields[f"prob_gt_{key}"] = prob
            fields["run_init_known"] = 1 if init_known else 0
            points.append({
                "measurement": ENSEMBLE_MEASUREMENT,
                "tags": {
                    "model": model_id,
                    "run": tag_run,
                    "window": f"{hours}h",
                    "lead": str(int((end - init).total_seconds() // 3600)),
                },
                "fields": fields,
                "timestamp": end,
            })
            end += timedelta(hours=24)
    last = last_valid_time(times, basin)
    points.append({
        "measurement": ENSEMBLE_MEASUREMENT,
        "tags": {"model": model_id, "run": tag_run, "window": "run", "lead": "0"},
        "fields": {
            "members": int(basin.shape[1]) if basin.size else 0,
            "horizon_hours": round((last - init).total_seconds() / 3600, 1) if last else 0.0,
            "run_init_known": 1 if init_known else 0,
        },
        "timestamp": init,
    })
    return points


def horizons(
    runs: dict[str, dict],
    models: Sequence[dict],
    t0: datetime,
    thresholds: dict[int, Sequence[float]],
    max_days: int = 15,
) -> dict[str, list[dict]]:
    """Windows from ``t0`` (24 h: day k; 72 h: starting every 24 h) for each model.

    ``blend`` = mean of the models' probabilities over the window (models
    that cover it, equal weights).
    """
    out: dict[str, list[dict]] = {}
    for hours in WINDOWS:
        rows = []
        for k in range(max_days):
            start = t0 + timedelta(hours=24 * k)
            per_model = {}
            for m in models:
                run = runs.get(m["id"])
                if not run:
                    continue
                stats = window_stats(run["times"], run["basin"], start, hours, thresholds[hours])
                if stats is not None:
                    per_model[m["id"]] = {key: v for key, v in stats.items() if key not in ("start", "end")}
            if not per_model:
                continue
            blend = {
                threshold_key(th): round(
                    float(np.mean([s["prob"][threshold_key(th)] for s in per_model.values()])), 3)
                for th in thresholds[hours]
            }
            rows.append({
                "start": start,
                "end": start + timedelta(hours=hours),
                "lead_start_h": 24 * k,
                "lead_end_h": 24 * k + hours,
                "models": per_model,
                "blend": {"prob": blend, "models": list(per_model)},
            })
        out[f"{hours}h"] = rows
    return out


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

async def fetch_model(client: httpx.AsyncClient, model: dict) -> dict:
    settings = get_settings()
    points = settings.ensemble_rain_points
    resp = await client.get(
        ENSEMBLE_API_URL,
        params={
            "latitude": ",".join(str(p["lat"]) for p in points),
            "longitude": ",".join(str(p["lon"]) for p in points),
            "hourly": "precipitation",
            "models": model["id"],
            "past_days": 1,
            "forecast_days": model.get("forecast_days", 7),
            "timezone": "GMT",
        },
        timeout=EXTERNAL_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()


async def fetch_meta(client: httpx.AsyncClient, model: dict) -> Optional[dict]:
    if not model.get("meta"):
        return None
    try:
        resp = await client.get(META_URL.format(meta=model["meta"]), timeout=EXTERNAL_TIMEOUT_S)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.debug("Ensemble meta.json %s unavailable: %s", model["meta"], describe_error(e))
        return None


async def _load_model(client: httpx.AsyncClient, model: dict, now: datetime) -> dict:
    payload, meta = await asyncio.gather(fetch_model(client, model), fetch_meta(client, model))
    times, cube = parse_ensemble(payload)
    weights = [p.get("weight", 1.0) for p in get_settings().ensemble_rain_points]
    basin = basin_average(cube, weights) if cube.size else np.empty((0, 0))
    init = run_init_from_meta(meta, now)
    return {
        "times": times,
        "basin": basin,
        "init": init or now.replace(minute=0, second=0, microsecond=0),
        "init_known": init is not None,
        "fetched_at": now,
        "error": None,
    }


async def refresh_runs(models: Sequence[dict], now: datetime) -> dict[str, dict]:
    """Fetch every model (concurrently) into the in-process cache."""
    if not _breaker.allow():
        return {}
    loaded: dict[str, dict] = {}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        results = await asyncio.gather(
            *(_load_model(client, m, now) for m in models), return_exceptions=True,
        )
    failures = 0
    for model, result in zip(models, results):
        if isinstance(result, Exception):
            failures += 1
            if is_upstream_failure(result):
                _breaker.record_failure(result)
            else:
                logger.warning("Ensemble %s failed: %s", model["id"], describe_error(result))
            continue
        loaded[model["id"]] = result
        _runs[model["id"]] = result
    if loaded:
        _breaker.record_success()
    return loaded


def _model_info(model: dict, run: Optional[dict]) -> dict:
    info = {"id": model["id"], "label": model.get("label", model["id"]), "available": run is not None}
    if run:
        last = last_valid_time(run["times"], run["basin"])
        info.update({
            "members": int(run["basin"].shape[1]) if run["basin"].size else 0,
            "run_init": run["init"],
            "run_init_known": run["init_known"],
            "horizon_end": last,
            "fetched_at": run["fetched_at"],
        })
    return info


async def get_ensemble_rain() -> dict:
    """``rain`` block of ``/api/forecast/ensemble`` (cache-through, never stored)."""
    settings = get_settings()
    thresholds = rain_thresholds()
    base = {
        "available": False,
        "thresholds": {f"{h}h": list(thresholds[h]) for h in WINDOWS},
        "points": settings.ensemble_rain_points,
        "attribution": ATTRIBUTION,
    }
    if not settings.ensemble_rain_enabled:
        return {**base, "note": "desactive (ENSEMBLE_RAIN_ENABLED=false)", "models": [], "horizons": {}}
    now = _utcnow()
    models = settings.ensemble_rain_models
    stale = [m for m in models if not _runs.get(m["id"]) or now - _runs[m["id"]]["fetched_at"] >= CACHE_TTL]
    if stale:
        await refresh_runs(stale, now)
    runs = {m["id"]: _runs[m["id"]] for m in models if m["id"] in _runs}
    t0 = now.replace(minute=0, second=0, microsecond=0)
    hz = horizons(runs, models, t0, thresholds)
    return {
        **base,
        "available": any(hz.values()),
        "issued_at": t0,
        "models": [_model_info(m, runs.get(m["id"])) for m in models],
        "horizons": hz,
    }


async def collect_and_store_ensemble_rain() -> int:
    """Scheduler job: refresh every model and store each new run."""
    settings = get_settings()
    if not settings.ensemble_rain_enabled:
        return 0
    now = _utcnow()
    loaded = await refresh_runs(settings.ensemble_rain_models, now)
    if not loaded:
        return 0
    thresholds = rain_thresholds()
    points = []
    new_runs: dict[str, str] = {}
    for model_id, run in loaded.items():
        tag = run_tag(run["init"])
        if run["init_known"] and _last_stored_run.get(model_id) == tag:
            continue  # same run as last time: nothing new to store
        pts = run_points(model_id, run["times"], run["basin"], run["init"], run["init_known"], thresholds)
        if len(pts) > 1:
            points.extend(pts)
            new_runs[model_id] = tag
    if not points:
        return 0
    from app.services import influx_client

    written = await influx_client.write_points_batch(points)
    if written == len(points):
        _last_stored_run.update(new_runs)
    return written
