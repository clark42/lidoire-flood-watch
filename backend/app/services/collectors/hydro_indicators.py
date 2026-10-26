"""Hydrological indicator collectors (display / evaluation / future v4 only).

Three new InfluxDB measurements, none of them read by the risk model v3.4:

- ``river_flow`` (tags ``key``, ``station_id``, ``river``, ``source``; fields
  ``q_m3s``, ``h_m``): discharge of the analog tributaries (Eyraud, Caudeau)
  and of the Dordogne (Pessac, Lamonzie, Argentat). Hub'Eau observations_tr
  (Q in l/s, H in mm, 5 min) for the realtime, Hydroportail for history
  (Hub'Eau obs_elab QmnJ, daily mean, as a fallback).
- ``groundwater`` (tags ``key``, ``code_bss``, ``source``; fields
  ``level_m_ngf``, ``depth_m``): alluvial piezometer, Hub'Eau niveaux_nappes
  ``chroniques_tr`` (hourly, ~12 h latency) and ``chroniques`` (daily).
- ``tide_observed`` (tags ``key``, ``source``; field ``height_m``): observed
  estuary water level (tide + surge + river flow). SHOM REFMAR observations
  (keyless, 1 min, resampled to 10 min means) and Hub'Eau (Bec d'Ambes).

Stations come from ``settings.indicator_*`` (never ``hubeau_stations``).
Collection runs from the scheduler only: no route calls these functions.
Realtime calls use EXTERNAL_TIMEOUT_S and one CircuitBreaker per upstream.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from influxdb_client import Point, WritePrecision

from app.config import get_settings
from app.services import influx_client
from app.services.cache import EXTERNAL_TIMEOUT_S, CircuitBreaker, describe_error, is_upstream_failure
from app.services.collectors.hubeau import HUBEAU_API_BASE, HYDROPORTAIL_BASE

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


FLOW_MEASUREMENT = "river_flow"
GROUNDWATER_MEASUREMENT = "groundwater"
TIDE_OBSERVED_MEASUREMENT = "tide_observed"

NAPPES_API_BASE = "https://hubeau.eaufrance.fr/api/v1/niveaux_nappes"
SHOM_OBSERVATION_URL = "https://services.data.shom.fr/maregraphie/observation/json/{shom_id}"
# SHOM source 1 = raw realtime (1 min); kept for every year since 2010 at Bordeaux
SHOM_SOURCE = 1
# SHOM 1-min samples are averaged into buckets of this size before storage
SHOM_RESAMPLE_MINUTES = 10

# Realtime windows (re-reads are idempotent: same timestamp and tags)
FLOW_REALTIME_HOURS = 24        # Eyraud/Caudeau publish with ~7 h latency
GROUNDWATER_REALTIME_DAYS = 3   # piezometer realtime ~12 h latency
TIDE_REALTIME_HOURS = 6         # SHOM ~40 min, Hub'Eau ~15 min latency

HISTORY_TIMEOUT_S = 60.0
BACKFILL_BATCH_DAYS = 30
MAX_CONCURRENT_REQUESTS = 4
HUBEAU_PAGE_SIZE = 20000

SOURCE_HUBEAU_TR = "hubeau-tr"
SOURCE_HYDROPORTAIL = "hydroportail"
SOURCE_HUBEAU_ELAB = "hubeau-elab-qmnj"
SOURCE_ADES_TR = "ades-tr"
SOURCE_ADES_DAILY = "ades-daily"
SOURCE_SHOM = "shom-refmar"

# Hydroportail requires browser-like headers (same as collectors/hubeau.py)
_HYDROPORTAIL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}

_hubeau_breaker = CircuitBreaker("Hub'Eau indicators")
_nappes_breaker = CircuitBreaker("Hub'Eau niveaux_nappes")
_shom_breaker = CircuitBreaker("SHOM REFMAR")


# ---------------------------------------------------------------------------
# Parsing (pure)
# ---------------------------------------------------------------------------


def _naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _iso(value: str) -> datetime:
    return _naive_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def parse_hubeau_tr(payload: dict, scale: float = 0.001) -> list[tuple[datetime, float]]:
    """Hub'Eau hydrometrie observations_tr -> sorted [(naive UTC, value x scale)].

    Q is in l/s and H in mm: the default scale gives m3/s and m.
    """
    out: dict[datetime, float] = {}
    for obs in (payload or {}).get("data", []) or []:
        try:
            value, ts = obs.get("resultat_obs"), obs.get("date_obs")
            if value is None or not ts:
                continue
            out[_iso(ts)] = float(value) * scale
        except (TypeError, ValueError) as e:
            logger.debug("Skipping Hub'Eau observation %r: %s", obs, e)
    return sorted(out.items())


def parse_hydroportail(payload: dict, scale: float = 0.001) -> list[tuple[datetime, float]]:
    """Hydroportail ``series.data`` ({"t", "v"}, l/s or mm) -> sorted pairs."""
    out: dict[datetime, float] = {}
    series = (payload or {}).get("series") or {}
    for obs in series.get("data", []) or []:
        try:
            value, ts = obs.get("v"), obs.get("t")
            if value is None or not ts:
                continue
            out[_iso(ts)] = float(value) * scale
        except (TypeError, ValueError) as e:
            logger.debug("Skipping Hydroportail observation %r: %s", obs, e)
    return sorted(out.items())


def parse_obs_elab(payload: dict, scale: float = 0.001) -> list[tuple[datetime, float]]:
    """Hub'Eau obs_elab (date_obs_elab 'YYYY-MM-DD') -> [(day 00:00 UTC, value)]."""
    out: dict[datetime, float] = {}
    for obs in (payload or {}).get("data", []) or []:
        try:
            value, day = obs.get("resultat_obs_elab"), obs.get("date_obs_elab")
            if value is None or not day:
                continue
            out[datetime.strptime(day[:10], "%Y-%m-%d")] = float(value) * scale
        except (TypeError, ValueError) as e:
            logger.debug("Skipping obs_elab value %r: %s", obs, e)
    return sorted(out.items())


def parse_piezo_tr(payload: dict) -> list[tuple[datetime, float, Optional[float]]]:
    """niveaux_nappes chroniques_tr -> sorted [(naive UTC, level m NGF, depth m)]."""
    out: dict[datetime, tuple[float, Optional[float]]] = {}
    for obs in (payload or {}).get("data", []) or []:
        try:
            level, ts = obs.get("niveau_eau_ngf"), obs.get("date_mesure")
            if level is None or not ts:
                continue
            depth = obs.get("profondeur_nappe")
            out[_iso(ts)] = (float(level), float(depth) if depth is not None else None)
        except (TypeError, ValueError) as e:
            logger.debug("Skipping piezometer TR value %r: %s", obs, e)
    return [(ts, lv, d) for ts, (lv, d) in sorted(out.items())]


def parse_piezo_daily(payload: dict) -> list[tuple[datetime, float, Optional[float]]]:
    """niveaux_nappes chroniques (daily) -> [(day 00:00 UTC, level m NGF, depth m)]."""
    out: dict[datetime, tuple[float, Optional[float]]] = {}
    for obs in (payload or {}).get("data", []) or []:
        try:
            level, day = obs.get("niveau_nappe_eau"), obs.get("date_mesure")
            if level is None or not day:
                continue
            depth = obs.get("profondeur_nappe")
            out[datetime.strptime(day[:10], "%Y-%m-%d")] = (
                float(level), float(depth) if depth is not None else None,
            )
        except (TypeError, ValueError) as e:
            logger.debug("Skipping piezometer daily value %r: %s", obs, e)
    return [(ts, lv, d) for ts, (lv, d) in sorted(out.items())]


def parse_shom(payload: dict, resample_minutes: int = SHOM_RESAMPLE_MINUTES) -> list[tuple[datetime, float]]:
    """SHOM observation json ('YYYY/MM/DD HH:MM:SS' UTC, metres) -> bucket means.

    Samples are averaged per ``resample_minutes`` bucket (timestamp = bucket
    start); 0 keeps the raw samples.
    """
    buckets: dict[datetime, list[float]] = {}
    for obs in (payload or {}).get("data", []) or []:
        try:
            value, ts = obs.get("value"), obs.get("timestamp")
            if value is None or not ts:
                continue
            t = datetime.strptime(ts, "%Y/%m/%d %H:%M:%S")
            if resample_minutes > 0:
                t = t.replace(minute=t.minute - t.minute % resample_minutes, second=0)
            buckets.setdefault(t, []).append(float(value))
        except (TypeError, ValueError) as e:
            logger.debug("Skipping SHOM observation %r: %s", obs, e)
    return [(t, round(sum(v) / len(v), 3)) for t, v in sorted(buckets.items())]


# ---------------------------------------------------------------------------
# Influx points (pure)
# ---------------------------------------------------------------------------


def make_flow_point(station: dict, timestamp: datetime, source: str,
                    q_m3s: Optional[float] = None, h_m: Optional[float] = None) -> Point:
    """``river_flow`` point; Q and H written at the same time and tags merge."""
    point = (
        Point(FLOW_MEASUREMENT)
        .tag("key", station["key"])
        .tag("station_id", station["id"])
        .tag("river", station.get("river", "Unknown"))
        .tag("source", source)
        .time(timestamp, WritePrecision.S)
    )
    if q_m3s is not None:
        point = point.field("q_m3s", float(q_m3s))
    if h_m is not None:
        point = point.field("h_m", float(h_m))
    return point


def make_groundwater_point(station: dict, timestamp: datetime, source: str,
                           level_m_ngf: float, depth_m: Optional[float] = None) -> Point:
    point = (
        Point(GROUNDWATER_MEASUREMENT)
        .tag("key", station["key"])
        .tag("code_bss", station["code_bss"])
        .tag("source", source)
        .field("level_m_ngf", float(level_m_ngf))
        .time(timestamp, WritePrecision.S)
    )
    if depth_m is not None:
        point = point.field("depth_m", float(depth_m))
    return point


def make_tide_observed_point(gauge: dict, timestamp: datetime, source: str, height_m: float) -> Point:
    return (
        Point(TIDE_OBSERVED_MEASUREMENT)
        .tag("key", gauge["key"])
        .tag("source", source)
        .field("height_m", float(height_m))
        .time(timestamp, WritePrecision.S)
    )


def flow_points(station: dict, source: str,
                q: list[tuple[datetime, float]],
                h: Optional[list[tuple[datetime, float]]] = None) -> list[Point]:
    """One point per timestamp with Q and/or H."""
    q_by_ts = dict(q or [])
    h_by_ts = dict(h or [])
    return [
        make_flow_point(station, ts, source, q_by_ts.get(ts), h_by_ts.get(ts))
        for ts in sorted(set(q_by_ts) | set(h_by_ts))
    ]


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------


def _fmt_z(dt: datetime) -> str:
    return _naive_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _get_json(client: httpx.AsyncClient, url: str, params: dict,
                    timeout: float, headers: Optional[dict] = None) -> dict:
    response = await client.get(url, params=params, timeout=timeout, headers=headers)
    response.raise_for_status()
    return response.json()


async def fetch_hubeau_tr(client: httpx.AsyncClient, code: str, grandeur: str,
                          start: datetime, end: datetime,
                          timeout: float = EXTERNAL_TIMEOUT_S) -> list[tuple[datetime, float]]:
    """Hub'Eau observations_tr for one station/grandeur (Q m3/s or H m). Raises."""
    payload = await _get_json(client, f"{HUBEAU_API_BASE}/observations_tr", {
        "code_entite": code,
        "grandeur_hydro": grandeur,
        "date_debut_obs": _fmt_z(start),
        "date_fin_obs": _fmt_z(end),
        "size": HUBEAU_PAGE_SIZE,
        "sort": "asc",
    }, timeout)
    return parse_hubeau_tr(payload)


async def fetch_hydroportail(client: httpx.AsyncClient, code: str, variable: str,
                             start: datetime, end: datetime,
                             timeout: float = HISTORY_TIMEOUT_S) -> list[tuple[datetime, float]]:
    """Hydroportail sub-hourly series (variable 'Q' or 'H'). Raises."""
    payload = await _get_json(client, f"{HYDROPORTAIL_BASE}/{code}/series", {
        "hydro_series[startAt]": start.strftime("%d/%m/%Y"),
        "hydro_series[endAt]": end.strftime("%d/%m/%Y"),
        "hydro_series[variableType]": "simple_and_interpolated_and_hourly_variable",
        "hydro_series[simpleAndInterpolatedAndHourlyVariable]": variable,
        "hydro_series[statusData]": "most_valid",
    }, timeout, headers={
        **_HYDROPORTAIL_HEADERS,
        "Referer": f"https://www.hydro.eaufrance.fr/stationhydro/{code}/series",
    })
    return parse_hydroportail(payload)


async def fetch_obs_elab_qmnj(client: httpx.AsyncClient, code: str, start: datetime, end: datetime,
                              timeout: float = HISTORY_TIMEOUT_S) -> list[tuple[datetime, float]]:
    """Hub'Eau obs_elab QmnJ (daily mean discharge, m3/s). Raises."""
    payload = await _get_json(client, f"{HUBEAU_API_BASE}/obs_elab", {
        "code_entite": code,
        "grandeur_hydro_elab": "QmnJ",
        "date_debut_obs_elab": start.strftime("%Y-%m-%d"),
        "date_fin_obs_elab": end.strftime("%Y-%m-%d"),
        "size": HUBEAU_PAGE_SIZE,
    }, timeout)
    return parse_obs_elab(payload)


async def fetch_piezo_tr(client: httpx.AsyncClient, code_bss: str, start: datetime,
                         end: Optional[datetime] = None,
                         timeout: float = EXTERNAL_TIMEOUT_S) -> list[tuple[datetime, float, Optional[float]]]:
    params = {
        "code_bss": code_bss,
        "date_debut_mesure": start.strftime("%Y-%m-%d"),
        "sort": "asc",
        "size": HUBEAU_PAGE_SIZE,
    }
    if end is not None:
        params["date_fin_mesure"] = end.strftime("%Y-%m-%d")
    payload = await _get_json(client, f"{NAPPES_API_BASE}/chroniques_tr", params, timeout)
    return parse_piezo_tr(payload)


async def fetch_piezo_daily(client: httpx.AsyncClient, code_bss: str, start: datetime, end: datetime,
                            timeout: float = HISTORY_TIMEOUT_S) -> list[tuple[datetime, float, Optional[float]]]:
    payload = await _get_json(client, f"{NAPPES_API_BASE}/chroniques", {
        "code_bss": code_bss,
        "date_debut_mesure": start.strftime("%Y-%m-%d"),
        "date_fin_mesure": end.strftime("%Y-%m-%d"),
        "sort": "asc",
        "size": HUBEAU_PAGE_SIZE,
    }, timeout)
    return parse_piezo_daily(payload)


async def fetch_shom(client: httpx.AsyncClient, shom_id: int, start: datetime, end: datetime,
                     timeout: float = EXTERNAL_TIMEOUT_S) -> list[tuple[datetime, float]]:
    payload = await _get_json(client, SHOM_OBSERVATION_URL.format(shom_id=int(shom_id)), {
        "sources": SHOM_SOURCE,
        "dtStart": _fmt_z(start),
        "dtEnd": _fmt_z(end),
    }, timeout)
    return parse_shom(payload)


def _record(breaker: CircuitBreaker, errors: list[BaseException], successes: int, label: str) -> None:
    """Open the breaker when nothing answered and the cause is upstream."""
    if successes:
        breaker.record_success()
        upstream = [e for e in errors if is_upstream_failure(e)]
        if upstream:
            logger.info("%s: %d request(s) failed this round: %s",
                        label, len(upstream), describe_error(upstream[0]))
    for e in errors:
        if not is_upstream_failure(e):
            logger.warning("%s rejected a request: %s", label, describe_error(e))
    if errors and not successes and any(is_upstream_failure(e) for e in errors):
        breaker.record_failure(next(e for e in errors if is_upstream_failure(e)))


# ---------------------------------------------------------------------------
# Realtime collection (scheduler jobs)
# ---------------------------------------------------------------------------


async def collect_and_store_flows(hours_back: int = FLOW_REALTIME_HOURS) -> int:
    """Fetch the recent Q (and H when ``collect_h``) of every indicator station."""
    if not _hubeau_breaker.allow():
        return 0
    stations = get_settings().indicator_flow_stations
    end = _utcnow()
    start = end - timedelta(hours=hours_back)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    errors: list[BaseException] = []
    successes = 0

    async with httpx.AsyncClient() as client:
        async def _one(station: dict) -> list[Point]:
            nonlocal successes
            async with semaphore:
                try:
                    q = await fetch_hubeau_tr(client, station["id"], "Q", start, end)
                    h = (await fetch_hubeau_tr(client, station["id"], "H", start, end)
                         if station.get("collect_h") else None)
                    successes += 1
                    return flow_points(station, SOURCE_HUBEAU_TR, q, h)
                except Exception as e:
                    errors.append(e)
                    return []

        batches = await asyncio.gather(*[_one(s) for s in stations])

    _record(_hubeau_breaker, errors, successes, "Hub'Eau indicators")
    points = [p for batch in batches for p in batch]
    return await influx_client.write_points_batch(points) if points else 0


async def collect_and_store_groundwater(days_back: int = GROUNDWATER_REALTIME_DAYS) -> int:
    """Fetch the recent hourly level of every indicator piezometer."""
    if not _nappes_breaker.allow():
        return 0
    stations = get_settings().indicator_groundwater_stations
    start = _utcnow() - timedelta(days=days_back)
    errors: list[BaseException] = []
    successes = 0
    points: list[Point] = []
    async with httpx.AsyncClient() as client:
        for station in stations:
            try:
                rows = await fetch_piezo_tr(client, station["code_bss"], start)
                successes += 1
                points.extend(
                    make_groundwater_point(station, ts, SOURCE_ADES_TR, level, depth)
                    for ts, level, depth in rows
                )
            except Exception as e:
                errors.append(e)
    _record(_nappes_breaker, errors, successes, "Hub'Eau niveaux_nappes")
    return await influx_client.write_points_batch(points) if points else 0


async def collect_and_store_tide_observed(hours_back: int = TIDE_REALTIME_HOURS) -> int:
    """Fetch the recent observed water level of every indicator tide gauge."""
    gauges = get_settings().indicator_tide_gauges
    end = _utcnow()
    start = end - timedelta(hours=hours_back)
    points: list[Point] = []
    async with httpx.AsyncClient() as client:
        for gauge in gauges:
            source = gauge.get("source")
            breaker = _shom_breaker if source == "shom" else _hubeau_breaker
            if not breaker.allow():
                continue
            try:
                if source == "shom":
                    rows = await fetch_shom(client, gauge["shom_id"], start, end)
                    tag = SOURCE_SHOM
                else:
                    rows = await fetch_hubeau_tr(client, gauge["id"], "H", start, end)
                    tag = SOURCE_HUBEAU_TR
                breaker.record_success()
                points.extend(make_tide_observed_point(gauge, ts, tag, h) for ts, h in rows)
            except Exception as e:
                _record(breaker, [e], 0, gauge["name"])
    return await influx_client.write_points_batch(points) if points else 0


# ---------------------------------------------------------------------------
# Historical backfill (explicit backfill task only)
# ---------------------------------------------------------------------------


def _batches(start: datetime, end: datetime, days: int):
    current = start
    while current < end:
        nxt = min(current + timedelta(days=days), end)
        yield current, nxt
        current = nxt


async def backfill_flows(start: datetime, end: datetime, batch_days: int = BACKFILL_BATCH_DAYS,
                         pause_s: float = 0.5) -> dict:
    """Hydroportail Q (and H) per 30-day batch, QmnJ when Hydroportail is empty."""
    results: dict[str, dict] = {}
    async with httpx.AsyncClient() as client:
        for station in get_settings().indicator_flow_stations:
            written = 0
            daily_batches = 0
            for b_start, b_end in _batches(start, end, batch_days):
                source = SOURCE_HYDROPORTAIL
                try:
                    q = await fetch_hydroportail(client, station["id"], "Q", b_start, b_end)
                    h = (await fetch_hydroportail(client, station["id"], "H", b_start, b_end)
                         if station.get("collect_h") else None)
                except Exception as e:
                    logger.warning("Hydroportail %s %s -> %s: %s", station["key"],
                                   b_start.date(), b_end.date(), describe_error(e))
                    q, h = [], None
                if not q:
                    try:
                        q = await fetch_obs_elab_qmnj(client, station["id"], b_start, b_end)
                        source = SOURCE_HUBEAU_ELAB
                        daily_batches += 1 if q else 0
                    except Exception as e:
                        logger.warning("obs_elab QmnJ %s: %s", station["key"], describe_error(e))
                points = flow_points(station, source, q, h)
                if points:
                    written += await influx_client.write_points_batch(points)
                await asyncio.sleep(pause_s)
            results[station["key"]] = {"points": written, "daily_fallback_batches": daily_batches}
            logger.info("Indicator flow backfill %s: %d points", station["key"], written)
    return results


async def backfill_groundwater(start: datetime, end: datetime) -> dict:
    """Daily ``chroniques`` over the period plus the hourly ``chroniques_tr``."""
    results: dict[str, dict] = {}
    async with httpx.AsyncClient() as client:
        for station in get_settings().indicator_groundwater_stations:
            points: list[Point] = []
            try:
                points.extend(
                    make_groundwater_point(station, ts, SOURCE_ADES_DAILY, level, depth)
                    for ts, level, depth in await fetch_piezo_daily(client, station["code_bss"], start, end)
                )
            except Exception as e:
                logger.warning("Piezometer daily %s: %s", station["key"], describe_error(e))
            try:
                points.extend(
                    make_groundwater_point(station, ts, SOURCE_ADES_TR, level, depth)
                    for ts, level, depth in await fetch_piezo_tr(
                        client, station["code_bss"], start, end, timeout=HISTORY_TIMEOUT_S,
                    )
                )
            except Exception as e:
                logger.warning("Piezometer TR %s: %s", station["key"], describe_error(e))
            written = await influx_client.write_points_batch(points) if points else 0
            results[station["key"]] = {"points": written}
    return results


async def backfill_tide_observed(start: datetime, end: datetime, batch_days: int = BACKFILL_BATCH_DAYS,
                                 pause_s: float = 0.5) -> dict:
    """SHOM per 30-day batch (~3 MB of 1-min data each), Hydroportail H for Hub'Eau gauges."""
    results: dict[str, dict] = {}
    async with httpx.AsyncClient() as client:
        for gauge in get_settings().indicator_tide_gauges:
            written = 0
            for b_start, b_end in _batches(start, end, batch_days):
                try:
                    if gauge.get("source") == "shom":
                        rows = await fetch_shom(client, gauge["shom_id"], b_start, b_end, timeout=HISTORY_TIMEOUT_S)
                        tag = SOURCE_SHOM
                    else:
                        rows = await fetch_hydroportail(client, gauge["id"], "H", b_start, b_end)
                        tag = SOURCE_HYDROPORTAIL
                except Exception as e:
                    logger.warning("Observed tide %s %s -> %s: %s", gauge["key"],
                                   b_start.date(), b_end.date(), describe_error(e))
                    rows = []
                points = [make_tide_observed_point(gauge, ts, tag, h) for ts, h in rows]
                if points:
                    written += await influx_client.write_points_batch(points)
                await asyncio.sleep(pause_s)
            results[gauge["key"]] = {"points": written}
            logger.info("Observed tide backfill %s: %d points", gauge["key"], written)
    return results


async def backfill_indicators(start: datetime, end: datetime) -> dict:
    """All indicator series over [start, end] (flows, groundwater, observed tide)."""
    return {
        "flows": await backfill_flows(start, end),
        "groundwater": await backfill_groundwater(start, end),
        "tide_observed": await backfill_tide_observed(start, end),
    }
