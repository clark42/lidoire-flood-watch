"""Climatology helpers for the hydrological indicators (PURE, no I/O).

Used by ``app/services/indicators.py`` to say "Eyraud au p99": where the
current value sits in the stored history of the same series. Never used by
the risk model.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from typing import Iterable, Mapping, Optional, Sequence

# Fewer historical values than this: no percentile (too short to rank)
MIN_HISTORY = 30


def percentile_of(value: Optional[float], history: Iterable[Optional[float]],
                  min_history: int = MIN_HISTORY) -> Optional[float]:
    """Percentile rank (0-100) of ``value`` in ``history`` (mid-rank for ties).

    p = 100 x (number below + half the number equal) / n. A value above the
    whole history gives 100, below it 0. None when ``value`` is None or the
    history holds fewer than ``min_history`` values (None entries ignored).
    """
    if value is None:
        return None
    values = sorted(float(v) for v in history if v is not None)
    n = len(values)
    if n == 0 or n < min_history:
        return None
    below = bisect_left(values, value)
    equal = bisect_right(values, value) - below
    return round(100.0 * (below + 0.5 * equal) / n, 1)


def percentile_label(p: Optional[float]) -> Optional[str]:
    """Display label: 'p99', 'p100' (floored, so p99.6 -> 'p99'), None if unknown."""
    if p is None:
        return None
    return f"p{int(min(100.0, max(0.0, p)))}"


def quantile(history: Iterable[Optional[float]], q: float) -> Optional[float]:
    """Linear-interpolation quantile (q in [0, 1]) of the history, None if empty."""
    values = sorted(float(v) for v in history if v is not None)
    if not values:
        return None
    q = min(1.0, max(0.0, q))
    pos = q * (len(values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def _pairs(points: Sequence) -> list[tuple[datetime, float]]:
    out = []
    for p in points:
        if isinstance(p, Mapping):
            ts, v = p.get("timestamp"), p.get("value")
        else:
            ts, v = p[0], p[1]
        if ts is None or v is None:
            continue
        if ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None) - (ts.utcoffset() or timedelta(0))
        out.append((ts, float(v)))
    out.sort(key=lambda x: x[0])
    return out


def latest(points: Sequence, t: Optional[datetime] = None) -> Optional[tuple[datetime, float]]:
    """Newest (timestamp, value) at or before ``t`` (default: the newest)."""
    pairs = _pairs(points)
    if t is not None:
        pairs = [p for p in pairs if p[0] <= t]
    return pairs[-1] if pairs else None


def trailing_mean(points: Sequence, t: datetime, hours: float) -> Optional[float]:
    """Mean of the values in (t - hours, t] (simple mean of the samples)."""
    start = t - timedelta(hours=hours)
    vals = [v for ts, v in _pairs(points) if start < ts <= t]
    return sum(vals) / len(vals) if vals else None


def slope_per_hour(points: Sequence, t: datetime, window_h: float = 3.0,
                   min_points: int = 3, min_span_h: float = 0.5) -> Optional[float]:
    """Least-squares slope (units per hour) over (t - window_h, t], points <= t.

    None when fewer than ``min_points`` points or a span under ``min_span_h``.
    """
    start = t - timedelta(hours=window_h)
    pairs = [(ts, v) for ts, v in _pairs(points) if start < ts <= t]
    if len(pairs) < min_points:
        return None
    xs = [(ts - pairs[0][0]).total_seconds() / 3600 for ts, _ in pairs]
    if xs[-1] - xs[0] < min_span_h:
        return None
    ys = [v for _, v in pairs]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx


def trend_label(slope: Optional[float], stable_band: float) -> Optional[str]:
    """'rising' / 'falling' / 'stable' with a +/- ``stable_band`` (units/h) dead band."""
    if slope is None:
        return None
    if slope > stable_band:
        return "rising"
    if slope < -stable_band:
        return "falling"
    return "stable"


def threshold_status(value: Optional[float], thresholds: Mapping[str, float]) -> dict:
    """Position of ``value`` against named thresholds (ascending).

    Returns ``{"exceeded": [names <= value], "next": name or None,
    "next_value": float or None, "distance": next_value - value or None,
    "ratio_to_next": value / next_value or None}``.
    """
    ordered = sorted(((name, float(v)) for name, v in (thresholds or {}).items()), key=lambda x: x[1])
    if value is None:
        return {"exceeded": [], "next": None, "next_value": None, "distance": None, "ratio_to_next": None}
    exceeded = [name for name, v in ordered if value >= v]
    upcoming = [(name, v) for name, v in ordered if value < v]
    if upcoming:
        name, v = upcoming[0]
        return {
            "exceeded": exceeded,
            "next": name,
            "next_value": v,
            "distance": round(v - value, 3),
            "ratio_to_next": round(value / v, 3) if v else None,
        }
    return {"exceeded": exceeded, "next": None, "next_value": None, "distance": None, "ratio_to_next": None}
