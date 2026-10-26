"""Flood drivers at a time t: labelled episodes, percentiles, analogs, profile.

Feeds three display endpoints (never the risk model v3.4: guard test):

- ``GET /api/episodes/drivers``: for each labelled episode, the values at
  the peak hour read from the stored series, with explicit causal windows
  (points <= t), and their percentile in the stored history;
- ``GET /api/risk/analogs``: the same values at any t, and the labelled
  episodes nearest on the percentile axes (prototype ``dist``);
- ``GET /api/geo/profile``: confluence stage Z_c and backwater reach on the
  Lidoire long profile (``services/lidoire_profile.py``).

Reads InfluxDB only (no external API). Values and windows:

| key | series | window |
|-----|--------|--------|
| ``pessac_m`` | ``river_level`` Pessac ``height_m`` | max over (t - 12.5 h, t] |
| ``libourne_m`` | ``river_level`` Libourne ``height_m`` | max over (t - 12.5 h, t] |
| ``eyraud_q_m3s`` | ``river_flow`` EYRAUD ``q_m3s`` | max over (t - 12.5 h, t] |
| ``rain_72h_mm`` | ``rainfall`` open-meteo / Moulin-Lidoire ``precipitation_mm_h`` | sum over (t - 72 h, t] |
| ``piezo_m_ngf`` | ``groundwater`` MOULIETS ``level_m_ngf`` | last point <= t, 48 h max age |
| ``glofas_m3s`` | ``glofas_forecast`` p50, newest run for the day of t | day of t |
| ``z_c_m_ngf`` | Pessac and Libourne envelopes, NGF zeros, alpha | ESTIMATED |

Percentiles (``climatology.percentile_of``, mid-rank) against the stored
history since 2022-11: Pessac and Eyraud daily maxima, 72 h rain sums on
each day of October-April (flood season, as in the prototype), piezometer
daily means, GloFAS daily values. The 12.5 h maxima are compared with daily
maxima (slightly pessimistic percentiles).
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

from app.config import get_settings
from app.models.schemas import utc_isoformat
from app.services import climatology
from app.services.cache import TTLCache
from app.services.lidoire_profile import (
    APPROX_LABEL,
    ESTIMATE_LABEL,
    backwater_reach,
    confluence_stage,
    envelope_max,
    latest_value,
    load_profile,
    reach_relative_to_mill,
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
    validate_source,
    validate_station_ids,
    validate_tag_value,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


HISTORY_START = datetime(2022, 11, 1)
DRIVERS_CACHE_TTL_S = 6 * 3600
HISTORY_CACHE_TTL_S = 6 * 3600
WET_SEASON_MONTHS = frozenset({10, 11, 12, 1, 2, 3, 4})
MAX_CONCURRENT_EPISODES = 4

AXES = ("pessac", "eyraud_q", "rain_72h", "piezo", "glofas")
VALUE_KEYS = {
    "pessac": "pessac_m",
    "eyraud_q": "eyraud_q_m3s",
    "rain_72h": "rain_72h_mm",
    "piezo": "piezo_m_ngf",
    "glofas": "glofas_m3s",
}

_history_cache = TTLCache(ttl_seconds=HISTORY_CACHE_TTL_S, max_entries=4)
_drivers_cache = TTLCache(ttl_seconds=DRIVERS_CACHE_TTL_S, max_entries=2)


# ---------------------------------------------------------------------------
# Flux (pure builders) and reads
# ---------------------------------------------------------------------------


def build_points_query(
    measurement: str,
    field: str,
    tags: dict[str, str],
    start: datetime,
    end: datetime,
    every: Optional[str] = None,
    fn: str = "max",
) -> str:
    """Points of one field (all series of the tags merged), raw or aggregated."""
    measurement = validate_identifier(measurement, "measurement")
    field = validate_identifier(field, "field")
    filters = "".join(
        f"\n        |> filter(fn: (r) => r.{validate_identifier(k, 'tag key')} == "
        f"{flux_string(validate_tag_value(v, 'tag value'))})"
        for k, v in tags.items()
    )
    window = ""
    if every is not None:
        validate_resolution(every)
        validate_agg_fn(fn)
        window = f"|> aggregateWindow(every: {every}, fn: {fn}, createEmpty: false)"
    return f'''
    from(bucket: {_bucket()})
        |> range(start: {_flux_ts(start)}, stop: {_flux_ts(end)})
        |> filter(fn: (r) => r._measurement == {flux_string(measurement)})
        |> filter(fn: (r) => r._field == {flux_string(field)}){filters}
        |> keep(columns: ["_time", "_value", "_measurement", "_field"])
        |> group(columns: ["_measurement", "_field"])
        {window}
        |> sort(columns: ["_time"])
    '''


async def query_points(measurement: str, field: str, tags: dict[str, str], start: datetime,
                       end: datetime, every: Optional[str] = None, fn: str = "max") -> list[tuple[datetime, float]]:
    """``[(naive UTC time, value)]`` sorted. Errors propagate (never cached as "no data")."""
    tables = await _run_query(build_points_query(measurement, field, tags, start, end, every, fn))
    out = [
        (to_naive_utc(r.get_time()), float(r.get_value()))
        for table in tables for r in table.records
        if r.get_value() is not None and r.get_time() is not None
    ]
    out.sort(key=lambda p: p[0])
    return out


def series_specs() -> dict[str, tuple[str, str, dict[str, str]]]:
    """name -> (measurement, field, tags) from the config (validated)."""
    from app.services.collectors.hydro_indicators import FLOW_MEASUREMENT, GROUNDWATER_MEASUREMENT

    s = get_settings()
    up, down = validate_station_ids([s.confluence_upstream_station, s.confluence_downstream_station])
    return {
        "pessac": ("river_level", "height_m", {"station_id": up}),
        "libourne": ("river_level", "height_m", {"station_id": down}),
        "eyraud_q": (FLOW_MEASUREMENT, "q_m3s", {"key": s.drivers_flow_key}),
        "rain": ("rainfall", "precipitation_mm_h",
                 {"source": validate_source("open-meteo"), "city": s.drivers_rain_station_city}),
        "piezo": (GROUNDWATER_MEASUREMENT, "level_m_ngf", {"key": s.drivers_groundwater_key}),
    }


async def fetch_window(t: datetime, series: Iterable[str] = ("pessac", "libourne", "eyraud_q", "rain", "piezo"),
                       ) -> tuple[dict[str, list[tuple[datetime, float]]], list[str]]:
    """Raw points of each series over the longest window before t (and errors)."""
    s = get_settings()
    hours = max(s.drivers_rain_hours, s.drivers_piezo_max_age_hours,
                s.drivers_envelope_hours, s.confluence_envelope_hours)
    start, end = t - timedelta(hours=hours), t + timedelta(seconds=1)
    specs = series_specs()
    names = list(series)
    results = await asyncio.gather(
        *(query_points(*specs[n], start, end) for n in names), return_exceptions=True,
    )
    out: dict[str, list[tuple[datetime, float]]] = {}
    errors: list[str] = []
    for name, res in zip(names, results):
        if isinstance(res, BaseException):
            logger.warning("Drivers: %s query failed at %s: %r", name, t.isoformat(), res)
            errors.append(name)
            out[name] = []
        else:
            out[name] = res
    return out, errors


async def _glofas_daily(start: datetime, end: datetime) -> dict:
    """Valid day -> p50 of the newest stored run ({} when nothing is stored)."""
    from app.services.probabilistic_store import latest_run_per_day, query_glofas_rows

    rows = await query_glofas_rows(start, end, fields=("p50",))
    return {p["timestamp"].date(): float(p["value"]) for p in latest_run_per_day(rows, "p50")}


def rolling_sums(daily: Sequence[tuple[datetime, float]], days: int = 3,
                 months: Optional[frozenset] = WET_SEASON_MONTHS) -> list[float]:
    """Sums of ``days`` consecutive calendar days (complete windows only)."""
    by_day = {ts.date(): v for ts, v in daily}
    out = []
    for day in sorted(by_day):
        if months is not None and day.month not in months:
            continue
        window = [by_day.get(day - timedelta(days=k)) for k in range(days)]
        if any(v is None for v in window):
            continue
        out.append(sum(window))
    return out


async def load_history(now: Optional[datetime] = None) -> tuple[dict[str, list[float]], list[str]]:
    """Stored history per axis (cached 6 h, not cached when a query failed)."""
    cached = _history_cache.get("history")
    if cached is not None:
        return cached, []
    now = now or _utcnow()
    specs = series_specs()
    jobs = {
        "pessac": query_points(*specs["pessac"], HISTORY_START, now, every="1d", fn="max"),
        "eyraud_q": query_points(*specs["eyraud_q"], HISTORY_START, now, every="1d", fn="max"),
        "rain": query_points(*specs["rain"], HISTORY_START, now, every="1d", fn="sum"),
        "piezo": query_points(*specs["piezo"], HISTORY_START, now, every="1d", fn="mean"),
        "glofas": _glofas_daily(HISTORY_START, now + timedelta(days=1)),
    }
    results = await asyncio.gather(*jobs.values(), return_exceptions=True)
    history: dict[str, list[float]] = {}
    errors: list[str] = []
    for name, res in zip(jobs, results):
        if isinstance(res, BaseException):
            logger.warning("Drivers history: %s query failed: %r", name, res)
            errors.append(name)
            res = {} if name == "glofas" else []
        if name == "rain":
            history["rain_72h"] = rolling_sums(res, days=3)
        elif name == "glofas":
            history["glofas"] = list(res.values())
        else:
            history[name] = [v for _, v in res]
    if not errors:
        _history_cache.set("history", history)
    return history, errors


# ---------------------------------------------------------------------------
# Values at t (pure)
# ---------------------------------------------------------------------------


def rain_sum(points: Sequence[tuple[datetime, float]], t: datetime, hours: float) -> dict:
    """Sum of the hourly points in (t - hours, t], with the number of hours found."""
    lo = t - timedelta(hours=hours)
    vals = [v for ts, v in points if lo < ts <= t]
    expected = int(round(hours))
    return {
        "mm": round(sum(vals), 2) if vals else None,
        "hours_found": len(vals),
        "hours_expected": expected,
        "complete": len(vals) >= expected,
    }


def _r(v: Optional[float], n: int = 3) -> Optional[float]:
    return None if v is None else round(v, n)


def values_at(t: datetime, window: dict[str, list[tuple[datetime, float]]],
              glofas_by_day: Optional[dict] = None) -> dict:
    """Driver values at ``t`` from raw points (points after t are ignored)."""
    s = get_settings()
    env_h = s.drivers_envelope_hours
    pessac = envelope_max(window.get("pessac", []), t, env_h)
    libourne = envelope_max(window.get("libourne", []), t, s.confluence_envelope_hours)
    pessac_c = envelope_max(window.get("pessac", []), t, s.confluence_envelope_hours)
    eyraud = envelope_max(window.get("eyraud_q", []), t, env_h)
    rain = rain_sum(window.get("rain", []), t, s.drivers_rain_hours)
    piezo = latest_value(window.get("piezo", []), t, s.drivers_piezo_max_age_hours)
    zeros = s.ngf_station_zeros_m
    z_c, notes = confluence_stage(
        pessac_c, libourne,
        zeros[s.confluence_upstream_station], zeros[s.confluence_downstream_station],
        s.confluence_downstream_weight,
    )
    if rain["mm"] is not None and not rain["complete"]:
        notes.append(f"pluie 72 h incomplete ({rain['hours_found']}/{rain['hours_expected']} h)")
    glofas = (glofas_by_day or {}).get(t.date())
    return {
        "pessac_m": _r(pessac),
        "libourne_m": _r(libourne),
        "eyraud_q_m3s": _r(eyraud),
        "rain_72h_mm": _r(rain["mm"], 1),
        "rain_72h_hours_found": rain["hours_found"],
        "piezo_m_ngf": _r(piezo[1] if piezo else None),
        "piezo_time": utc_isoformat(piezo[0]) if piezo else None,
        "glofas_m3s": _r(glofas),
        "z_c_m_ngf": _r(z_c),
        "notes": notes,
    }


def percentiles_of(values: dict, history: dict[str, list[float]]) -> dict[str, Optional[float]]:
    return {axis: climatology.percentile_of(values.get(VALUE_KEYS[axis]), history.get(axis, []))
            for axis in AXES}


def analog_distance(a: dict, b: dict, axes: Sequence[str], min_axes: int) -> tuple[Optional[float], list[str]]:
    """RMS of the percentile differences on the axes both sides have (prototype ``dist``)."""
    used = [ax for ax in axes if a.get(ax) is not None and b.get(ax) is not None]
    if len(used) < max(1, min_axes):
        return None, used
    acc = sum((a[ax] - b[ax]) ** 2 for ax in used)
    return round(math.sqrt(acc / len(used)), 1), used


def windows_doc() -> dict[str, str]:
    s = get_settings()
    return {
        "pessac_m": f"max de H Pessac sur (t - {s.drivers_envelope_hours:g} h, t]",
        "libourne_m": f"max de H Libourne sur (t - {s.confluence_envelope_hours:g} h, t]",
        "eyraud_q_m3s": f"max du debit de l'Eyraud sur (t - {s.drivers_envelope_hours:g} h, t]",
        "rain_72h_mm": f"somme de la pluie horaire Open-Meteo au moulin sur (t - {s.drivers_rain_hours:g} h, t]",
        "piezo_m_ngf": f"dernier niveau de la nappe (Mouliets) <= t, au plus {s.drivers_piezo_max_age_hours:g} h",
        "glofas_m3s": "GloFAS p50, dernier run stocke pour le jour de t (souvent absent avant la collecte)",
        "z_c_m_ngf": (
            f"estime : (1 - {s.confluence_downstream_weight:g}) x Pessac NGF + {s.confluence_downstream_weight:g} x "
            f"Libourne NGF, max sur {s.confluence_envelope_hours:g} h"
        ),
    }


def percentile_basis() -> dict[str, str]:
    return {
        "pessac": "max 12.5 h vs max journaliers de Pessac depuis 2022-11",
        "eyraud_q": "max 12.5 h vs max journaliers de l'Eyraud depuis 2022-11",
        "rain_72h": "cumul 72 h vs cumuls de 3 jours (octobre-avril) depuis 2022-11",
        "piezo": "niveau vs moyennes journalieres depuis 2022-11",
        "glofas": "p50 vs valeurs journalieres GloFAS stockees",
    }


def confluence_config() -> dict:
    s = get_settings()
    return {
        "label": ESTIMATE_LABEL,
        "upstream_station": s.confluence_upstream_station,
        "downstream_station": s.confluence_downstream_station,
        "ngf_zeros_m": dict(s.ngf_station_zeros_m),
        "alpha_downstream_weight": s.confluence_downstream_weight,
        "envelope_hours": s.confluence_envelope_hours,
        "formula": "Z_c = (1 - alpha) x (zero_Pessac + H_Pessac) + alpha x (zero_Libourne + H_Libourne)",
    }


# ---------------------------------------------------------------------------
# /api/episodes/drivers
# ---------------------------------------------------------------------------


async def get_episode_drivers(use_cache: bool = True) -> dict:
    """Values at each labelled peak, with percentiles (cached 6 h when complete)."""
    if use_cache:
        cached = _drivers_cache.get("drivers")
        if cached is not None:
            return cached
    from app.services.labels import load_label_set

    label_set = load_label_set()
    now = _utcnow()
    history, hist_errors = await load_history(now)
    try:
        glofas = await _glofas_daily(HISTORY_START, now + timedelta(days=1))
    except Exception as e:
        logger.warning("Drivers: GloFAS query failed: %r", e)
        glofas, hist_errors = {}, hist_errors + ["glofas"]

    sem = asyncio.Semaphore(MAX_CONCURRENT_EPISODES)

    async def one(label):
        t = to_naive_utc(label.peak_time)
        async with sem:
            window, errors = await fetch_window(t)
        values = values_at(t, window, glofas)
        return {
            "key": label.key,
            "peak_time": utc_isoformat(t),
            "outcome": label.outcome.value,
            "severity": label.severity,
            "water_height_cm": label.water_height_cm,
            "notes": label.notes,
            "flags": list(label.flags),
            "values": values,
            "percentiles": percentiles_of(values, history),
            "errors": errors,
        }

    episodes = await asyncio.gather(*(one(lb) for lb in label_set.episodes))
    partial = bool(hist_errors) or any(e["errors"] for e in episodes)
    payload = {
        "generated_at": utc_isoformat(now),
        "model_input": False,
        "labels_version": label_set.version,
        "labels_updated_at": label_set.updated_at,
        "windows": windows_doc(),
        "percentile_basis": percentile_basis(),
        "history_counts": {axis: len(history.get(axis, [])) for axis in AXES},
        "confluence": confluence_config(),
        "caveats": [
            "peak_time des labels a la journee pres (06:00 ou 12:00 le plus souvent) : Pessac et l'Eyraud "
            "ne culminent pas forcement a cette heure",
            "fenetres causales : seuls les points <= t sont lus",
            "Z_c est une estimation (aucune station a la confluence)",
        ],
        "partial": partial,
        "errors": sorted(set(hist_errors)),
        "episodes": list(episodes),
    }
    if use_cache and not partial:
        _drivers_cache.set("drivers", payload)
    return payload


# ---------------------------------------------------------------------------
# /api/risk/analogs
# ---------------------------------------------------------------------------


async def situation_at(t: datetime) -> dict:
    history, hist_errors = await load_history()
    window, errors = await fetch_window(t)
    try:
        glofas = await _glofas_daily(t - timedelta(days=1), t + timedelta(days=1))
    except Exception as e:
        logger.warning("Analogs: GloFAS query failed: %r", e)
        glofas, errors = {}, errors + ["glofas"]
    values = values_at(t, window, glofas)
    return {
        "timestamp": utc_isoformat(t),
        "values": values,
        "percentiles": percentiles_of(values, history),
        "errors": sorted(set(errors + hist_errors)),
    }


async def observation_counts(peaks: dict[str, datetime], margin_hours: float) -> Optional[dict[str, dict]]:
    """Observations (and those with a photo) within peak +/- margin, per episode key."""
    if not peaks:
        return {}
    from app.services.observations import list_observations

    margin = timedelta(hours=margin_hours)
    try:
        obs = await list_observations(start=min(peaks.values()) - margin, end=max(peaks.values()) + margin,
                                      limit=5000)
    except Exception as e:
        logger.warning("Analogs: observations unavailable: %r", e)
        return None
    out = {}
    for key, peak in peaks.items():
        inside = [o for o in obs if peak - margin <= to_naive_utc(o.observed_at) <= peak + margin]
        out[key] = {"count": len(inside), "with_photo": sum(1 for o in inside if o.has_photo)}
    return out


def rank_analogs(situation_pct: dict, episodes: list[dict], k: int, axes: Sequence[str],
                 min_axes: int, threshold: float) -> dict:
    """k nearest labelled episodes and the "nothing close" verdict (pure)."""
    ranked = []
    for ep in episodes:
        dist, used = analog_distance(situation_pct, ep["percentiles"], axes, min_axes)
        ranked.append((dist, ep, used))
    ranked.sort(key=lambda x: (x[0] is None, x[0] if x[0] is not None else 0.0, x[1]["key"]))
    top = ranked[:k]
    nearest = top[0][0] if top else None
    nothing_close = nearest is None or nearest > threshold
    return {"top": top, "nearest_distance": nearest, "nothing_close": nothing_close}


async def get_analogs(t: datetime, k: int = 3) -> dict:
    s = get_settings()
    axes = [ax for ax in s.analog_axes if ax in AXES]
    drivers = await get_episode_drivers()  # fills the history cache first
    situation = await situation_at(t)
    ranking = rank_analogs(situation["percentiles"], drivers["episodes"], k, axes,
                           s.analog_min_axes, s.analog_distance_threshold)
    peaks = {ep["key"]: to_naive_utc(datetime.fromisoformat(ep["peak_time"].replace("Z", "+00:00")))
             for _, ep, _ in ranking["top"]}
    counts: Optional[dict] = {}
    if peaks:
        counts = await observation_counts(peaks, s.analog_observation_margin_hours)

    analogs = []
    for dist, ep, used in ranking["top"]:
        analogs.append({
            "key": ep["key"],
            "peak_time": ep["peak_time"],
            "outcome": ep["outcome"],
            "severity": ep["severity"],
            "water_height_cm": ep["water_height_cm"],
            "notes": ep["notes"],
            "distance": dist,
            "close": dist is not None and dist <= s.analog_distance_threshold,
            "axes_used": used,
            "percentiles": {ax: ep["percentiles"].get(ax) for ax in axes},
            "percentile_diff": {
                ax: (round(ep["percentiles"][ax] - situation["percentiles"][ax], 1)
                     if ax in used else None)
                for ax in axes
            },
            "values": ep["values"],
            "observations": counts.get(ep["key"]) if counts is not None else None,
        })
    nearest = ranking["nearest_distance"]
    if nearest is None:
        message = "Distance non calculable : trop peu d'axes disponibles a cette date."
    elif ranking["nothing_close"]:
        message = (f"Aucun episode labellise ne ressemble a cette situation : le plus proche est a "
                   f"{nearest:.0f} points de percentile (seuil {s.analog_distance_threshold:g}).")
    else:
        message = f"Episode labellise le plus proche a {nearest:.0f} points de percentile."
    return {
        "timestamp": situation["timestamp"],
        "model_input": False,
        "k": k,
        "axes": axes,
        "method": (
            "distance = racine de la moyenne des carres des ecarts de percentile sur les axes disponibles "
            f"des deux cotes (au moins {s.analog_min_axes}) ; 0 = identique, 100 = opposes"
        ),
        "threshold": s.analog_distance_threshold,
        "nothing_close": ranking["nothing_close"],
        "nearest_distance": nearest,
        "message": message,
        "situation": situation,
        "windows": windows_doc(),
        "percentile_basis": percentile_basis(),
        "observation_window_hours": s.analog_observation_margin_hours,
        "observations_available": counts is not None,
        "labels_partial": drivers["partial"],
        "analogs": analogs,
    }


# ---------------------------------------------------------------------------
# /api/geo/profile
# ---------------------------------------------------------------------------


async def get_profile_at(t: datetime, include_points: bool = True) -> dict:
    """Lidoire long profile, Z_c at t (estimated) and backwater reach (approximation)."""
    s = get_settings()
    profile = load_profile()
    meta = profile["metadata"]
    window, errors = await fetch_window(t, series=("pessac", "libourne"))
    zeros = s.ngf_station_zeros_m
    z0_up = zeros[s.confluence_upstream_station]
    z0_down = zeros[s.confluence_downstream_station]
    alpha = s.confluence_downstream_weight
    env_h = s.confluence_envelope_hours

    pessac_env = envelope_max(window["pessac"], t, env_h)
    lib_env = envelope_max(window["libourne"], t, env_h)
    z_env, notes = confluence_stage(pessac_env, lib_env, z0_up, z0_down, alpha)
    max_age = s.risk_level_max_age_hours
    p_now = latest_value(window["pessac"], t, max_age)
    l_now = latest_value(window["libourne"], t, max_age)
    z_now, notes_now = confluence_stage(p_now[1] if p_now else None, l_now[1] if l_now else None,
                                        z0_up, z0_down, alpha)

    points = profile["points"]
    mill = meta["mill"]
    reach = backwater_reach(points, z_env)
    reach.update(reach_relative_to_mill(reach, mill["d_m"]))
    reach_now = backwater_reach(points, z_now)
    reach_now.update(reach_relative_to_mill(reach_now, mill["d_m"]))
    ground = s.mill_ground_z_m_ngf

    def _ngf(h, z0):
        return None if h is None else round(z0 + h, 3)

    return {
        "at": utc_isoformat(t),
        "model_input": False,
        "profile": {
            "metadata": meta,
            "length_m": points[-1]["d_m"] if points else None,
            "z_range_m": [min(p["z_m"] for p in points), max(p["z_m"] for p in points)] if points else None,
            "points": points if include_points else None,
        },
        "mill": {
            "lon": mill["lon"], "lat": mill["lat"], "d_m": mill["d_m"],
            "ground_z_m_ngf": ground,
            "ground_z_source": s.mill_ground_z_source,
            "floor_z_m_ngf": None,
        },
        "confluence": {
            **confluence_config(),
            "lon": meta["confluence"]["lon"], "lat": meta["confluence"]["lat"], "d_m": 0.0,
            "ground_z_m": meta["confluence"]["z_m"],
            "z_c_m_ngf": z_env,
            "inputs": {
                "pessac_max_m": _r(pessac_env), "pessac_max_m_ngf": _ngf(pessac_env, z0_up),
                "libourne_max_m": _r(lib_env), "libourne_max_m_ngf": _ngf(lib_env, z0_down),
            },
            "instant": {
                "z_c_m_ngf": z_now,
                "pessac_m": _r(p_now[1]) if p_now else None,
                "pessac_time": utc_isoformat(p_now[0]) if p_now else None,
                "libourne_m": _r(l_now[1]) if l_now else None,
                "libourne_time": utc_isoformat(l_now[0]) if l_now else None,
                "notes": notes_now,
            },
            "notes": notes,
        },
        "backwater": {
            **reach,
            "method": (
                "plan d'eau horizontal a Z_c depuis la confluence : distance_m = point le plus en amont du "
                "profil dont le terrain est <= Z_c (interpole jusqu'a l'echantillon suivant, borne haute) ; "
                "connected_distance_m = s'arrete au premier point du profil au-dessus de Z_c en remontant"
            ),
            "limits": [
                "ignore le debit propre de la Lidoire, la pente de la ligne d'eau, les ponts et les seuils",
                "z du profil = point bas du terrain sur une section de +/- 30 m (pas le fond du lit)",
                "Z_c est estime (aucune station a la confluence), enveloppe 12.5 h = pleine mer du cycle",
            ],
            "instant": reach_now,
        },
        "margin": {
            "mill_ground_minus_z_c_m": round(ground - z_env, 2) if z_env is not None else None,
            "label": ESTIMATE_LABEL,
            "note": "terrain au moulin moins Z_c ; ce n'est pas la marge au plancher (inconnu)",
        },
        "labels": {"z_c": ESTIMATE_LABEL, "backwater": APPROX_LABEL},
        "errors": errors,
    }
