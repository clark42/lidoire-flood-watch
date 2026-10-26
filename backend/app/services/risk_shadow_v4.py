"""Shadow model "4.0a": v4-alpha (``risk_v4``, variant ``v4a_joint``) on stored series.

The ONLY production module allowed to import :mod:`app.services.risk_v4`
(enforced by a test). The score is stored next to v3.4 in the dedicated
``risk_score_shadow`` measurement (tag ``model_version=4.0a``): it never
feeds the displayed score, the API, the forecasts, the episode detection,
the TimeMachine or the notifier.

Inputs (:class:`risk_v4.V4Features`), same causal semantics as the offline
candidate ``v4a_joint`` (``scripts/eval/dataset.py`` + ``V4AlphaModel``):

- backwater H_aval = max of Pessac over the trailing 12.5 h (points <= t),
  the ``backwater_level_m`` of the production v3.4 features
  (``risk_core.backwater_level``); also the overbank input;
- runoff Q = max of the Eyraud discharge (``river_flow``, key EYRAUD) over
  12.5 h ending at the publication time t - 7 h. History (backfill) has no
  publication time, so the 7 h lag is applied: window [t - 19.5 h, t - 7 h].
  In realtime the lag is real: every point STORED at t is used, window
  [t - 19.5 h, t] (identical to the offline window when Hub'Eau publishes
  with 7 h of latency, fresher when it publishes sooner). Daily QmnJ
  fallback points (``hubeau-elab-qmnj``, stamped at day 00:00) are moved to
  D+1 00:00, as the offline daily fallback, so a day is never used early;
- groundwater = Mouliets piezometer daily level (``groundwater``, key
  MOULIETS; daily ``ades-daily`` value, else the mean of the hourly points
  of that day), day D used from D+1 12:00, ignored after 3 days. Recorded
  but OFF in ``v4a_joint`` (``use_groundwater=False``).

A missing input follows risk_v4's defaults (mechanism unavailable, score
from the remaining branches, None when no branch is available: not
stored); unavailability is stored in ``factor_<mechanism>_available``.
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Optional, Sequence

from app.services import risk_v4
from app.services.risk_core import BACKWATER_WINDOW_H, to_utc_naive

logger = logging.getLogger(__name__)

SHADOW_V4_MODEL_VERSION = risk_v4.MODEL_VERSION_V4  # "4.0a", no suffix: see is_shadow_risk_version
SHADOW_V4_VARIANT = "v4a_joint"

FLOW_MEASUREMENT = "river_flow"
GROUNDWATER_MEASUREMENT = "groundwater"
EYRAUD_KEY = "EYRAUD"
GROUNDWATER_KEY = "MOULIETS"
# Daily QmnJ fallback of the flow backfill (collectors/hydro_indicators.py)
SOURCE_FLOW_DAILY = "hubeau-elab-qmnj"
SOURCE_GROUNDWATER_DAILY = "ades-daily"

# Same constants as scripts/eval/dataset.py (HYDRO_Q_SERIES, ROLLING_WINDOW_H,
# PIEZO_LAG_AFTER_DAY_H, DAILY_INPUT_MAX_AGE_DAYS; equivalence test)
EYRAUD_PUBLICATION_LAG_H = 7.0
RUNOFF_WINDOW_H = BACKWATER_WINDOW_H  # 12.5 h
PIEZO_LAG_AFTER_DAY_H = 12.0
DAILY_INPUT_MAX_AGE_DAYS = 3

# Extra history loaded before t (with margins for the exact window cuts; +1 day
# for the daily QmnJ points stored at D 00:00 and used from D+1 00:00)
EYRAUD_LOOKBACK = timedelta(hours=EYRAUD_PUBLICATION_LAG_H + RUNOFF_WINDOW_H + 1.0 + 24.0)
GROUNDWATER_LOOKBACK = timedelta(days=DAILY_INPUT_MAX_AGE_DAYS + 3)

Point_ = tuple[datetime, float]


def shadow_v4_params() -> risk_v4.V4Params:
    """Parameters of ``v4a_joint``: pre-registered defaults (Eyraud runoff, overbank on, groundwater off)."""
    return risk_v4.default_v4_params()


# ---------------------------------------------------------------------------
# Pure feature builder
# ---------------------------------------------------------------------------

def runoff_q_at(
    points: Sequence[Point_],
    t: datetime,
    realtime: bool,
    lag_h: float = EYRAUD_PUBLICATION_LAG_H,
    window_h: float = RUNOFF_WINDOW_H,
) -> Optional[float]:
    """Max Eyraud Q over [t - lag - window, t - lag] (history) or [t - lag - window, t] (realtime).

    ``points`` sorted (naive UTC, m3/s); both bounds inclusive, as the
    offline ``_window_max``. None without a point in the window.
    """
    t = to_utc_naive(t)
    lo_t = t - timedelta(hours=lag_h + window_h)
    hi_t = t if realtime else t - timedelta(hours=lag_h)
    lo = bisect.bisect_left(points, (lo_t, float("-inf")))
    hi = bisect.bisect_right(points, (hi_t, float("inf")))
    window = points[lo:hi]
    return max(q for _, q in window) if window else None


def daily_value_at(
    daily: Sequence[tuple[date, float]],
    t: datetime,
    lag_after_day_h: float = PIEZO_LAG_AFTER_DAY_H,
    max_age_days: int = DAILY_INPUT_MAX_AGE_DAYS,
) -> Optional[float]:
    """Value of the last day D published at t (D+1 00:00 + lag <= t), None if older than ``max_age_days``."""
    if not daily:
        return None
    last_day = (to_utc_naive(t) - timedelta(hours=lag_after_day_h) - timedelta(days=1)).date()
    i = bisect.bisect_right(daily, (last_day, float("inf")))
    if i == 0:
        return None
    d, v = daily[i - 1]
    return v if (last_day - d).days <= max_age_days else None


def eyraud_points_from_rows(rows: Iterable[dict]) -> list[Point_]:
    """Sorted (time, Q) from stored rows {timestamp, value, source}; daily QmnJ moved to D+1 00:00."""
    by_ts: dict[datetime, float] = {}
    for r in rows:
        ts, q = r.get("timestamp"), r.get("value")
        if ts is None or q is None:
            continue
        ts = to_utc_naive(ts)
        if r.get("source") == SOURCE_FLOW_DAILY:
            ts = datetime(ts.year, ts.month, ts.day) + timedelta(days=1)
        by_ts[ts] = max(float(q), by_ts.get(ts, float("-inf")))
    return sorted(by_ts.items())


def groundwater_daily_from_rows(rows: Iterable[dict]) -> list[tuple[date, float]]:
    """Daily level (m NGF): the ``ades-daily`` value of the day, else the mean of its hourly points."""
    daily: dict[date, list[float]] = {}
    other: dict[date, list[float]] = {}
    for r in rows:
        ts, v = r.get("timestamp"), r.get("value")
        if ts is None or v is None:
            continue
        d = to_utc_naive(ts).date()
        (daily if r.get("source") == SOURCE_GROUNDWATER_DAILY else other).setdefault(d, []).append(float(v))
    out = {d: sum(v) / len(v) for d, v in other.items()}
    out.update({d: sum(v) / len(v) for d, v in daily.items()})
    return sorted(out.items())


def build_v4_features(
    t: datetime,
    backwater_level_m: Optional[float],
    eyraud_points: Sequence[Point_],
    groundwater_daily: Sequence[tuple[date, float]],
    realtime: bool,
) -> risk_v4.V4Features:
    """V4Features at t (pure). ``backwater_level_m`` = production H_aval (Pessac 12.5 h max)."""
    return risk_v4.V4Features(
        backwater_level_m=backwater_level_m,
        runoff_q_m3s=runoff_q_at(eyraud_points, t, realtime=realtime),
        groundwater_ngf=daily_value_at(groundwater_daily, t),
    )


def compute_shadow_v4(features: risk_v4.V4Features) -> risk_v4.V4Breakdown:
    return risk_v4.compute_risk_v4(features, shadow_v4_params())


def shadow_v4_fields(features: risk_v4.V4Features, bd: risk_v4.V4Breakdown) -> dict[str, float]:
    """``factor_*`` fields: mechanism scores, availability, inputs and the joint branch."""
    out: dict[str, float] = {}
    for name, m in bd.mechanisms.items():
        if m.score is not None:
            out[name] = m.score
        out[f"{name}_available"] = 1.0 if m.available else 0.0
    if bd.joint_score is not None:
        out["joint"] = bd.joint_score
    inputs = {
        "h_aval_m": features.backwater_level_m,
        "q_eyraud_m3s": features.runoff_q_m3s,
        "piezo_ngf": features.groundwater_ngf,
    }
    out.update({k: float(v) for k, v in inputs.items() if v is not None})
    return out


def shadow_v4_point(
    timestamp: datetime,
    features: risk_v4.V4Features,
    source: str,
):
    """``risk_score_shadow`` point (tag ``model_version=4.0a``), None when not scored."""
    from app.services import influx_client

    bd = compute_shadow_v4(features)
    if bd.score is None:
        return None
    point = influx_client.make_risk_point(
        score=bd.score,
        level=bd.level.value,
        timestamp=timestamp,
        factors=shadow_v4_fields(features, bd),
        model_version=SHADOW_V4_MODEL_VERSION,
        source=source,
        measurement=influx_client.RISK_SHADOW_MEASUREMENT,
    )
    return point.field("dominant", str(bd.dominant))


# ---------------------------------------------------------------------------
# Stored series (I/O)
# ---------------------------------------------------------------------------

async def _query_indicator(measurement: str, key: str, value_field: str,
                           start: datetime, end: datetime) -> list[dict]:
    """Raw points {timestamp, value, source} of one indicator series in [start, end). Raises."""
    from app.services import influx_client

    query = f'''
    from(bucket: {influx_client._bucket()})
        |> range(start: {influx_client._flux_ts(start)}, stop: {influx_client._flux_ts(end)})
        |> filter(fn: (r) => r._measurement == {influx_client.flux_string(measurement)})
        |> filter(fn: (r) => r._field == {influx_client.flux_string(value_field)})
        |> filter(fn: (r) => r.key == {influx_client.flux_string(key)})
        |> keep(columns: ["_time", "_value", "source"])
    '''
    tables = await influx_client._run_query(query)
    return [
        {"timestamp": rec.get_time(), "value": rec.get_value(), "source": rec.values.get("source")}
        for table in tables for rec in table.records
        if rec.get_value() is not None
    ]


async def load_eyraud_rows(start: datetime, end: datetime) -> list[dict]:
    return await _query_indicator(FLOW_MEASUREMENT, EYRAUD_KEY, "q_m3s", start, end)


async def load_groundwater_rows(start: datetime, end: datetime) -> list[dict]:
    return await _query_indicator(GROUNDWATER_MEASUREMENT, GROUNDWATER_KEY, "level_m_ngf", start, end)


@dataclass
class V4History:
    eyraud: list[Point_] = field(default_factory=list)
    groundwater: list[tuple[date, float]] = field(default_factory=list)


async def load_history(first: datetime, last: datetime) -> V4History:
    """Stored inputs to score every hour of [first, last] (raises on a query error)."""
    first, last = to_utc_naive(first), to_utc_naive(last)
    end = last + timedelta(hours=1)
    eyraud = eyraud_points_from_rows(await load_eyraud_rows(first - EYRAUD_LOOKBACK, end))
    gw = groundwater_daily_from_rows(await load_groundwater_rows(first - GROUNDWATER_LOOKBACK, end))
    return V4History(eyraud, gw)


def history_point(t: datetime, backwater_level_m: Optional[float], history: V4History, source: str):
    """Backfill point at t (publication lag applied to the Eyraud history)."""
    features = build_v4_features(t, backwater_level_m, history.eyraud, history.groundwater, realtime=False)
    return shadow_v4_point(t, features, source)


async def store_realtime(timestamp: datetime, backwater_level_m: Optional[float]) -> bool:
    """Compute and store the realtime 4.0a score (never raises).

    A query error skips the point (no score from a half-read store); an
    empty series only makes its mechanism unavailable.
    """
    from app.services import influx_client

    try:
        t = to_utc_naive(timestamp)
        end = t + timedelta(minutes=1)
        eyraud = eyraud_points_from_rows(await load_eyraud_rows(t - EYRAUD_LOOKBACK, end))
        gw = groundwater_daily_from_rows(await load_groundwater_rows(t - GROUNDWATER_LOOKBACK, end))
        features = build_v4_features(t, backwater_level_m, eyraud, gw, realtime=True)
        point = shadow_v4_point(timestamp, features, influx_client.RISK_SOURCE_REALTIME)
        if point is None:
            return False
        return await influx_client.write_points_batch([point]) == 1
    except Exception as e:
        logger.error("Error storing shadow risk score %s: %r", SHADOW_V4_MODEL_VERSION, e)
        return False
