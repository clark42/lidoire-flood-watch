"""Public archives used by the evaluation, with a local raw cache.

Read-only HTTP GETs only, politely rate limited per host, every raw response
cached as JSON under ``backend/data/eval_cache/`` (gitignored by ``data/``),
so a second ``build`` runs fully offline.

Sources (best free historical source per input):

- River levels: Hydroportail (hydro.eaufrance.fr ``/stationhydro/ajax``),
  the same endpoint as ``collectors.hubeau.fetch_hydroportail_data`` used by
  the production backfill. It serves the validated Banque Hydro series at
  variable time step (points are stored when the level changes; median step
  ~20 min, some gaps of several hours when the level is flat). When a chunk
  is empty, Hub'Eau ``obs_elab`` HIXnJ (DAILY maximum) is used instead, like
  the production backfill fallback, and flagged as degraded.
- Rain: Open-Meteo Historical Forecast API (archived short-range forecasts,
  closest to what the production ``/v1/forecast?past_days`` input sees), on
  the configured Lidoire basin points; ERA5 archive (``archive-api``) only
  fills hours the first source lacks.
- Tide: Open-Meteo Marine ``sea_level_height_msl`` at the Gironde mouth, the
  same series and point as the production tide collector/backfill (model
  sea level; coefficients are ESTIMATED from the tidal range).

Hydrological inputs of the v4-alpha candidates (research 2026-10-26):

- Discharge Q (Eyraud P540401002, Caudeau P524401001, Pessac P555001001):
  Hydroportail variable-step Q (L/s -> m3/s), Hub'Eau ``QIXnJ`` daily max
  where a chunk is empty. Libourne H (P577001001, weight 0, tidal) for the
  confluence stage.
- Climatology (fixed period ending 2022-09-30, BEFORE the evaluation
  period): Hub'Eau ``QIXnJ`` (Eyraud since 2009, Caudeau since 2010) and
  ``HIXnJ`` (Pessac since 1993) daily maxima, for a-priori thresholds.
- Groundwater: Hub'Eau ``niveaux_nappes/chroniques`` (BRGM ADES), alluvial
  piezometer of Mouliets-et-Villemartin 08048X0088/P, daily, m NGF.
- GloFAS v4 through the Open-Meteo Flood API, Lidoire cell (44.875,
  -0.025): ``consolidated_v4`` (reanalysis, 1984 -> 2025-05, climatology)
  and ``seamless_v4`` (consolidated, then the archived ``forecast_v4``
  day 0) over the evaluation period. Daily values.

COMEPHORE (Meteo-France hourly 1 km radar reanalysis) is NOT downloaded:
the monthly tar files (200-240 MB) can be range-read, but finding the RR
members needs one header request per member (3 members per hour), i.e.
~140k requests over 2022-2026, or ~10 GB for the whole tars.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parents[2]
CACHE_DIR = BACKEND_DIR / "data" / "eval_cache"

HYDROPORTAIL_URL = "https://www.hydro.eaufrance.fr/stationhydro/ajax/{station_id}/series"
HUBEAU_ELAB_URL = "https://hubeau.eaufrance.fr/api/v2/hydrometrie/obs_elab"
OPEN_METEO_HIST_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
OPEN_METEO_FLOOD_URL = "https://flood-api.open-meteo.com/v1/flood"
HUBEAU_PIEZO_URL = "https://hubeau.eaufrance.fr/api/v1/niveaux_nappes/chroniques"

# Minimum delay between two requests to the same host (seconds)
MIN_INTERVAL_S = {
    "www.hydro.eaufrance.fr": 2.0,
    "hubeau.eaufrance.fr": 1.0,
}
DEFAULT_MIN_INTERVAL_S = 1.0

EVAL_USER_AGENT = "lidoire-flood-watch-eval/1.0 (offline evaluation; read-only)"

# Hydroportail answers 403 without browser-like headers (same headers as the
# production collector collectors/hubeau.py).
HYDROPORTAIL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}


# ---------------------------------------------------------------------------
# HTTP + cache
# ---------------------------------------------------------------------------

class PoliteClient:
    """Synchronous GET-only JSON client with per-host rate limiting and retries."""

    def __init__(
        self,
        timeout: float = 90.0,
        retries: int = 3,
        min_interval_s: Optional[dict[str, float]] = None,
        transport: Optional[httpx.BaseTransport] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": EVAL_USER_AGENT},
            transport=transport,
        )
        self._retries = retries
        self._min_interval = dict(MIN_INTERVAL_S if min_interval_s is None else min_interval_s)
        self._last_call: dict[str, float] = {}
        self._sleep = sleep
        self.n_requests = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _wait_turn(self, host: str) -> None:
        interval = self._min_interval.get(host, DEFAULT_MIN_INTERVAL_S)
        last = self._last_call.get(host)
        if last is not None:
            delay = interval - (time.monotonic() - last)
            if delay > 0:
                self._sleep(delay)
        self._last_call[host] = time.monotonic()

    def get_json(self, url: str, params: dict, headers: Optional[dict] = None) -> dict:
        """GET ``url`` and decode JSON. Raises httpx.HTTPError after retries."""
        host = urlparse(url).netloc
        last_exc: Optional[Exception] = None
        for attempt in range(self._retries):
            self._wait_turn(host)
            self.n_requests += 1
            try:
                response = self._client.get(url, params=params, headers=headers)
                if response.status_code in (429, 500, 502, 503, 504):
                    raise httpx.HTTPStatusError(
                        f"HTTP {response.status_code}", request=response.request, response=response,
                    )
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None and status < 500 and status != 429:
                    break  # client error: retrying will not help
                self._sleep(min(30.0, 3.0 * (2 ** attempt)))
        assert last_exc is not None
        raise last_exc


class RawCache:
    """Raw JSON responses stored as ``<root>/<kind>/<key>.json``."""

    def __init__(self, root: Path = CACHE_DIR):
        self.root = Path(root)

    def path(self, kind: str, key: str) -> Path:
        return self.root / kind / f"{key}.json"

    def load(self, kind: str, key: str) -> Optional[dict]:
        p = self.path(kind, key)
        if not p.exists():
            return None
        with p.open(encoding="utf-8") as fh:
            return json.load(fh)

    def save(self, kind: str, key: str, payload: dict) -> None:
        p = self.path(kind, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        tmp.replace(p)


def cached_get(
    cache: RawCache,
    client: Optional[PoliteClient],
    kind: str,
    key: str,
    url: str,
    params: dict,
    headers: Optional[dict] = None,
    refresh: bool = False,
) -> Optional[dict]:
    """Cached JSON, else a download (stored), else None (offline or error).

    ``client=None`` means offline: only the cache is read.
    """
    if not refresh:
        hit = cache.load(kind, key)
        if hit is not None:
            return hit
    if client is None:
        return None
    try:
        payload = client.get_json(url, params, headers)
    except Exception as exc:  # network/HTTP errors are reported, not fatal
        logger.warning("download failed %s %s: %s", kind, key, exc)
        return None
    cache.save(kind, key, payload)
    return payload


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def last_complete_day() -> date:
    """Yesterday (UTC): archives are requested up to this day at most."""
    return (datetime.now(timezone.utc) - timedelta(days=1)).date()


def _aligned_chunks(start: date, end: date, months: int, cap: Optional[date]) -> list[tuple[date, date]]:
    chunks = []
    cur = date(start.year, months * ((start.month - 1) // months) + 1, 1)
    while cur <= end:
        nxt_month = cur.month + months
        nxt = date(cur.year + (nxt_month - 1) // 12, (nxt_month - 1) % 12 + 1, 1)
        c_end = nxt - timedelta(days=1)
        if cap is not None:
            c_end = min(c_end, cap)
        if cur <= c_end:
            chunks.append((cur, c_end))
        cur = nxt
    return chunks


def quarter_chunks(start: date, end: date, cap: Optional[date] = None) -> list[tuple[date, date]]:
    """Whole calendar quarters covering [start, end] (inclusive days).

    Chunks are NOT clipped to the requested period, so cache files are
    reused whatever --start/--end is asked; only ``cap`` (last day with
    data, e.g. yesterday) shortens the current quarter.
    """
    return _aligned_chunks(start, end, 3, cap)


def year_chunks(start: date, end: date, cap: Optional[date] = None) -> list[tuple[date, date]]:
    """Whole calendar years covering [start, end], last one capped at ``cap``."""
    return _aligned_chunks(start, end, 12, cap)


# ---------------------------------------------------------------------------
# Parsers (pure)
# ---------------------------------------------------------------------------

def _naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# Hydroportail units -> m (heights) or m3/s (discharge)
HYDROPORTAIL_UNIT_SCALE = {"mm": 1000.0, "cm": 100.0, "l": 1000.0, "l/s": 1000.0}


def parse_hydroportail(payload: Optional[dict]) -> list[tuple[datetime, float]]:
    """(naive UTC timestamp, value) points from a Hydroportail series payload.

    Heights come in mm (``series.unit == "mm"``) -> m, discharge in L/s
    (``"l"``) -> m3/s; timestamps ISO UTC.
    """
    if not payload:
        return []
    series = payload.get("series") or {}
    unit = (series.get("unit") or "mm").lower()
    scale = HYDROPORTAIL_UNIT_SCALE.get(unit, 1.0)
    out = []
    for obs in series.get("data") or []:
        v, t = obs.get("v"), obs.get("t")
        if v is None or not t:
            continue
        try:
            ts = _naive_utc(datetime.fromisoformat(str(t).replace("Z", "+00:00")))
            out.append((ts, float(v) / scale))
        except (TypeError, ValueError):
            continue
    out.sort()
    return out


def parse_hubeau_elab(payload: Optional[dict]) -> list[tuple[date, float]]:
    """(day, value) from a Hub'Eau obs_elab payload: HIXnJ mm -> m, QIXnJ L/s -> m3/s."""
    if not payload:
        return []
    out = []
    for obs in payload.get("data") or []:
        v, d = obs.get("resultat_obs_elab"), obs.get("date_obs_elab")
        if v is None or not d:
            continue
        try:
            out.append((date.fromisoformat(str(d)[:10]), float(v) / 1000.0))
        except (TypeError, ValueError):
            continue
    out.sort()
    return out


def parse_open_meteo_hourly(payload: Optional[dict], variable: str) -> list[tuple[datetime, Optional[float]]]:
    """(naive UTC hour label, value or None) from an Open-Meteo hourly payload.

    Requests use ``timezone=UTC``; for precipitation the value at label T is
    the amount of the preceding hour [T-1h, T].
    """
    if not payload:
        return []
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    values = hourly.get(variable) or []
    out = []
    for t, v in zip(times, values):
        try:
            ts = _naive_utc(datetime.fromisoformat(str(t)))
        except ValueError:
            continue
        out.append((ts, None if v is None else float(v)))
    return out


def parse_hubeau_piezo(payload: Optional[dict]) -> list[tuple[date, float]]:
    """(day, groundwater level m NGF) from a Hub'Eau niveaux_nappes chroniques payload."""
    if not payload:
        return []
    out = []
    for obs in payload.get("data") or []:
        v, d = obs.get("niveau_nappe_eau"), obs.get("date_mesure")
        if v is None or not d:
            continue
        try:
            out.append((date.fromisoformat(str(d)[:10]), float(v)))
        except (TypeError, ValueError):
            continue
    out.sort()
    return out


def parse_open_meteo_daily(payload: Optional[dict], variable: str) -> list[tuple[date, Optional[float]]]:
    """(day, value or None) from an Open-Meteo daily payload."""
    if not payload:
        return []
    daily = payload.get("daily") or {}
    out = []
    for t, v in zip(daily.get("time") or [], daily.get(variable) or []):
        try:
            out.append((date.fromisoformat(str(t)[:10]), None if v is None else float(v)))
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------

@dataclass
class StationRaw:
    """Raw level series of one station over the period."""

    station_id: str
    # Variable-step validated points (naive UTC, m)
    points: list[tuple[datetime, float]] = field(default_factory=list)
    # Daily maxima used where the variable-step series is missing (day, m)
    daily_max: list[tuple[date, float]] = field(default_factory=list)
    chunks_ok: int = 0
    chunks_daily_fallback: list[str] = field(default_factory=list)
    chunks_missing: list[str] = field(default_factory=list)


def download_station_levels(
    station_id: str,
    start: date,
    end: date,
    cache: RawCache,
    client: Optional[PoliteClient],
    refresh: bool = False,
    variable: str = "H",
) -> StationRaw:
    """Hydroportail series by quarter, Hub'Eau <variable>IXnJ daily max where empty.

    ``variable`` is ``"H"`` (height, m) or ``"Q"`` (discharge, m3/s).
    """
    if variable not in ("H", "Q"):
        raise ValueError(f"unknown variable {variable!r}")
    raw = StationRaw(station_id=station_id)
    seen: set[datetime] = set()
    suffix = "" if variable == "H" else f"_{variable}"
    for c_start, c_end in quarter_chunks(start, end, cap=last_complete_day()):
        key = f"{station_id}{suffix}_{c_start.isoformat()}_{c_end.isoformat()}"
        payload = cached_get(
            cache, client, "hydroportail", key,
            HYDROPORTAIL_URL.format(station_id=station_id),
            {
                "hydro_series[startAt]": c_start.strftime("%d/%m/%Y"),
                "hydro_series[endAt]": c_end.strftime("%d/%m/%Y"),
                "hydro_series[variableType]": "simple_and_interpolated_and_hourly_variable",
                "hydro_series[simpleAndInterpolatedAndHourlyVariable]": variable,
                "hydro_series[statusData]": "most_valid",
            },
            headers={
                **HYDROPORTAIL_HEADERS,
                "Referer": f"https://www.hydro.eaufrance.fr/stationhydro/{station_id}/series",
            },
            refresh=refresh,
        )
        points = parse_hydroportail(payload)
        if points:
            raw.chunks_ok += 1
            for ts, h in points:
                if ts not in seen:
                    seen.add(ts)
                    raw.points.append((ts, h))
            continue

        daily_payload = cached_get(
            cache, client, f"hubeau_{variable.lower()}ixnj", key, HUBEAU_ELAB_URL,
            {
                "code_entite": station_id,
                "grandeur_hydro_elab": f"{variable}IXnJ",
                "date_debut_obs_elab": c_start.isoformat(),
                "date_fin_obs_elab": c_end.isoformat(),
                "size": 20000,
                "sort": "asc",
            },
            refresh=refresh,
        )
        daily = parse_hubeau_elab(daily_payload)
        if daily:
            raw.daily_max.extend(daily)
            raw.chunks_daily_fallback.append(key)
        else:
            raw.chunks_missing.append(key)
    raw.points.sort()
    raw.daily_max.sort()
    return raw


def download_open_meteo_series(
    url: str,
    kind: str,
    point_id: str,
    lat: float,
    lon: float,
    variable: str,
    start: date,
    end: date,
    cache: RawCache,
    client: Optional[PoliteClient],
    refresh: bool = False,
) -> list[tuple[datetime, Optional[float]]]:
    """One Open-Meteo hourly variable at one point, by calendar year."""
    out: list[tuple[datetime, Optional[float]]] = []
    for c_start, c_end in year_chunks(start, end, cap=last_complete_day()):
        key = f"{point_id}_{variable}_{c_start.isoformat()}_{c_end.isoformat()}"
        payload = cached_get(
            cache, client, kind, key, url,
            {
                "latitude": lat,
                "longitude": lon,
                "hourly": variable,
                "start_date": c_start.isoformat(),
                "end_date": c_end.isoformat(),
                "timezone": "UTC",
            },
            refresh=refresh,
        )
        out.extend(parse_open_meteo_hourly(payload, variable))
    out.sort(key=lambda x: x[0])
    return out


@dataclass
class RainRaw:
    """Hourly rain per basin point (label = hour end, mm over the hour)."""

    # point_id -> {hour label: mm}
    by_point: dict[str, dict[datetime, float]] = field(default_factory=dict)
    # hours filled from ERA5 per point
    era5_filled: dict[str, int] = field(default_factory=dict)


def download_rain(
    points: list[dict],
    start: date,
    end: date,
    cache: RawCache,
    client: Optional[PoliteClient],
    refresh: bool = False,
    era5_fallback: bool = True,
) -> RainRaw:
    """Historical-forecast hourly precipitation per basin point (+ ERA5 gaps)."""
    rain = RainRaw()
    for pt in points:
        pid = pt["id"]
        series = download_open_meteo_series(
            OPEN_METEO_HIST_FORECAST_URL, "openmeteo_histfc", pid, pt["lat"], pt["lon"],
            "precipitation", start, end, cache, client, refresh,
        )
        values = {ts: v for ts, v in series if v is not None}
        last = min(end, last_complete_day())
        expected_hours = max(0, (last - start).days + 1) * 24
        in_range = sum(1 for ts in values if start <= ts.date() <= last)
        filled = 0
        if era5_fallback and in_range < expected_hours:
            era5 = download_open_meteo_series(
                OPEN_METEO_ARCHIVE_URL, "openmeteo_era5", pid, pt["lat"], pt["lon"],
                "precipitation", start, end, cache, client, refresh,
            )
            for ts, v in era5:
                if v is not None and ts not in values:
                    values[ts] = v
                    filled += 1
        rain.by_point[pid] = values
        rain.era5_filled[pid] = filled
    return rain


def download_sea_level(
    lat: float,
    lon: float,
    start: date,
    end: date,
    cache: RawCache,
    client: Optional[PoliteClient],
    refresh: bool = False,
) -> list[tuple[datetime, Optional[float]]]:
    """Hourly model sea level (m) at the Gironde mouth (Open-Meteo Marine)."""
    return download_open_meteo_series(
        OPEN_METEO_MARINE_URL, "openmeteo_marine", "gironde", lat, lon,
        "sea_level_height_msl", start, end, cache, client, refresh,
    )


def download_daily_elab(
    station_id: str,
    grandeur: str,
    start: date,
    end: date,
    cache: RawCache,
    client: Optional[PoliteClient],
    refresh: bool = False,
) -> list[tuple[date, float]]:
    """Hub'Eau obs_elab daily series (one request, e.g. QIXnJ/HIXnJ climatology).

    The period is fixed by the caller (climatology before the evaluation),
    so the cache key never depends on today's date.
    """
    payload = cached_get(
        cache, client, "hubeau_elab_clim", f"{station_id}_{grandeur}_{start.isoformat()}_{end.isoformat()}",
        HUBEAU_ELAB_URL,
        {
            "code_entite": station_id,
            "grandeur_hydro_elab": grandeur,
            "date_debut_obs_elab": start.isoformat(),
            "date_fin_obs_elab": end.isoformat(),
            "size": 20000,
            "sort": "asc",
        },
        refresh=refresh,
    )
    return parse_hubeau_elab(payload)


def download_piezo(
    code_bss: str,
    start: date,
    end: date,
    cache: RawCache,
    client: Optional[PoliteClient],
    refresh: bool = False,
) -> list[tuple[date, float]]:
    """Daily groundwater level (m NGF) of one piezometer, by calendar year."""
    out: list[tuple[date, float]] = []
    safe = code_bss.replace("/", "_")
    for c_start, c_end in year_chunks(start, end, cap=last_complete_day()):
        payload = cached_get(
            cache, client, "hubeau_piezo", f"{safe}_{c_start.isoformat()}_{c_end.isoformat()}",
            HUBEAU_PIEZO_URL,
            {
                "code_bss": code_bss,
                "date_debut_mesure": c_start.isoformat(),
                "date_fin_mesure": c_end.isoformat(),
                "size": 20000,
                "sort": "asc",
            },
            refresh=refresh,
        )
        out.extend(parse_hubeau_piezo(payload))
    out = sorted(dict(out).items())
    return out


def download_glofas(
    model: str,
    lat: float,
    lon: float,
    start: date,
    end: date,
    cache: RawCache,
    client: Optional[PoliteClient],
    refresh: bool = False,
) -> list[tuple[date, Optional[float]]]:
    """GloFAS v4 daily river discharge (m3/s) at one cell (Open-Meteo Flood API), by year."""
    out: list[tuple[date, Optional[float]]] = []
    for c_start, c_end in year_chunks(start, end, cap=last_complete_day()):
        payload = cached_get(
            cache, client, "openmeteo_flood", f"{model}_{lat:g}_{lon:g}_{c_start.isoformat()}_{c_end.isoformat()}",
            OPEN_METEO_FLOOD_URL,
            {
                "latitude": lat,
                "longitude": lon,
                "daily": "river_discharge",
                "models": model,
                "start_date": c_start.isoformat(),
                "end_date": c_end.isoformat(),
            },
            refresh=refresh,
        )
        out.extend(parse_open_meteo_daily(payload, "river_discharge"))
    out.sort(key=lambda x: x[0])
    return out
