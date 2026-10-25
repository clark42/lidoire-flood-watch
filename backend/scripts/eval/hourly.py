"""Hourly probabilistic metrics on a 'flood within the next 24h' target.

Target at hour t: 1 when a flooded event peaks in [t, t + horizon_h],
0 otherwise. Excluded hours (target None):

- outside the annotated period;
- (peak, peak + post_peak_h] after a flooded peak (the mill may still be
  flooded: neither a clean positive nor a clean negative);
- [peak - unknown_before_h, peak + unknown_after_h] around unknown events.

The labels are not exhaustive, so some "0" hours may precede unannotated
floods: AUC/Brier are optimistic for nobody and pessimistic for a good model.
For score models (0-100) the Brier score uses score/100 and is only a
"Brier-like" ranking aid, not a calibrated probability score.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence

import numpy as np

from scripts.eval.truth import EvalEvent, LabelPeriod


@dataclass(frozen=True)
class HourlyTargetConfig:
    horizon_h: float = 24.0
    post_peak_h: float = 24.0
    unknown_before_h: float = 72.0
    unknown_after_h: float = 24.0


def hourly_targets(
    times: Sequence[datetime],
    events: Sequence[EvalEvent],
    period: Optional[LabelPeriod] = None,
    config: HourlyTargetConfig = HourlyTargetConfig(),
) -> list[Optional[int]]:
    """0/1 target per hour, None when the hour is excluded."""
    floods = [e.peak_time for e in events if e.positive is True]
    unknown = [e.peak_time for e in events if e.positive is None]
    horizon = timedelta(hours=config.horizon_h)
    post = timedelta(hours=config.post_peak_h)
    ub, ua = timedelta(hours=config.unknown_before_h), timedelta(hours=config.unknown_after_h)

    out: list[Optional[int]] = []
    for t in times:
        if period is not None and not period.contains(t):
            out.append(None)
            continue
        if any(p - ub <= t <= p + ua for p in unknown):
            out.append(None)
            continue
        if any(p < t <= p + post for p in floods):
            out.append(None)
            continue
        out.append(1 if any(t <= p <= t + horizon for p in floods) else 0)
    return out


def roc_auc(scores: Sequence[float], targets: Sequence[int]) -> Optional[float]:
    """ROC AUC by the Mann-Whitney statistic (ties get half credit)."""
    s = np.asarray(scores, dtype=float)
    y = np.asarray(targets, dtype=int)
    n_pos = int(y.sum())
    n_neg = int(len(y) - n_pos)
    if n_pos == 0 or n_neg == 0:
        return None
    order = np.argsort(s, kind="mergesort")
    sorted_s = s[order]
    ranks = np.empty(len(s), dtype=float)
    # average ranks over ties (1-based)
    _, first_idx, counts = np.unique(sorted_s, return_index=True, return_counts=True)
    avg = first_idx + (counts + 1) / 2.0
    ranks[order] = np.repeat(avg, counts)
    rank_sum_pos = ranks[y == 1].sum()
    return float((rank_sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def brier(probabilities: Sequence[float], targets: Sequence[int]) -> Optional[float]:
    if len(targets) == 0:
        return None
    p = np.clip(np.asarray(probabilities, dtype=float), 0.0, 1.0)
    y = np.asarray(targets, dtype=float)
    return float(np.mean((p - y) ** 2))


@dataclass
class HourlyResult:
    n_hours: int
    n_pos: int
    n_neg: int
    base_rate: Optional[float]
    roc_auc: Optional[float]
    brier: Optional[float]
    brier_reference: Optional[float]  # climatology (always predict the base rate)
    brier_kind: str  # "probability" | "score/100"

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def hourly_metrics(
    scores: Sequence[Optional[float]],
    targets: Sequence[Optional[int]],
    kind: str = "score",
) -> HourlyResult:
    """AUC and Brier over hours with both a score and a target."""
    pairs = [(s, y) for s, y in zip(scores, targets) if s is not None and y is not None]
    s = [p[0] for p in pairs]
    y = [p[1] for p in pairs]
    n_pos = sum(y)
    n = len(y)
    probs = s if kind == "probability" else [v / 100.0 for v in s]
    base = n_pos / n if n else None
    return HourlyResult(
        n_hours=n,
        n_pos=n_pos,
        n_neg=n - n_pos,
        base_rate=base,
        roc_auc=roc_auc(s, y) if n else None,
        brier=brier(probs, y),
        brier_reference=brier([base] * n, y) if n else None,
        brier_kind="probability" if kind == "probability" else "score/100",
    )
