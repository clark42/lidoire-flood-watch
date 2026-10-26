"""Outlook: "do I need to act, and when?" over the next 6 / 24 / 72 hours.

Pure aggregation of the hourly forecast (forecast.py, model v3.4, no extra
scoring) plus the owner's action plan:

- :func:`summarize_horizons` (called by forecast.get_risk_forecast_v2, so
  the result is cached with the forecast): per horizon, the max score, its
  level and time, and the top contributing factors of the max hour,
  explained in plain French from its RiskBreakdown;
- :func:`build_outlook`: horizons + next estimated high water at the
  confluence + data-confidence notes + the checklist of the highest level
  (``app/data/action_plan.json``);
- :func:`get_outlook`: I/O wrapper used by ``GET /api/risk/outlook``
  (forecast cache, source freshness, Redis cache of the outlook).

Contribution points are the exact decomposition of the score:
weight x factor score / denominator x conjunction x dampener x 100 (scaled
down when the score is capped at 100), so they add up to the score.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from app.services.risk_core import WEIGHT_KEYS, RiskBreakdown, get_model_params, to_utc_naive

logger = logging.getLogger(__name__)

# (hours, label) of the outlook horizons
HORIZONS: tuple[tuple[int, str], ...] = ((6, "6 h"), (24, "24 h"), (72, "72 h"))
# Factors listed in the "why" of a horizon
TOP_DRIVERS = 3
# Contributions below this many points are not worth explaining
MIN_DRIVER_POINTS = 0.5

LEVEL_RANK = {"green": 0, "yellow": 1, "orange": 2, "red": 3}
LEVEL_FR = {"green": "vert", "yellow": "jaune", "orange": "orange", "red": "rouge"}

FACTOR_LABELS_FR = {
    "river": "Niveau des rivières",
    "rain_24h": "Pluie sur 24 h",
    "rain_72h": "Pluie sur 72 h",
    "rain_7d": "Pluie sur 7 jours",
    "tide": "Coefficient de marée",
    "tide_phase": "Pleine mer proche",
    "tendency": "Montée de Pessac",
    "momentum": "Stations en hausse",
    "wind_west": "Vent d'ouest",
    "soil_sat": "Sols saturés",
    "video": "Caméra",
}
UNAVAILABLE_FR = {
    "soil_sat": "saturation des sols",
    "rain_7d": "pluie sur 7 jours",
    "wind_west": "vent d'ouest",
    "video": "caméra",
}
DATA_SOURCE_FR = {
    "river": "niveaux des rivières",
    "rain": "prévision de pluie",
    "rain_past": "pluie observée (Open-Meteo)",
    "soil_wind": "sol et vent",
}
LONG_RANGE_NOTE = (
    "Au-delà de 24 h, la prévision repose surtout sur la pluie prévue et une "
    "projection simple des niveaux : à confirmer aux échéances suivantes."
)

# Owner-editable action plan: a copy on the backend data volume wins over
# the default shipped with the code.
ACTION_PLAN_PATH = Path(__file__).resolve().parent.parent / "data" / "action_plan.json"
ACTION_PLAN_OVERRIDE_PATH = Path("/app/data/action_plan.json")

# Redis cache of the outlook. The "forecast:" prefix makes
# forecast.invalidate_forecast_cache() drop it too.
OUTLOOK_CACHE_KEY = "forecast:outlook"
OUTLOOK_CACHE_TTL_S = 10 * 60
# Forecast length the outlook reads (same key as the dashboard and the archive)
OUTLOOK_FORECAST_HOURS = 168


def _num(value: float, decimals: int = 1) -> str:
    return f"{value:.{decimals}f}".replace(".", ",")


def _parse_ts(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return to_utc_naive(value)
    if isinstance(value, str):
        try:
            return to_utc_naive(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _iso(dt: datetime) -> str:
    return to_utc_naive(dt).replace(tzinfo=timezone.utc).isoformat()


def _rank(level: Optional[str]) -> int:
    return LEVEL_RANK.get(level or "green", 0)


# ---------------------------------------------------------------------------
# Why: exact contribution of each factor
# ---------------------------------------------------------------------------

def factor_scores(bd: RiskBreakdown) -> dict[str, float]:
    """Normalized score (0-1) of each weighted factor, as used in the sum."""
    return {
        "river": bd.river_score,
        "rain_24h": bd.rain_24h_score,
        "rain_72h": bd.rain_72h_score,
        "rain_7d": bd.rain_7d_score,
        "tide": bd.tide_score,
        "tide_phase": bd.tide_phase_score,
        "tendency": bd.tendency_score,
        "momentum": bd.momentum_score,
        "wind_west": bd.wind_west_score,
        "soil_sat": bd.soil_sat_score,
        "video": bd.video_score or 0.0,
    }


def factor_points(bd: RiskBreakdown) -> dict[str, float]:
    """Points of the 0-100 score brought by each available factor.

    weight x score / denominator x conjunction x dampener x 100; when the
    score is capped at 100 the points are scaled so they still sum to it.
    """
    if bd.total_weight <= 0:
        return {}
    scores = factor_scores(bd)
    scale = bd.conjunction_multiplier * bd.stability_dampener / bd.total_weight * 100
    points = {
        k: scores[k] * bd.weights.get(k, 0.0) * scale
        for k in WEIGHT_KEYS if k not in bd.unavailable
    }
    total = sum(points.values())
    if total > 100.0:
        points = {k: v * 100.0 / total for k, v in points.items()}
    return points


def _primary(bd: RiskBreakdown):
    return next((s for s in bd.stations if s.station.station_id == bd.primary_station_id), None)


def describe_factor(key: str, bd: RiskBreakdown) -> str:
    """Plain French explanation of one factor at the breakdown's hour."""
    primary = _primary(bd)
    if key == "river":
        if primary is None:
            return "Niveaux des stations amont"
        s = primary.station
        text = (
            f"{s.station_name} à {_num(s.height_m, 2)} m "
            f"(jaune {_num(s.thresholds[0], 1)} m, orange {_num(s.thresholds[1], 1)} m)"
        )
        if bd.n_above_orange:
            text += f", {bd.n_above_orange} station(s) au-dessus de leur seuil orange"
        return text
    if key == "tendency":
        rate = bd.primary_rate_cm_h
        name = primary.station.station_name if primary else "La station principale"
        if rate >= 0:
            return f"{name} monte de {_num(rate, 1)} cm/h"
        return f"{name} baisse de {_num(-rate, 1)} cm/h"
    if key == "momentum":
        return f"{bd.n_rising} station(s) en hausse sur {bd.n_active}"
    if key == "rain_24h":
        return f"{_num(bd.rain_24h_effective_mm, 0)} mm sur 24 h (observée + moitié de la prévision)"
    if key == "rain_72h":
        return f"{_num(bd.rain_72h_effective_mm, 0)} mm sur 72 h (observée + 30 % de la prévision)"
    if key == "rain_7d":
        return "Pluie abondante sur les 7 derniers jours"
    if key == "tide":
        text = f"Coefficient {bd.tide_coefficient}" if bd.tide_coefficient else "Marée"
        if bd.tide_gate < 0.999:
            text += f", effet réduit à {bd.tide_gate * 100:.0f} % (remous)"
        return text
    if key == "tide_phase":
        h = bd.hours_to_high_tide
        if h is None:
            return "Pleine mer proche à la confluence"
        if h >= 0:
            return f"Pleine mer à la confluence dans {_num(h, 1)} h"
        return f"Pleine mer à la confluence il y a {_num(-h, 1)} h"
    if key == "soil_sat":
        return f"Sols saturés (facteur {_num(bd.soil_saturation_factor, 2)} sur 1,5)"
    if key == "wind_west":
        return "Vent d'ouest soutenu (freine l'écoulement)"
    if key == "video":
        return "Niveau d'eau détecté par la caméra"
    return FACTOR_LABELS_FR.get(key, key)


def explain_drivers(bd: RiskBreakdown, top: int = TOP_DRIVERS) -> list[dict]:
    """Top contributing factors of an hour, largest first."""
    points = factor_points(bd)
    ranked = sorted(
        ((k, v) for k, v in points.items() if v >= MIN_DRIVER_POINTS),
        key=lambda kv: kv[1], reverse=True,
    )[:top]
    return [
        {
            "factor": k,
            "label": FACTOR_LABELS_FR.get(k, k),
            "points": round(v, 1),
            "text": describe_factor(k, bd),
        }
        for k, v in ranked
    ]


def explain_modifiers(bd: RiskBreakdown) -> list[str]:
    """Multipliers and gates that moved the score, in plain French."""
    out = []
    if bd.conjunction_multiplier > 1.0 + 1e-9:
        out.append(
            f"Plusieurs signaux simultanés : score multiplié par {_num(bd.conjunction_multiplier, 2)}"
        )
    if bd.stability_dampener < 1.0 - 1e-9:
        out.append(
            f"Niveaux stables et peu de pluie : score réduit (x{_num(bd.stability_dampener, 2)})"
        )
    if bd.tide_available and bd.tide_gate < 0.999 and bd.backwater_level_m is not None:
        full = get_model_params().tide_gate_thresholds[1]
        out.append(
            f"Effet de la marée réduit à {bd.tide_gate * 100:.0f} % : remous limité "
            f"(Pessac {_num(bd.backwater_level_m, 2)} m sur 12,5 h, plein effet à {_num(full, 1)} m)"
        )
    return out


def _horizon_summary_text(level: str, score: float, drivers: Sequence[dict]) -> str:
    if level == "green" and not drivers:
        return f"Risque vert (max {_num(score, 0)}/100) : aucun facteur notable."
    text = f"Risque {LEVEL_FR.get(level, level)} (max {_num(score, 0)}/100)"
    if drivers:
        text += ", surtout : " + ", ".join(d["label"].lower() for d in drivers)
    return text + "."


def summarize_horizons(
    forecasts: Sequence[Mapping],
    breakdowns: Sequence[RiskBreakdown],
    horizons: Sequence[tuple[int, str]] = HORIZONS,
) -> list[dict]:
    """Max score, level, time and why for each horizon (pure, JSON-ready).

    ``forecasts`` are forecast.build_forecast entries, ``breakdowns`` their
    RiskBreakdown in the same order. A horizon covers H+0..H+hours; ties
    keep the earliest hour. ``first_at_level`` is the first hour at or above
    the horizon's level (when to be ready).
    """
    out = []
    pairs = list(zip(forecasts, breakdowns))
    for hours, label in horizons:
        window = [(f, bd) for f, bd in pairs if f["hours_ahead"] <= hours]
        if not window:
            continue
        best_f, best_bd = window[0]
        for f, bd in window[1:]:
            if f["score"] > best_f["score"]:
                best_f, best_bd = f, bd
        level = best_f["level"]
        first = next(f for f, _ in window if _rank(f["level"]) >= _rank(level))
        drivers = explain_drivers(best_bd)
        out.append({
            "hours": hours,
            "label": label,
            "max_score": round(float(best_f["score"]), 1),
            "level": level,
            "max_at": best_f["timestamp"],
            "max_hours_ahead": best_f["hours_ahead"],
            "first_at_level": first["timestamp"],
            "drivers": drivers,
            "modifiers": explain_modifiers(best_bd),
            "unavailable": list(best_bd.unavailable),
            "summary": _horizon_summary_text(level, float(best_f["score"]), drivers),
        })
    return out


# ---------------------------------------------------------------------------
# Action plan
# ---------------------------------------------------------------------------

def load_action_plan(paths: Optional[Sequence[Path]] = None) -> dict:
    """Owner action plan: the first readable, valid file of ``paths``.

    Default order: the copy on the data volume (/app/data/action_plan.json),
    then the default shipped in app/data. Invalid files are logged and
    skipped; returns {"levels": {}} when none is usable.
    """
    for path in paths or (ACTION_PLAN_OVERRIDE_PATH, ACTION_PLAN_PATH):
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            levels = data.get("levels")
            if not isinstance(levels, dict):
                raise ValueError("missing 'levels' object")
            for name, entry in levels.items():
                if name not in LEVEL_RANK or not isinstance(entry, dict):
                    raise ValueError(f"invalid level {name!r}")
                actions = entry.get("actions", [])
                if not isinstance(actions, list) or not all(isinstance(a, str) for a in actions):
                    raise ValueError(f"level {name!r}: 'actions' must be a list of strings")
            return data
        except Exception as e:
            logger.error("Action plan %s ignored: %s", path, e)
    return {"levels": {}}


def checklist_for(level: str, plan: Mapping, act_before: Optional[str]) -> dict:
    """Checklist of one level (empty for green or a level missing from the plan)."""
    entry = (plan.get("levels") or {}).get(level) if level != "green" else None
    if not entry:
        return {
            "level": level,
            "title": "Aucune action particulière" if level == "green" else f"Niveau {LEVEL_FR.get(level, level)}",
            "actions": [],
            "act_before": None,
        }
    return {
        "level": level,
        "title": entry.get("title") or f"Niveau {LEVEL_FR.get(level, level)}",
        "actions": list(entry.get("actions", [])),
        "act_before": act_before,
    }


# ---------------------------------------------------------------------------
# Outlook
# ---------------------------------------------------------------------------

def next_high_tide(high_tides: Sequence[Mapping], now: datetime) -> Optional[dict]:
    """First estimated high water at the confluence after ``now``."""
    for ht in sorted(high_tides, key=lambda x: str(x.get("timestamp"))):
        ts = _parse_ts(ht.get("timestamp"))
        if ts is not None and ts > now:
            return {
                "timestamp": _iso(ts),
                "coefficient": ht.get("coefficient"),
                "estimated": bool(ht.get("estimated", False)),
                "hours_from_now": round((ts - now).total_seconds() / 3600, 1),
            }
    return None


def confidence_notes(
    forecast: Mapping,
    horizons: Sequence[Mapping],
    sources: Optional[Mapping[str, Mapping]],
) -> dict:
    """Data-confidence notes: unavailable factors, estimated tide, stale sources.

    ``level``: "low" when a critical source (Pessac, realtime score) or the
    river / rain forecast inputs are missing, "reduced" when anything else
    is degraded, "good" otherwise. The long-range caveat is always listed.
    """
    notes: list[str] = []
    severity = 0
    inputs = forecast.get("inputs_summary") or {}

    unavailable: list[str] = []
    for h in horizons:
        for key in h.get("unavailable", []):
            if key not in unavailable:
                unavailable.append(key)
    if unavailable:
        notes.append(
            "Facteurs indisponibles, exclus du calcul : "
            + ", ".join(UNAVAILABLE_FR.get(k, k) for k in unavailable) + "."
        )
        severity = max(severity, 1)

    tide_estimated = inputs.get("tide_reliable") is False or (
        (forecast.get("data_sources") or {}).get("tide", {}).get("status") in ("estimated", "error")
    )
    if tide_estimated:
        notes.append("Marées estimées (pas de prédiction fiable) : facteurs marée mis à 0.")
        severity = max(severity, 1)

    for key, label in DATA_SOURCE_FR.items():
        status = ((forecast.get("data_sources") or {}).get(key) or {}).get("status")
        if status in ("error", "empty"):
            notes.append(f"Donnée de prévision manquante : {label}.")
            severity = max(severity, 2 if key in ("river", "rain") else 1)

    stale: list[str] = []
    for key, entry in (sources or {}).items():
        status = entry.get("status")
        if status in (None, "ok"):
            continue
        label = entry.get("label", key)
        stale.append(key)
        if status == "stale":
            age = entry.get("age_hours")
            detail = f"dernier point il y a {_num(age, 1)} h" if isinstance(age, (int, float)) else "périmée"
        elif status == "unavailable":
            detail = "aucune donnée récente"
        else:
            detail = "fraîcheur inconnue"
        if entry.get("note"):
            detail += f" ({entry['note']})"
        notes.append(f"Source {label} : {detail}.")
        severity = max(severity, 2 if entry.get("critical") else 1)

    notes.append(LONG_RANGE_NOTE)
    return {
        "level": ("good", "reduced", "low")[severity],
        "notes": notes,
        "unavailable_factors": unavailable,
        "tide_estimated": tide_estimated,
        "stale_sources": stale,
    }


def build_outlook(
    forecast: Mapping,
    plan: Mapping,
    now: datetime,
    sources: Optional[Mapping[str, Mapping]] = None,
) -> dict:
    """Outlook payload from a forecast result (pure, JSON-ready)."""
    horizons = list(forecast.get("horizons") or [])
    highest = max(horizons, key=lambda h: _rank(h["level"]), default=None)
    highest_level = highest["level"] if highest else "green"
    act_before = highest["first_at_level"] if highest and highest_level != "green" else None
    return {
        "generated_at": _iso(now),
        "forecast_generated_at": forecast.get("generated_at") or _iso(now),
        "model_version": forecast.get("model_version"),
        "horizons": horizons,
        "highest_level": highest_level,
        "next_high_tide": next_high_tide(forecast.get("high_tides") or [], now),
        "confidence": confidence_notes(forecast, horizons, sources),
        "checklist": checklist_for(highest_level, plan, act_before),
        "inputs_summary": forecast.get("inputs_summary") or {},
    }


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _get_cached_outlook() -> Optional[dict]:
    try:
        from app.services.cache import get_redis

        raw = await get_redis().get(OUTLOOK_CACHE_KEY)
        return json.loads(raw) if raw else None
    except Exception as e:
        logger.warning("Outlook cache read error: %s", e)
        return None


async def _set_cached_outlook(data: dict) -> None:
    try:
        from app.services.cache import get_redis

        await get_redis().set(OUTLOOK_CACHE_KEY, json.dumps(data, default=str), ex=OUTLOOK_CACHE_TTL_S)
    except Exception as e:
        logger.warning("Outlook cache write error: %s", e)


async def invalidate_outlook_cache() -> None:
    try:
        from app.services.cache import get_redis

        await get_redis().delete(OUTLOOK_CACHE_KEY)
    except Exception as e:
        logger.warning("Outlook cache invalidate error: %s", e)


async def _source_freshness() -> Optional[dict]:
    try:
        from app.api.status import check_sources

        return await check_sources()
    except Exception as e:
        logger.warning("Outlook: source freshness unavailable: %r", e)
        return None


async def get_outlook() -> dict:
    """Outlook for ``GET /api/risk/outlook`` (Redis cache, 10 min). Never stores scores."""
    cached = await _get_cached_outlook()
    if cached:
        return cached
    from app.services.forecast import get_risk_forecast_v2

    forecast = await get_risk_forecast_v2(hours=OUTLOOK_FORECAST_HOURS)
    sources = await _source_freshness()
    result = build_outlook(forecast, load_action_plan(), _utcnow(), sources)
    await _set_cached_outlook(result)
    return result
