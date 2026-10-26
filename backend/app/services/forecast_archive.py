"""Forecast archive (PostgreSQL ``forecast_runs``) and forecast-vs-actual skill.

Prospective verification of a frozen model is the only evidence that is not
in-sample: every hour, right after ``risk_scorer``, the ``forecast_archiver``
job recomputes the 168 h forecast (forecast.get_risk_forecast_v2 with
``use_cache=False``, same v3.4 path as /api/risk/forecast, which it also
refreshes) and stores one row: run time, model version, hourly scores,
levels and projected Pessac levels, and the key inputs (forecast rain
24/72 h, projected Pessac max, tide gate). Read routes never write.

Retention: everything is kept. A row is ~3-4 KB (3 lists of 169 values +
a small inputs object), i.e. ~85 KB a day and ~30 MB a year: negligible
next to the 5 Gi Postgres volume, and multi-season verification (one
season = 5 floods at best) needs the whole history. Revisit only if the
horizon or the number of archived models grows by an order of magnitude.

Skill (``GET /api/risk/forecast-skill``): for each archived run and lead L,
the forecast score at H+L is paired with the stored realtime score
(``risk_score``, source=realtime, same model version) nearest to
run_at + L, within PAIR_TOLERANCE. Hourly pairs, not events: MAE, bias and a
hit / miss / false-alarm table at the orange threshold.
"""

from __future__ import annotations

import bisect
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Mapping, Optional, Protocol, Sequence

from app.services.risk_core import MODEL_VERSION, get_model_params, to_utc_naive

logger = logging.getLogger(__name__)

JOB_ID = "forecast_archiver"
# Delay after the risk_scorer run (the forecast H+0 uses the same inputs)
JOB_OFFSET = timedelta(minutes=2)
# Archived horizon (same as the dashboard forecast: one shared cache entry)
ARCHIVE_HOURS = 168
DEFAULT_LEADS = (6, 24, 72)
MAX_LEADS = 10
# A realtime score is paired with run_at + lead when this close
PAIR_TOLERANCE = timedelta(minutes=35)
SKILL_MAX_DAYS = 180


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_ts(value) -> datetime:
    if isinstance(value, datetime):
        return to_utc_naive(value)
    return to_utc_naive(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------

def run_row_from_forecast(forecast: Mapping) -> dict:
    """Column values of a ``forecast_runs`` row from a forecast result (pure).

    Lists are indexed by lead hour (entries sorted by ``hours_ahead``).
    """
    entries = sorted(forecast.get("forecasts") or [], key=lambda f: f["hours_ahead"])
    if not entries:
        raise ValueError("forecast has no hourly entries")
    inputs = dict(forecast.get("inputs_summary") or {})
    sources = forecast.get("data_sources") or {}
    inputs["data_sources"] = {
        k: v.get("status") for k, v in sources.items() if isinstance(v, Mapping) and "status" in v
    }
    return {
        "run_at": _parse_ts(forecast["generated_at"]),
        "model_version": str(forecast.get("model_version") or MODEL_VERSION),
        "horizon_hours": int(entries[-1]["hours_ahead"]),
        "scores": [round(float(f["score"]), 1) for f in entries],
        "levels": [f["level"] for f in entries],
        "pessac_levels_m": [
            round(float(f["projected_level_m"]), 2) if f.get("projected_level_m") is not None else None
            for f in entries
        ],
        "rain_forecast_24h_mm": inputs.get("rain_forecast_24h_mm"),
        "rain_forecast_72h_mm": inputs.get("rain_forecast_72h_mm"),
        "pessac_projected_max_m": inputs.get("pessac_projected_max_m"),
        "tide_gate": inputs.get("tide_gate"),
        "inputs": inputs,
    }


class ForecastRunStore(Protocol):
    async def save(self, row: dict) -> bool: ...

    async def list_runs(self, start: datetime, end: datetime, model_version: str) -> list[dict]: ...


_ROW_FIELDS = (
    "run_at", "model_version", "horizon_hours", "scores", "levels", "pessac_levels_m",
    "rain_forecast_24h_mm", "rain_forecast_72h_mm", "pessac_projected_max_m", "tide_gate", "inputs",
)


class PostgresForecastRunStore:
    """``forecast_runs`` table (created by init_db like the other tables)."""

    async def save(self, row: dict) -> bool:
        """Insert a run; False when this (run_at, model_version) already exists."""
        from sqlalchemy.exc import IntegrityError

        from app.db import get_session_factory
        from app.models.orm import ForecastRunRow

        try:
            async with get_session_factory()() as session:
                async with session.begin():
                    session.add(ForecastRunRow(**{k: row[k] for k in _ROW_FIELDS}))
            return True
        except IntegrityError:
            logger.info("forecast archive: run %s already stored", row["run_at"])
            return False

    async def list_runs(self, start: datetime, end: datetime, model_version: str) -> list[dict]:
        from sqlalchemy import select

        from app.db import get_session_factory
        from app.models.orm import ForecastRunRow

        async with get_session_factory()() as session:
            result = await session.execute(
                select(ForecastRunRow)
                .where(ForecastRunRow.run_at >= start, ForecastRunRow.run_at <= end)
                .where(ForecastRunRow.model_version == model_version)
                .order_by(ForecastRunRow.run_at)
            )
            return [{k: getattr(r, k) for k in _ROW_FIELDS} for r in result.scalars()]


async def _default_forecast() -> dict:
    from app.services.forecast import get_risk_forecast_v2

    return await get_risk_forecast_v2(hours=ARCHIVE_HOURS, use_cache=False)


async def archive_forecast_run(
    store: Optional[ForecastRunStore] = None,
    forecast_fn: Optional[Callable[[], Awaitable[dict]]] = None,
) -> Optional[dict]:
    """Compute a fresh forecast and archive it. Returns the stored row (None if duplicate)."""
    store = store or PostgresForecastRunStore()
    forecast = await (forecast_fn or _default_forecast)()
    row = run_row_from_forecast(forecast)
    saved = await store.save(row)
    try:
        from app.services.outlook import invalidate_outlook_cache

        await invalidate_outlook_cache()
    except Exception as e:  # cache is optional
        logger.warning("forecast archive: outlook cache not invalidated: %s", e)
    return row if saved else None


async def forecast_archive_job() -> None:
    """Scheduler job: one archived run per hour (failures never affect scoring)."""
    try:
        row = await archive_forecast_run()
        if row:
            logger.info(
                "Forecast run archived: %s, model %s, %d h, max 72h %.1f",
                row["run_at"].isoformat(), row["model_version"], row["horizon_hours"],
                max(row["scores"][:73]),
            )
    except Exception as e:
        logger.error("Error archiving forecast run: %r", e)


def schedule_forecast_archive(scheduler, first_run: datetime) -> None:
    """Register the hourly archive job, ``JOB_OFFSET`` after the risk_scorer runs."""
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler.add_job(
        forecast_archive_job,
        trigger=IntervalTrigger(hours=1),
        id=JOB_ID,
        name="Archive the hourly forecast run",
        next_run_time=first_run + JOB_OFFSET,
        replace_existing=True,
    )


# ---------------------------------------------------------------------------
# Forecast vs actual
# ---------------------------------------------------------------------------

def pair_forecasts(
    runs: Sequence[Mapping],
    actuals: Sequence[tuple[datetime, float]],
    leads: Sequence[int],
    tolerance: timedelta = PAIR_TOLERANCE,
) -> list[dict]:
    """Pair forecast score at H+lead with the realized score nearest to run_at + lead.

    ``actuals``: (naive UTC time, score) of stored realtime scores. A pair is
    kept only when an actual lies within ``tolerance`` of the target time.
    """
    points = sorted((to_utc_naive(t), float(s)) for t, s in actuals)
    times = [t for t, _ in points]
    pairs = []
    for run in runs:
        run_at = to_utc_naive(run["run_at"])
        scores = run.get("scores") or []
        for lead in leads:
            if lead >= len(scores) or scores[lead] is None:
                continue
            target = run_at + timedelta(hours=lead)
            i = bisect.bisect_left(times, target)
            best = None
            for j in (i - 1, i):
                if 0 <= j < len(points):
                    gap = abs(times[j] - target)
                    if gap <= tolerance and (best is None or gap < best[0]):
                        best = (gap, points[j])
            if best is None:
                continue
            actual_at, actual = best[1]
            pairs.append({
                "run_at": run_at,
                "lead_hours": lead,
                "valid_at": target,
                "forecast_score": float(scores[lead]),
                "actual_score": actual,
                "actual_at": actual_at,
            })
    return pairs


def _ratio(num: int, den: int) -> Optional[float]:
    return round(num / den, 3) if den else None


def skill_by_lead(pairs: Sequence[Mapping], leads: Sequence[int], threshold: float) -> list[dict]:
    """MAE, bias and the contingency table at ``threshold`` for each lead (pure)."""
    out = []
    for lead in leads:
        ps = [p for p in pairs if p["lead_hours"] == lead]
        errors = [p["forecast_score"] - p["actual_score"] for p in ps]
        hits = sum(1 for p in ps if p["forecast_score"] >= threshold and p["actual_score"] >= threshold)
        misses = sum(1 for p in ps if p["forecast_score"] < threshold and p["actual_score"] >= threshold)
        false_alarms = sum(1 for p in ps if p["forecast_score"] >= threshold and p["actual_score"] < threshold)
        out.append({
            "lead_hours": lead,
            "n": len(ps),
            "mae": round(sum(abs(e) for e in errors) / len(errors), 2) if errors else None,
            "bias": round(sum(errors) / len(errors), 2) if errors else None,
            "hits": hits,
            "misses": misses,
            "false_alarms": false_alarms,
            "correct_negatives": len(ps) - hits - misses - false_alarms,
            "pod": _ratio(hits, hits + misses),
            "far": _ratio(false_alarms, hits + false_alarms),
            "csi": _ratio(hits, hits + misses + false_alarms),
        })
    return out


def parse_leads(raw: str) -> list[int]:
    """"6,24,72" -> [6, 24, 72] (unique, sorted). ValueError on bad input."""
    leads = sorted({int(x) for x in raw.split(",") if x.strip()})
    if not leads:
        raise ValueError("at least one lead is required")
    if len(leads) > MAX_LEADS:
        raise ValueError(f"at most {MAX_LEADS} leads")
    if leads[0] < 0 or leads[-1] > ARCHIVE_HOURS:
        raise ValueError(f"leads must be between 0 and {ARCHIVE_HOURS} h")
    return leads


async def _default_actuals(start: datetime, end: datetime, model_version: str) -> list[tuple[datetime, float]]:
    """Stored realtime scores (risk_score, source=realtime or untagged) of a version."""
    from app.services import influx_client

    rows = await influx_client.query_risk_score_values(start, end, model_version=model_version)
    return [
        (to_utc_naive(r["timestamp"]), float(r["score"]))
        for r in rows
        if r.get("source") in (influx_client.RISK_SOURCE_REALTIME, None)
        and r.get("model_version") in (model_version, None)
    ]


async def get_forecast_skill(
    leads: Sequence[int] = DEFAULT_LEADS,
    days: int = 30,
    model_version: str = MODEL_VERSION,
    store: Optional[ForecastRunStore] = None,
    actuals_fn: Optional[Callable[[datetime, datetime, str], Awaitable[list]]] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Forecast-vs-actual pairs and scores over the last ``days`` days of runs."""
    store = store or PostgresForecastRunStore()
    actuals_fn = actuals_fn or _default_actuals
    now = now or _utcnow()
    threshold = float(get_model_params().level_thresholds[1])
    runs = await store.list_runs(now - timedelta(days=days), now, model_version)
    pairs: list[dict] = []
    notes: list[str] = []
    if runs:
        start = to_utc_naive(runs[0]["run_at"]) - PAIR_TOLERANCE
        actuals = await actuals_fn(start, now + PAIR_TOLERANCE, model_version)
        pairs = pair_forecasts(runs, actuals, leads)
    else:
        notes.append("Aucune prévision archivée sur la période : l'archive se remplit d'un run par heure.")
    if runs and not pairs:
        notes.append("Runs archivés, mais aucune échéance encore atteinte avec un score temps réel stocké.")
    return {
        "generated_at": now,
        "model_version": model_version,
        "threshold": threshold,
        "threshold_level": "orange",
        "days": days,
        "lead_hours": list(leads),
        "tolerance_minutes": int(PAIR_TOLERANCE.total_seconds() // 60),
        "n_runs": len(runs),
        "first_run_at": to_utc_naive(runs[0]["run_at"]) if runs else None,
        "last_run_at": to_utc_naive(runs[-1]["run_at"]) if runs else None,
        "by_lead": skill_by_lead(pairs, leads, threshold),
        "pairs": pairs,
        "notes": notes,
    }
