"""Official vigilance collector: Vigicrues (floods) and Meteo-France (weather).

Informative only: the colours are shown on the dashboard and archived for a
later correlation with the episodes, they are NOT a risk model input.

Endpoints (verified 2026-10-26, no key):
- Vigicrues ``services/1/InfoVigiCru.geojson``: one feature per troncon
  (``CdEntCru``, ``lbentcru``, ``NivInfViCr`` 1-4) plus ``DtHrInfoVigiCru``.
  About 2.2 MB, no gzip, no per-troncon filter (query parameters are
  ignored). DO5 "Dordogne aval" holds Pessac, DO22 "Confluence Garonne -
  Dordogne" holds Libourne.
- Meteo-France vigilance map, data.gouv mirror (no "latest" alias):
  ``.../vigilance/metropole/{YYYY}/{MM}/{DD}/{HHMMSS}/CDP_CARTE_EXTERNE.json``
  (302 to the OVH bucket). The day index lists the run directories (UTC);
  text-only updates hold no map (404), so older runs of the day are tried.
  Phenomena: 1 vent, 2 pluie-inondation, 3 orages, 4 crues, 5 neige-verglas,
  6 canicule, 7 grand froid, 8 avalanches, 9 vagues-submersion; periods J
  (today) and J1 (tomorrow); colours 1 vert .. 4 rouge.

Request paths never call these APIs: the scheduler job (every 30 min) fills
the cache read by :func:`peek_vigilance`. Every colour change is written to
the InfluxDB measurement ``vigilance`` (tags source/zone/phenomenon, field
``level``), plus once after a restart and every KEEPALIVE so the series
stays readable as a step function and /api/status can tell its freshness.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from app.config import get_settings
from app.services.cache import (
    EXTERNAL_TIMEOUT_S,
    CircuitBreaker,
    TTLCache,
    describe_error,
    is_upstream_failure,
)

logger = logging.getLogger(__name__)

VIGICRUES_INFO_URL = "https://www.vigicrues.gouv.fr/services/1/InfoVigiCru.geojson"
VIGICRUES_TERRITORY_PAGE = "https://www.vigicrues.gouv.fr/territoire/{territory}"
VIGICRUES_STATION_PAGE = "https://www.vigicrues.gouv.fr/station/{station_id}"
VIGICRUES_HOME = "https://www.vigicrues.gouv.fr/"

MF_MIRROR_BASE = "https://files.data.gouv.fr/meteofrance/data/vigilance/metropole"
MF_MAP_FILE = "CDP_CARTE_EXTERNE.json"
MF_DEPARTMENT_PAGE = "https://vigilance.meteofrance.fr/fr/{slug}"
MF_HOME = "https://vigilance.meteofrance.fr/fr"
# Newest run directories tried before giving up (text-only runs have no map)
MF_MAX_RUNS_TRIED = 4

MEASUREMENT = "vigilance"
SOURCE_VIGICRUES = "vigicrues"
SOURCE_METEOFRANCE = "meteofrance"
PHENOMENON_FLOOD = "crues"

# Official colour names (same for both services)
LEVEL_COLORS = {1: "vert", 2: "jaune", 3: "orange", 4: "rouge"}

# Meteo-France phenomenon id -> (tag key, French label)
MF_PHENOMENA: dict[str, tuple[str, str]] = {
    "1": ("vent", "Vent violent"),
    "2": ("pluie-inondation", "Pluie-inondation"),
    "3": ("orages", "Orages"),
    "4": ("crues", "Crues"),
    "5": ("neige-verglas", "Neige-verglas"),
    "6": ("canicule", "Canicule"),
    "7": ("grand-froid", "Grand froid"),
    "8": ("avalanches", "Avalanches"),
    "9": ("vagues-submersion", "Vagues-submersion"),
}

JOB_ID = "vigilance_collector"
JOB_INTERVAL = timedelta(minutes=30)
# Cached results older than this are served as unavailable
LATEST_MAX_AGE_S = 2 * 60 * 60
# A level is re-written at least this often even when unchanged
KEEPALIVE = timedelta(hours=6)
# Freshness threshold of the stored series (/api/status, informative)
STATUS_MAX_AGE_HOURS = 7.0

_latest = TTLCache(ttl_seconds=LATEST_MAX_AGE_S, max_entries=4)
# (source, zone, phenomenon) -> (level, written_at): last value written
_stored: dict[tuple[str, str, str], tuple[int, datetime]] = {}
# Last Meteo-France run parsed (its map is not downloaded again)
_mf_last_run: Optional[str] = None

_vigicrues_breaker = CircuitBreaker("Vigicrues vigilance")
_meteofrance_breaker = CircuitBreaker("Meteo-France vigilance")

_RUN_DIR_RE = re.compile(r'href="[^"]*/(\d{6})/"')


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_dt(value: Any) -> Optional[datetime]:
    """ISO 8601 string -> naive UTC (None when missing or invalid)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _level(value: Any) -> Optional[int]:
    """Colour id -> 1..4 (None when missing or out of range)."""
    try:
        level = int(value)
    except (TypeError, ValueError):
        return None
    return level if 1 <= level <= 4 else None


def level_color(level: Optional[int]) -> Optional[str]:
    return LEVEL_COLORS.get(level) if level is not None else None


def reset_state() -> None:
    """Forget cached results and write state (tests)."""
    global _mf_last_run
    _latest.clear()
    _stored.clear()
    _mf_last_run = None


# ---------------------------------------------------------------------------
# Parsing (pure)
# ---------------------------------------------------------------------------


def parse_vigicrues(data: dict, troncons: list[dict], fetched_at: Optional[datetime]) -> dict:
    """InfoVigiCru.geojson -> ``{"available", "issued_at", "fetched_at", "troncons"}``.

    ``troncons`` is the configured list (code, name, territory, station_id),
    in display order; a troncon missing from the feed keeps ``level=None``.
    """
    levels: dict[str, tuple[Optional[int], Optional[str]]] = {}
    for feature in (data or {}).get("features") or []:
        props = (feature or {}).get("properties") or {}
        code = props.get("CdEntCru")
        if code:
            levels[str(code)] = (_level(props.get("NivInfViCr")), props.get("lbentcru"))

    items = []
    for troncon in troncons:
        code = troncon["code"]
        level, name = levels.get(code, (None, None))
        items.append({
            "code": code,
            "name": name or troncon.get("name") or code,
            "level": level,
            "color": level_color(level),
            "url": VIGICRUES_TERRITORY_PAGE.format(territory=troncon.get("territory", "")),
            "station_url": (
                VIGICRUES_STATION_PAGE.format(station_id=troncon["station_id"])
                if troncon.get("station_id") else None
            ),
        })
    return {
        "available": any(t["level"] is not None for t in items),
        "issued_at": _parse_dt((data or {}).get("DtHrInfoVigiCru")),
        "fetched_at": fetched_at,
        "troncons": items,
    }


def _windows(per_period: list[dict]) -> list[dict]:
    """Windows above green, merged when contiguous at the same level."""
    windows: list[dict] = []
    for begin, end, level in sorted(per_period, key=lambda w: w[0]):
        if level is None or level < 2 or begin is None or end is None:
            continue
        last = windows[-1] if windows else None
        if last and last["level"] == level and last["end"] >= begin:
            last["end"] = max(last["end"], end)
        else:
            windows.append({"begin": begin, "end": end, "level": level})
    return windows


def parse_meteofrance(
    data: dict,
    departments: list[dict],
    phenomena: list[str],
    fetched_at: Optional[datetime],
) -> dict:
    """CDP_CARTE_EXTERNE.json -> colours per departement and phenomenon.

    ``level`` is the J (today) colour of the phenomenon, ``level_tomorrow``
    the J1 one. A phenomenon without time slots (``crues``) covers its whole
    period. ``max_level`` is the departement colour over every phenomenon (J).
    """
    product = (data or {}).get("product") or {}
    periods = {p.get("echeance"): p for p in product.get("periods") or [] if isinstance(p, dict)}

    def domain(period: Optional[dict], code: str) -> dict:
        for item in ((period or {}).get("timelaps") or {}).get("domain_ids") or []:
            if str(item.get("domain_id")) == code:
                return item
        return {}

    def phenomenon(dom: dict, pid: str) -> dict:
        for item in dom.get("phenomenon_items") or []:
            if str(item.get("phenomenon_id")) == pid:
                return item
        return {}

    def slots(period: Optional[dict], item: dict, level: Optional[int]) -> list[tuple]:
        if not item:
            return []
        items = item.get("timelaps_items") or []
        if not items:
            return [(
                _parse_dt((period or {}).get("begin_validity_time")),
                _parse_dt((period or {}).get("end_validity_time")),
                level,
            )]
        return [(_parse_dt(t.get("begin_time")), _parse_dt(t.get("end_time")), _level(t.get("color_id")))
                for t in items]

    today, tomorrow = periods.get("J"), periods.get("J1")
    result_departments = []
    for dept in departments:
        code = str(dept["code"])
        dom_j, dom_j1 = domain(today, code), domain(tomorrow, code)
        items = []
        for pid in phenomena:
            key, label = MF_PHENOMENA.get(pid, (f"phenomene-{pid}", f"Phenomene {pid}"))
            ph_j, ph_j1 = phenomenon(dom_j, pid), phenomenon(dom_j1, pid)
            level = _level(ph_j.get("phenomenon_max_color_id")) if ph_j else None
            level_j1 = _level(ph_j1.get("phenomenon_max_color_id")) if ph_j1 else None
            items.append({
                "id": pid,
                "key": key,
                "label": label,
                "level": level,
                "color": level_color(level),
                "level_tomorrow": level_j1,
                "windows": _windows(slots(today, ph_j, level) + slots(tomorrow, ph_j1, level_j1)),
            })
        result_departments.append({
            "code": code,
            "name": dept.get("name") or code,
            "max_level": _level(dom_j.get("max_color_id")) if dom_j else None,
            "phenomena": items,
            "url": MF_DEPARTMENT_PAGE.format(slug=dept.get("slug", "")),
        })

    available = any(p["level"] is not None for d in result_departments for p in d["phenomena"])
    return {
        "available": available,
        "issued_at": _parse_dt(product.get("update_time")),
        "fetched_at": fetched_at,
        "valid_from": _parse_dt((today or {}).get("begin_validity_time")),
        "valid_to": _parse_dt((tomorrow or today or {}).get("end_validity_time")),
        "departments": result_departments,
    }


def parse_run_directories(html: str) -> list[str]:
    """Run directories (HHMMSS) of a day index page, newest first."""
    return sorted(set(_RUN_DIR_RE.findall(html or "")), reverse=True)


def current_levels(vigicrues: Optional[dict], meteofrance: Optional[dict]) -> dict[tuple[str, str, str], int]:
    """``(source, zone, phenomenon) -> level`` of every known colour."""
    levels: dict[tuple[str, str, str], int] = {}
    if vigicrues and vigicrues.get("available"):
        for t in vigicrues["troncons"]:
            if t["level"] is not None:
                levels[(SOURCE_VIGICRUES, t["code"], PHENOMENON_FLOOD)] = t["level"]
    if meteofrance and meteofrance.get("available"):
        for d in meteofrance["departments"]:
            for p in d["phenomena"]:
                if p["level"] is not None:
                    levels[(SOURCE_METEOFRANCE, d["code"], p["key"])] = p["level"]
    return levels


def points_to_write(
    levels: dict[tuple[str, str, str], int],
    stored: dict[tuple[str, str, str], tuple[int, datetime]],
    now: datetime,
    keepalive: timedelta = KEEPALIVE,
) -> list[tuple[str, str, str]]:
    """Keys whose level changed, was never written, or is due for a keepalive."""
    due = []
    for key, level in levels.items():
        previous = stored.get(key)
        if previous is None or previous[0] != level or now - previous[1] >= keepalive:
            due.append(key)
    return due


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def _record_failure(breaker: CircuitBreaker, what: str, e: Exception) -> None:
    if is_upstream_failure(e):
        breaker.record_failure(e)
    else:
        logger.warning("Error fetching %s: %s", what, describe_error(e))


async def fetch_vigicrues_vigilance(client: httpx.AsyncClient) -> Optional[dict]:
    """Current Vigicrues colours of the configured troncons (None on failure)."""
    if not _vigicrues_breaker.allow():
        return None
    try:
        response = await client.get(VIGICRUES_INFO_URL, timeout=EXTERNAL_TIMEOUT_S)
        response.raise_for_status()
        result = parse_vigicrues(
            response.json(), get_settings().vigilance_vigicrues_troncons, _utcnow(),
        )
    except Exception as e:
        _record_failure(_vigicrues_breaker, "Vigicrues vigilance", e)
        return None
    _vigicrues_breaker.record_success()
    return result


async def _mf_run_candidates(client: httpx.AsyncClient, now: datetime) -> list[str]:
    """``YYYY/MM/DD/HHMMSS`` run paths, newest first (today, then yesterday)."""
    runs: list[str] = []
    for day in (now, now - timedelta(days=1)):
        prefix = day.strftime("%Y/%m/%d")
        response = await client.get(f"{MF_MIRROR_BASE}/{prefix}/", timeout=EXTERNAL_TIMEOUT_S)
        if response.status_code == 404:
            continue
        response.raise_for_status()
        runs += [f"{prefix}/{run}" for run in parse_run_directories(response.text)]
        if len(runs) >= MF_MAX_RUNS_TRIED:
            break
    return runs[:MF_MAX_RUNS_TRIED]


async def fetch_meteofrance_vigilance(client: httpx.AsyncClient) -> Optional[dict]:
    """Current Meteo-France colours for the configured departements (None on failure).

    The newest run holding a map is parsed; when it is the run already
    parsed, the cached result is returned without downloading the map again.
    """
    global _mf_last_run
    if not _meteofrance_breaker.allow():
        return None
    settings = get_settings()
    now = _utcnow()
    try:
        for run in await _mf_run_candidates(client, now):
            if run == _mf_last_run:
                cached = _latest.get(SOURCE_METEOFRANCE)
                if cached is not None:
                    _meteofrance_breaker.record_success()
                    return {**cached, "fetched_at": now}
            response = await client.get(f"{MF_MIRROR_BASE}/{run}/{MF_MAP_FILE}", timeout=EXTERNAL_TIMEOUT_S)
            if response.status_code in (403, 404):
                continue  # text-only run: no map
            response.raise_for_status()
            result = parse_meteofrance(
                response.json(),
                settings.vigilance_meteofrance_departments,
                settings.vigilance_meteofrance_phenomena,
                now,
            )
            _mf_last_run = run
            _meteofrance_breaker.record_success()
            return result
        logger.warning("Meteo-France vigilance: no map in the latest runs")
        return None
    except Exception as e:
        _record_failure(_meteofrance_breaker, "Meteo-France vigilance", e)
        return None


async def _store_changes(vigicrues: Optional[dict], meteofrance: Optional[dict]) -> int:
    from app.services import influx_client

    now = _utcnow()
    levels = current_levels(vigicrues, meteofrance)
    due = points_to_write(levels, _stored, now)
    if not due:
        return 0
    points = [
        influx_client.make_point(
            measurement=MEASUREMENT,
            tags={"source": source, "zone": zone, "phenomenon": phenomenon},
            fields={"level": levels[(source, zone, phenomenon)]},
            timestamp=now,
        )
        for source, zone, phenomenon in due
    ]
    written = await influx_client.write_points_batch(points)
    if written == len(points):
        for key in due:
            _stored[key] = (levels[key], now)
    return written


async def collect_and_store_vigilance() -> dict:
    """Refresh both vigilance sources, update the cache, archive the changes."""
    async with httpx.AsyncClient(follow_redirects=True) as client:
        vigicrues = await fetch_vigicrues_vigilance(client)
        meteofrance = await fetch_meteofrance_vigilance(client)
    if vigicrues is not None:
        _latest.set(SOURCE_VIGICRUES, vigicrues)
    if meteofrance is not None:
        _latest.set(SOURCE_METEOFRANCE, meteofrance)
    written = await _store_changes(vigicrues, meteofrance)
    return {
        "vigicrues": vigicrues is not None and vigicrues.get("available", False),
        "meteofrance": meteofrance is not None and meteofrance.get("available", False),
        "points_written": written,
    }


def peek_vigilance() -> dict:
    """Last vigilance results, never calling the APIs (request paths).

    ``{"vigicrues": dict | None, "meteofrance": dict | None}``: None when no
    result younger than LATEST_MAX_AGE_S exists.
    """
    return {
        SOURCE_VIGICRUES: _latest.get(SOURCE_VIGICRUES),
        SOURCE_METEOFRANCE: _latest.get(SOURCE_METEOFRANCE),
    }


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


async def collect_vigilance_job():
    """Scheduler job: refresh the official vigilance colours."""
    try:
        result = await collect_and_store_vigilance()
        logger.info(
            "Collected vigilance (vigicrues=%s, meteofrance=%s, %s points written)",
            result["vigicrues"], result["meteofrance"], result["points_written"],
        )
    except Exception as e:
        logger.error("Error collecting vigilance: %r", e)


def schedule_vigilance(scheduler, first_run: datetime) -> None:
    """Register the vigilance job (every 30 min, first run at ``first_run``)."""
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler.add_job(
        collect_vigilance_job,
        trigger=IntervalTrigger(seconds=JOB_INTERVAL.total_seconds()),
        id=JOB_ID,
        name="Collect official vigilance (Vigicrues, Meteo-France)",
        next_run_time=first_run,
        replace_existing=True,
    )
