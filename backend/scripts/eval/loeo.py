"""Leave-one-event-out (LOEO) evaluation for fitted candidate models.

``fit(dataset, train_events) -> Model`` must only use the labels it is
given (features of every hour may be used: they are not labels). For each
evaluable event (flooded or negative), the model is fitted on all the other
events and replayed on the whole dataset; the held-out event is then judged:

- flooded held-out: hit or miss at each threshold, lead time;
- negative held-out: whether an alert matched it (labelled false alarm).

Unknown events are never held out and are passed to ``fit`` unchanged
(a fit function should ignore events whose ``positive`` is None).

Helper :func:`training_targets` gives the hourly (row, target) pairs for a
set of training events, with the hours around the other events excluded
as in :mod:`scripts.eval.hourly`.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence

from scripts.eval.contingency import ContingencyConfig, alert_episodes, match_alert
from scripts.eval.dataset import EvalDataset, FeatureRow
from scripts.eval.hourly import HourlyTargetConfig, hourly_targets
from scripts.eval.models import Model, model_thresholds, replay
from scripts.eval.truth import EvalEvent, LabelPeriod, event_window

FitFn = Callable[[EvalDataset, Sequence[EvalEvent]], Model]


@dataclass
class FoldResult:
    held_out: str
    outcome: str
    positive: bool
    model_name: str
    # level -> matched (hit for a flood, false alarm for a negative)
    matched: dict[str, bool] = field(default_factory=dict)
    lead_time_h: dict[str, Optional[float]] = field(default_factory=dict)
    max_score_window: Optional[float] = None


@dataclass
class LoeoResult:
    folds: list[FoldResult]
    thresholds: dict[str, float]

    def summary(self) -> dict:
        out: dict = {"n_folds": len(self.folds), "levels": {}}
        pos = [f for f in self.folds if f.positive]
        neg = [f for f in self.folds if not f.positive]
        for level in self.thresholds:
            hits = sum(1 for f in pos if f.matched.get(level))
            fa = sum(1 for f in neg if f.matched.get(level))
            leads = [f.lead_time_h[level] for f in pos if f.lead_time_h.get(level) is not None]
            out["levels"][level] = {
                "pod": hits / len(pos) if pos else None,
                "hits": hits,
                "n_floods": len(pos),
                "labelled_negatives_alerted": fa,
                "n_negatives": len(neg),
                "median_lead_h": statistics.median(leads) if leads else None,
            }
        return out

    def to_dict(self) -> dict:
        out = self.summary()
        out["folds"] = [f.__dict__ for f in self.folds]
        return out


def training_targets(
    dataset: EvalDataset,
    train_events: Sequence[EvalEvent],
    period: Optional[LabelPeriod] = None,
    config: HourlyTargetConfig = HourlyTargetConfig(),
) -> list[tuple[FeatureRow, int]]:
    """(row, 0/1 'flood within 24h') pairs, excluded hours dropped."""
    targets = hourly_targets(dataset.times, train_events, period, config)
    return [(row, y) for row, y in zip(dataset.rows, targets) if y is not None]


def judge_held_out(
    dataset: EvalDataset,
    scores: Sequence[Optional[float]],
    event: EvalEvent,
    thresholds: Mapping[str, float],
    config: ContingencyConfig = ContingencyConfig(),
) -> FoldResult:
    times = dataset.times
    fold = FoldResult(event.key, event.outcome.value, bool(event.positive), "")
    for level, th in thresholds.items():
        alerts = alert_episodes(times, scores, th, config.merge_gap_h)
        matches = [m for m in (match_alert(a, event, config) for a in alerts) if m is not None]
        fold.matched[level] = bool(matches)
        fold.lead_time_h[level] = matches[0][0] if matches else None
    w0, w1 = event_window(event, config.lead_window_h, config.lag_window_h)
    window = [s for t, s in zip(times, scores) if w0 <= t <= w1 and s is not None]
    fold.max_score_window = max(window) if window else None
    return fold


def leave_one_event_out(
    fit: FitFn,
    dataset: EvalDataset,
    events: Optional[Sequence[EvalEvent]] = None,
    thresholds: Optional[Mapping[str, float]] = None,
    config: ContingencyConfig = ContingencyConfig(),
) -> LoeoResult:
    """Fit on all events but one, judge the held-out one; repeat for each."""
    if events is None:
        from scripts.eval.truth import load_events

        events, _ = load_events()
    folds: list[FoldResult] = []
    used_thresholds: dict[str, float] = {}
    for held in [e for e in events if e.evaluable]:
        train = [e for e in events if e.key != held.key]
        model = fit(dataset, train)
        th = model_thresholds(model, thresholds)
        used_thresholds = th
        scores = replay(model, dataset)
        fold = judge_held_out(dataset, scores, held, th, config)
        fold.model_name = model.name
        folds.append(fold)
    return LoeoResult(folds=folds, thresholds=used_thresholds)


# ---------------------------------------------------------------------------
# Example fit (template for candidate models)
# ---------------------------------------------------------------------------

@dataclass
class PessacThresholdModel:
    """One-parameter model: alert when the 12.5h max Pessac level >= threshold_m."""

    threshold_m: float
    name: str = "pessac_max12h_fitted"
    kind: str = "score"
    thresholds: dict[str, float] = field(default_factory=lambda: {"fitted": 50.0})

    def score(self, row: FeatureRow) -> Optional[float]:
        h = row.values.get("pessac_max_12h")
        if h is None:
            return None
        return min(100.0, max(0.0, 50.0 + 50.0 * (h - self.threshold_m)))


def fit_pessac_threshold(
    dataset: EvalDataset,
    train_events: Sequence[EvalEvent],
    margin_m: float = 0.10,
    config: ContingencyConfig = ContingencyConfig(),
) -> PessacThresholdModel:
    """Example fit: lowest 'max 12.5h Pessac level in the event window' over
    the TRAINING floods, minus a margin. Only ``train_events`` labels are used."""
    times = dataset.times
    peaks = []
    for ev in train_events:
        if ev.positive is not True:
            continue
        w0, w1 = event_window(ev, config.lead_window_h, config.lag_window_h)
        vals = [r.values.get("pessac_max_12h") for t, r in zip(times, dataset.rows) if w0 <= t <= w1]
        vals = [v for v in vals if v is not None]
        if vals:
            peaks.append(max(vals))
    threshold = (min(peaks) - margin_m) if peaks else 4.0
    return PessacThresholdModel(threshold_m=threshold)
