"""Candidate models (app.services.models_experimental) against v3.3 and the baselines.

Run from ``backend/``::

    python -m scripts.eval.candidates [--offline] [--out data/eval_out/candidates]

Protocol (every number of a FITTED model is out-of-sample):

- Blocked leave-one-event-out cross-validation: the timeline is cut into
  blocks, one per group of evaluable labelled events (events whose peaks are
  less than ``group_days`` apart form one group, e.g. 2023-12-01 and
  2023-12-11, so that one hydrological episode is never split between the
  training and the test side); block boundaries are the midpoints between
  groups. For each block, the model is fitted on the labels AND the hours of
  every other block only, then replayed; its scores on the held-out block
  are kept. Concatenating the blocks gives an out-of-sample hourly series on
  which every metric (POD, FAR, CSI, lead time, alerts/year, AUC) is computed
  with the harness (:func:`scripts.eval.models.evaluate_model`).
- The harness :func:`scripts.eval.loeo.leave_one_event_out` (fit on all
  events but one, judge the held-out one) is also run as a cross-check.
- Label-free models (thresholds from config or from the unlabelled feature
  distribution: v3.3+backwater gate, a-priori backwater rules, GR4H) are not
  fitted: their in-sample numbers are their out-of-sample numbers. v3.3
  itself was hand-tuned on these 14 episodes: its numbers are IN-SAMPLE.
- v4-alpha (:mod:`app.services.risk_v4`, pre-registered 2026-10-26 before
  any v4 metric): backwater AND runoff, OR overbank, optional groundwater;
  thresholds from config, physics and pre-2022-10 climatology
  (:mod:`scripts.eval.climatology`), zero label-tuned parameter. Variants:
  runoff proxy (Eyraud Q lagged 7 h, Caudeau Q, GR4H, GloFAS), backwater
  input (Pessac H_aval or confluence stage Z_c), groundwater on/off,
  overbank off (ablation). Fitted Eyraud rules go through the blocked LOEO.

Small sample: 5 floods (one of them an infiltration flood, 2023-03-11, since
sealed). POD comes with a Clopper-Pearson 95% interval, AUC with a
block-bootstrap 90% interval (blocks resampled with replacement).
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

import numpy as np

from app.services import risk_v4
from app.services.models_experimental import (
    DEFAULT_PROBABILITY_THRESHOLDS,
    PESSAC_THRESHOLDS_M,
    BackwaterRuleModel,
    LogisticModel,
    V33BackwaterModel,
    V4AlphaModel,
    design_matrix,
    feature_table,
    fit_logistic_l2,
    logistic_model_from_fit,
    steps_from_orange,
)
from scripts.eval.contingency import ContingencyConfig, ContingencyResult
from scripts.eval.dataset import EvalDataset
from scripts.eval.hourly import HourlyTargetConfig, hourly_targets, roc_auc
from scripts.eval.loeo import fit_pessac_threshold, leave_one_event_out
from scripts.eval.models import (
    Model,
    ModelEvaluation,
    PessacLevelModel,
    Rain72hModel,
    V33Model,
    evaluate_model,
    model_kind,
    model_thresholds,
    replay,
)
from scripts.eval.truth import EvalEvent, LabelPeriod, load_events

logger = logging.getLogger("scripts.eval.candidates")

LEAD_H = 72
LAG_H = 6
# Hourly weight of a training row: one effective observation per day
# (hourly rows are strongly autocorrelated)
LR_ROW_WEIGHT = 1.0 / 24.0
# Ridge strength on standardised slopes (N(0, 1) prior per slope, after the
# 1/24 row weighting): strong shrinkage for 4-5 training floods
LR_L2 = 1.0
WINTER_MONTHS = (11, 12, 1, 2, 3)

FitFn = Callable[..., Model]


# ---------------------------------------------------------------------------
# Feature table cache (features are not labels: computed once on all hours)
# ---------------------------------------------------------------------------

_TABLE_CACHE: list = []  # [(dataset, table)] for the last dataset seen


def candidate_table(dataset: EvalDataset) -> dict[str, np.ndarray]:
    """Candidate features of every row of ``dataset`` (memoised for the last dataset)."""
    if _TABLE_CACHE and _TABLE_CACHE[0][0] is dataset and len(_TABLE_CACHE[0][1]["h_aval"]) == len(dataset):
        return _TABLE_CACHE[0][1]
    table = feature_table(dataset.rows)
    _TABLE_CACHE[:] = [(dataset, table)]
    return table


# ---------------------------------------------------------------------------
# Cross-validation blocks
# ---------------------------------------------------------------------------

@dataclass
class CVBlock:
    key: str
    events: list[EvalEvent]  # every event (evaluable or not) whose peak is in the block
    start: int  # row index, inclusive
    end: int  # row index, exclusive

    @property
    def keys(self) -> set[str]:
        return {e.key for e in self.events}


def event_blocks(dataset: EvalDataset, events: Sequence[EvalEvent], group_days: float = 21.0) -> list[CVBlock]:
    """Contiguous blocks covering the dataset, one per group of evaluable events."""
    times = dataset.times
    evaluable = sorted((e for e in events if e.evaluable), key=lambda e: e.peak_time)
    if not evaluable:
        return [CVBlock("all", list(events), 0, len(times))]
    groups: list[list[EvalEvent]] = [[evaluable[0]]]
    for ev in evaluable[1:]:
        if ev.peak_time - groups[-1][-1].peak_time <= timedelta(days=group_days):
            groups[-1].append(ev)
        else:
            groups.append([ev])
    bounds = [0]
    for g0, g1 in zip(groups, groups[1:]):
        mid = g0[-1].peak_time + (g1[0].peak_time - g0[-1].peak_time) / 2
        bounds.append(int(np.searchsorted(np.array(times, dtype="datetime64[s]"), np.datetime64(mid))))
    bounds.append(len(times))
    blocks = []
    for g, b0, b1 in zip(groups, bounds, bounds[1:]):
        t0 = times[b0] if b0 < len(times) else times[-1]
        t1 = times[b1 - 1] if b1 > 0 else times[0]
        members = [e for e in events if t0 <= e.peak_time <= t1] if b0 < b1 else list(g)
        blocks.append(CVBlock("+".join(e.key for e in g), members, b0, b1))
    return blocks


def mask_excluding(n: int, start: int, end: int) -> np.ndarray:
    m = np.ones(n, dtype=bool)
    m[start:end] = False
    return m


def mask_around_missing_events(
    dataset: EvalDataset, train_events: Sequence[EvalEvent], all_events: Sequence[EvalEvent],
    days: float = 7.0,
) -> np.ndarray:
    """Training mask for the harness LOEO: drop +-``days`` around every
    evaluable event missing from ``train_events`` (the held-out one), whatever
    its label, so its hours are neither negatives nor false alarms in training."""
    times = np.array(dataset.times, dtype="datetime64[s]")
    keys = {e.key for e in train_events}
    m = np.ones(len(times), dtype=bool)
    for e in all_events:
        if e.evaluable and e.key not in keys:
            p = np.datetime64(e.peak_time)
            d = np.timedelta64(int(days * 86400), "s")
            m &= ~((times >= p - d) & (times <= p + d))
    return m


# ---------------------------------------------------------------------------
# Fast event-based objective (training side only)
# ---------------------------------------------------------------------------

def _runs(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(starts, ends inclusive) of runs of True."""
    m = np.concatenate(([False], mask, [False])).astype(np.int8)
    d = np.diff(m)
    return np.flatnonzero(d == 1), np.flatnonzero(d == -1) - 1


@dataclass
class TrainingFrame:
    """Index-based view of the training labels for the fit objectives."""

    flood_idx: list[int]
    other_idx: list[int]  # unknown events: alerts matching them are not false alarms
    period_mask: np.ndarray
    train_mask: np.ndarray

    @classmethod
    def build(cls, dataset: EvalDataset, train_events: Sequence[EvalEvent], period: Optional[LabelPeriod],
              train_mask: Optional[np.ndarray]) -> "TrainingFrame":
        times = dataset.times
        n = len(times)
        flood_idx = [i for i in (dataset.index_of(e.peak_time) for e in train_events if e.positive is True)
                     if i is not None]
        other_idx = [i for i in (dataset.index_of(e.peak_time) for e in train_events if e.positive is None)
                     if i is not None]
        if period is not None:
            period_mask = np.array([period.contains(t) for t in times])
        else:
            period_mask = np.ones(n, dtype=bool)
        tm = np.ones(n, dtype=bool) if train_mask is None else np.asarray(train_mask, dtype=bool)
        return cls(flood_idx, other_idx, period_mask, tm)

    def csi(self, alert_mask: np.ndarray) -> tuple[float, int, int, int]:
        """(CSI raw, hits, false alarms, alert hours) of an hourly alert mask."""
        mask = alert_mask & self.train_mask
        starts, ends = _runs(mask)
        hit = set()
        fa = 0
        for s, e in zip(starts, ends):
            matched_flood = False
            for k, p in enumerate(self.flood_idx):
                w0, w1 = p - LEAD_H, p + LAG_H
                if w0 <= s <= w1 or s < w0 <= e:
                    hit.add(k)
                    matched_flood = True
            if matched_flood or not self.period_mask[s]:
                continue
            if any(p - LEAD_H <= s <= p + LAG_H or s < p - LEAD_H <= e for p in self.other_idx):
                continue
            fa += 1
        n_floods = len(self.flood_idx)
        den = n_floods + fa
        return (len(hit) / den if den else 0.0), len(hit), fa, int(mask.sum())


# ---------------------------------------------------------------------------
# Fits
# ---------------------------------------------------------------------------

H_GRID = np.round(np.arange(2.5, 5.55, 0.1), 2)
F_QUANTILES = np.linspace(80.0, 99.8, 34)


def fit_backwater_rule(
    dataset: EvalDataset,
    train_events: Sequence[EvalEvent],
    forcing: str = "api_fast",
    train_mask: Optional[np.ndarray] = None,
    period: Optional[LabelPeriod] = None,
    name: Optional[str] = None,
) -> BackwaterRuleModel:
    """2 parameters (H*, F*): orange = H_aval >= H* AND forcing >= F*.

    Grid search on the training side maximising the event CSI (raw: every
    alert matching no training flood counts as a false alarm), ties broken
    by fewer alert hours. Yellow/red steps follow the Pessac threshold
    ratios. The forcing grid is made of quantiles of the forcing over the
    training hours (features only).
    """
    if period is None:
        _, period = load_events()
    table = candidate_table(dataset)
    frame = TrainingFrame.build(dataset, train_events, period, train_mask)
    h = table["h_aval"]
    f = table[forcing]
    h_ok = np.nan_to_num(h, nan=-1.0)
    f_ok = np.nan_to_num(f, nan=-1.0)
    f_grid = np.unique(np.round(np.nanpercentile(f[frame.train_mask], F_QUANTILES), 3))
    f_grid = f_grid[f_grid > 0]  # a zero threshold would make the rule backwater-only
    if f_grid.size == 0:
        f_grid = np.array([float(np.nanmax(f[frame.train_mask]))])
    best = None
    for hs in H_GRID:
        hm = h_ok >= hs
        for fs in f_grid:
            csi, hits, fa, hours = frame.csi(hm & (f_ok >= fs))
            key = (csi, -hours, hs, fs)
            if best is None or key > best[0]:
                best = (key, hs, fs, hits, fa)
    _, hs, fs, hits, fa = best
    model = BackwaterRuleModel(
        f_thresholds=steps_from_orange(float(fs)),
        h_thresholds=steps_from_orange(float(hs)),
        forcing=forcing,
        name=name or f"bw_rule_{forcing}_cv",
    )
    model.fit_info = {"h_star": float(hs), "f_star": float(fs), "train_csi": best[0][0],
                      "train_hits": hits, "train_fa": fa}
    return model


def fit_logistic(
    dataset: EvalDataset,
    train_events: Sequence[EvalEvent],
    features: Sequence[str] = ("api_fast", "h_aval"),
    train_mask: Optional[np.ndarray] = None,
    period: Optional[LabelPeriod] = None,
    l2: float = LR_L2,
    row_weight: float = LR_ROW_WEIGHT,
    thresholds: Optional[Mapping[str, float]] = None,
    name: Optional[str] = None,
) -> LogisticModel:
    """L2-penalised logistic regression on 'flood within next 24h' (training hours only).

    Targets from :func:`scripts.eval.hourly.hourly_targets` with the training
    events only; hours outside ``train_mask`` or with a missing feature are
    dropped. Probability thresholds are FIXED a priori (orange 5%, red 20%),
    not fitted.
    """
    if period is None:
        _, period = load_events()
    table = candidate_table(dataset)
    targets = hourly_targets(dataset.times, train_events, period, HourlyTargetConfig())
    y = np.array([np.nan if t is None else t for t in targets], dtype=float)
    x = design_matrix(table, features)
    keep = ~np.isnan(y) & np.all(np.isfinite(x), axis=1)
    if train_mask is not None:
        keep &= np.asarray(train_mask, dtype=bool)
    fit = fit_logistic_l2(x[keep], y[keep], np.full(int(keep.sum()), row_weight), l2=l2)
    model = logistic_model_from_fit(
        fit, features, name=name or f"lr_{'+'.join(features)}_cv",
        thresholds=thresholds or DEFAULT_PROBABILITY_THRESHOLDS,
    )
    model.fit_info = {
        "coef_std": [float(c) for c in fit.coef], "intercept": fit.intercept,
        "n_rows": int(keep.sum()), "n_pos": int(y[keep].sum()), "converged": fit.converged,
    }
    return model


# ---------------------------------------------------------------------------
# Label-free (a-priori) candidates
# ---------------------------------------------------------------------------

def label_free_quantile_thresholds(values: np.ndarray, q=(90.0, 97.0, 99.5)) -> tuple[float, float, float]:
    """(yellow, orange, red) as quantiles of the unlabelled hourly distribution."""
    return tuple(float(v) for v in np.nanpercentile(values, q))  # type: ignore[return-value]


def hydro_year_maxima(times: Sequence[datetime], q: np.ndarray, min_hours: int = 6000) -> list[float]:
    """Max of each hydrological year (Sep -> Aug) with enough hours."""
    by_year: dict[int, list[float]] = {}
    for t, v in zip(times, q):
        if not math.isnan(v):
            by_year.setdefault(t.year if t.month >= 9 else t.year - 1, []).append(v)
    return [max(v) for v in by_year.values() if len(v) >= min_hours]


def label_free_q_thresholds(times: Sequence[datetime], q: np.ndarray) -> tuple[float, float, float]:
    """GR4H discharge steps from the simulated 2-year flood (median of annual maxima).

    yellow 0.4 Q2, orange 0.7 Q2 (~ bankfull, 1-1.5 year flood), red Q2.
    Uses the simulated series only (no label), but the whole period.
    """
    maxima = hydro_year_maxima(times, q)
    q2 = float(np.median(maxima)) if maxima else float(np.nanpercentile(q, 99.9))
    return (0.4 * q2, 0.7 * q2, q2)


# ---------------------------------------------------------------------------
# Blocked cross-validation
# ---------------------------------------------------------------------------

@dataclass
class CVResult:
    name: str
    kind: str
    thresholds: dict[str, float]
    scores: list[Optional[float]]
    folds: list[dict] = field(default_factory=list)


def blocked_cv(
    fit: FitFn,
    dataset: EvalDataset,
    events: Sequence[EvalEvent],
    blocks: Sequence[CVBlock],
    period: Optional[LabelPeriod],
    name: str,
) -> CVResult:
    """Out-of-sample hourly scores: each block scored by a model fitted without it."""
    n = len(dataset)
    scores: list[Optional[float]] = [None] * n
    folds = []
    kind, th = "score", None
    for b in blocks:
        train_events = [e for e in events if e.key not in b.keys]
        model = fit(dataset, train_events, train_mask=mask_excluding(n, b.start, b.end), period=period)
        kind, th = model_kind(model), model_thresholds(model)
        s = replay(model, dataset)
        scores[b.start:b.end] = s[b.start:b.end]
        info = getattr(model, "fit_info", None) or {
            k: v for k, v in vars(model).items() if isinstance(v, float) and not k.startswith("_")
        }
        folds.append({"block": b.key, "start": dataset.times[b.start].isoformat() if b.start < n else None,
                      **info})
    return CVResult(name, kind, th or {}, scores, folds)


class ScoresStub:
    """Model-like holder so that precomputed scores go through evaluate_model."""

    def __init__(self, name: str, kind: str, thresholds: Mapping[str, float]):
        self.name = name
        self.kind = kind
        self.thresholds = dict(thresholds)

    def score(self, row):  # pragma: no cover - never replayed
        raise RuntimeError("ScoresStub holds precomputed scores")


# ---------------------------------------------------------------------------
# Uncertainty
# ---------------------------------------------------------------------------

def _binom_cdf(k: int, n: int, p: float) -> float:
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(0, k + 1))


def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Exact binomial interval (bisection, no scipy)."""
    if n == 0:
        return (0.0, 1.0)

    def solve(f, lo=0.0, hi=1.0):
        for _ in range(60):
            mid = (lo + hi) / 2
            if f(mid):
                hi = mid
            else:
                lo = mid
        return (lo + hi) / 2

    lower = 0.0 if k == 0 else solve(lambda p: 1 - _binom_cdf(k - 1, n, p) >= alpha / 2)
    upper = 1.0 if k == n else solve(lambda p: _binom_cdf(k, n, p) <= alpha / 2)
    return lower, upper


def block_bootstrap_auc(
    scores: Sequence[Optional[float]],
    targets: Sequence[Optional[int]],
    blocks: Sequence[CVBlock],
    n_boot: int = 300,
    seed: int = 20261026,
    q=(5.0, 95.0),
) -> Optional[tuple[float, float]]:
    """AUC interval by resampling CV blocks with replacement."""
    s = np.array([np.nan if v is None else v for v in scores], dtype=float)
    y = np.array([-1 if v is None else v for v in targets], dtype=int)
    per_block = []
    for b in blocks:
        idx = np.arange(b.start, b.end)
        ok = idx[(y[idx] >= 0) & ~np.isnan(s[idx])]
        per_block.append(ok)
    rng = np.random.default_rng(seed)
    aucs = []
    for _ in range(n_boot):
        pick = rng.integers(0, len(per_block), len(per_block))
        idx = np.concatenate([per_block[i] for i in pick])
        if idx.size == 0:
            continue
        a = roc_auc(s[idx], y[idx])
        if a is not None:
            aucs.append(a)
    if not aucs:
        return None
    lo, hi = np.percentile(aucs, q)
    return float(lo), float(hi)


def alerts_per_winter(result: ContingencyResult, winters: Sequence[int] = (2022, 2023, 2024)) -> float:
    """Mean number of alerts starting Nov-Mar over the given winters (Nov of year y)."""
    counts = []
    for y in winters:
        lo, hi = datetime(y, 11, 1), datetime(y + 1, 4, 1)
        counts.append(sum(1 for a in result.alerts if lo <= a.alert.start < hi))
    return float(np.mean(counts)) if counts else float("nan")


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    name: str
    protocol: str  # "in-sample (hand-tuned)" | "label-free" | "blocked LOEO (out-of-sample)"
    n_fitted: int
    scores: list[Optional[float]]
    kind: str
    thresholds: dict[str, float]
    folds: list[dict] = field(default_factory=list)
    params: dict = field(default_factory=dict)


def _fmt(v, digits=2, pct=False) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    if pct:
        return f"{100 * v:.0f}%"
    return f"{v:.{digits}f}" if isinstance(v, float) else str(v)


def comparison_rows(
    candidates: Sequence[Candidate],
    dataset: EvalDataset,
    events: Sequence[EvalEvent],
    period: Optional[LabelPeriod],
    blocks: Sequence[CVBlock],
    config: ContingencyConfig,
    with_ci: bool = True,
) -> tuple[list[dict], list[ModelEvaluation]]:
    targets = hourly_targets(dataset.times, events, period, HourlyTargetConfig())
    rows, evals = [], []
    for c in candidates:
        ev = evaluate_model(ScoresStub(c.name, c.kind, c.thresholds), dataset, events, period,
                            None, config, scores=c.scores)
        evals.append(ev)
        auc_ci = block_bootstrap_auc(c.scores, targets, blocks) if with_ci else None
        for level, r in ev.contingency.items():
            n_fl = r.hits + r.misses
            rows.append({
                "model": c.name, "protocol": c.protocol, "n_fitted": c.n_fitted, "level": level,
                "threshold": r.threshold, "hits": r.hits, "n_floods": n_fl, "pod": r.pod,
                "pod_ci95": clopper_pearson(r.hits, n_fl) if n_fl else None,
                "alerts": r.n_alerts_judged, "fa_raw": r.false_alarms_raw,
                "fa_labelled": r.false_alarms_labelled, "far_raw": r.far_raw,
                "far_labelled": r.far_labelled, "csi_raw": r.csi_raw, "csi_labelled": r.csi_labelled,
                "lead_median_h": r.median_lead_h, "lead_min_h": r.min_lead_h,
                "alerts_per_year": r.alerts_per_year, "alerts_per_winter": alerts_per_winter(r),
                "hours_in_alert": r.alert_hours_share,
                "auc": ev.hourly.roc_auc, "auc_ci90": auc_ci, "brier": ev.hourly.brier,
                "brier_kind": ev.hourly.brier_kind,
            })
    return rows, evals


def comparison_markdown(rows: Sequence[dict]) -> str:
    lines = [
        "| Model | Protocol (fitted params) | Level (thr) | Hits/Floods | POD [95% CI] | Alerts | FA raw (lab+unlab) "
        "| FAR raw | FAR lab | CSI raw | CSI lab | Lead median / min (h) | Alerts/yr | Alerts/winter | AUC [90% CI] |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        ci = r["pod_ci95"]
        auc_ci = r["auc_ci90"]
        lines.append(
            f"| {r['model']} | {r['protocol']} ({r['n_fitted']}) | {r['level']} ({r['threshold']:g}) | "
            f"{r['hits']}/{r['n_floods']} | {_fmt(r['pod'], pct=True)}"
            + (f" [{100 * ci[0]:.0f}-{100 * ci[1]:.0f}]" if ci else "") + " | "
            f"{r['alerts']} | {r['fa_raw']} ({r['fa_labelled']}+{r['fa_raw'] - r['fa_labelled']}) | "
            f"{_fmt(r['far_raw'], pct=True)} | {_fmt(r['far_labelled'], pct=True)} | {_fmt(r['csi_raw'])} | "
            f"{_fmt(r['csi_labelled'])} | {_fmt(r['lead_median_h'], 0)} / {_fmt(r['lead_min_h'], 0)} | "
            f"{_fmt(r['alerts_per_year'], 1)} | {_fmt(r['alerts_per_winter'], 1)} | {_fmt(r['auc'], 3)}"
            + (f" [{auc_ci[0]:.2f}-{auc_ci[1]:.2f}]" if auc_ci else "") + " |"
        )
    return "\n".join(lines)


def event_matrix_markdown(evals: Sequence[ModelEvaluation], events: Sequence[EvalEvent]) -> str:
    """Per labelled event: highest level reached in the event window (initial letter) and the
    lead time of the first score at/above the lowest level, '-' if none."""
    names = [e.name for e in evals]
    lines = ["| Event | Outcome | " + " | ".join(names) + " |", "|---|---|" + "---|" * len(names)]
    for ev in events:
        cells = []
        for e in evals:
            entry = next((r for r in e.event_table if r["key"] == ev.key), None)
            cell = "-"
            if entry:
                levels = sorted(e.thresholds.items(), key=lambda kv: kv[1])
                reached = [lv for lv, _ in levels if entry.get(f"first_{lv}")]
                if reached:
                    lead = entry.get(f"lead_{reached[0]}_h")
                    cell = reached[-1][0].upper() + (f" {lead:.0f}h" if lead is not None else "")
            cells.append(cell)
        lines.append(f"| {ev.key} | {ev.outcome.value} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def build_candidates(
    dataset: EvalDataset,
    events: Sequence[EvalEvent],
    period: Optional[LabelPeriod],
    blocks: Sequence[CVBlock],
) -> list[Candidate]:
    """Replay every model and baseline; fitted ones through the blocked CV."""
    table = candidate_table(dataset)
    times = dataset.times
    out: list[Candidate] = []

    def plain(model: Model, protocol: str, n_fitted: int = 0, params: Optional[dict] = None) -> None:
        t0 = time.time()
        s = replay(model, dataset)
        logger.info("replayed %s (%.0fs)", model.name, time.time() - t0)
        out.append(Candidate(model.name, protocol, n_fitted, s, model_kind(model), model_thresholds(model),
                             params=params or {}))

    def cv(fit: FitFn, name: str, n_fitted: int) -> None:
        t0 = time.time()
        r = blocked_cv(lambda ds, ev, **kw: fit(ds, ev, name=name, **kw), dataset, events, blocks, period, name)
        logger.info("blocked CV %s (%.0fs)", name, time.time() - t0)
        out.append(Candidate(name, "blocked LOEO", n_fitted, r.scores, r.kind, r.thresholds, folds=r.folds))

    # reference models
    plain(V33Model(), "in-sample, hand-tuned", 45)
    plain(PessacLevelModel(), "label-free (config)")
    plain(Rain72hModel(), "label-free (config)")
    r = blocked_cv(lambda ds, ev, train_mask=None, period=None: fit_pessac_threshold(ds, ev), dataset, events,
                   blocks, period, "pessac_max12h_cv")
    out.append(Candidate("pessac_max12h_cv", "blocked LOEO", 1, r.scores, r.kind, r.thresholds, folds=r.folds))

    # (1) backwater: v3.3 + gate, a-priori 2-variable rules, fitted 2-variable rules
    plain(V33BackwaterModel(), "label-free (config)")
    api_th = label_free_quantile_thresholds(table["api_fast"])
    plain(BackwaterRuleModel(f_thresholds=api_th, forcing="api_fast", name="bw_rule_api_apriori"),
          "label-free (config + quantiles)", params={"h": PESSAC_THRESHOLDS_M, "api": api_th})
    q_th = label_free_q_thresholds(times, table["q_gr4h"])
    plain(BackwaterRuleModel(f_thresholds=q_th, forcing="q_gr4h", name="bw_rule_gr4h_apriori"),
          "label-free (config + simulated Q2)", params={"h": PESSAC_THRESHOLDS_M, "q_m3s": q_th})
    cv(lambda ds, ev, **kw: fit_backwater_rule(ds, ev, forcing="api_fast", **kw), "bw_rule_api_cv", 2)
    cv(lambda ds, ev, **kw: fit_backwater_rule(ds, ev, forcing="rain_24h", **kw), "bw_rule_rain24_cv", 2)
    cv(lambda ds, ev, **kw: fit_backwater_rule(ds, ev, forcing="q_gr4h", **kw), "bw_rule_gr4h_cv", 2)

    # (2) penalised logistic regressions
    cv(lambda ds, ev, **kw: fit_logistic(ds, ev, ("api_fast", "h_aval"), **kw), "lr_api_haval_cv", 3)
    cv(lambda ds, ev, **kw: fit_logistic(ds, ev, ("api_fast", "h_aval", "api_slow"), **kw),
       "lr_api_haval_soil_cv", 4)
    cv(lambda ds, ev, **kw: fit_logistic(ds, ev, ("q_gr4h", "h_aval"), **kw), "lr_gr4h_haval_cv", 3)

    # (3) v4-alpha (pre-registered, label-free) and its variants
    for model, protocol in v4_alpha_models(dataset):
        plain(model, protocol, params={"runoff": model.runoff_feature, "backwater": model.params.backwater_source,
                                       "overbank": model.params.use_overbank,
                                       "groundwater": model.params.use_groundwater})
    if has_feature(table, "q_eyraud"):
        cv(lambda ds, ev, **kw: fit_backwater_rule(ds, ev, forcing="q_eyraud", **kw), "bw_rule_eyraud_cv", 2)
        cv(lambda ds, ev, **kw: fit_logistic(ds, ev, ("q_eyraud", "h_aval"), **kw), "lr_eyraud_haval_cv", 3)
    return out


def has_feature(table: Mapping[str, np.ndarray], name: str) -> bool:
    return name in table and bool(np.isfinite(table[name]).any())


V4_HEADLINE = "v4a_joint"
TIMING_WINDOWS = {
    "2025-04 flood (label peak 2025-04-23 16:00)": (datetime(2025, 4, 18), datetime(2025, 4, 25, 23)),
    "2026-02 Pessac record (unlabelled)": (datetime(2026, 2, 8), datetime(2026, 2, 17, 23)),
}


def v4_alpha_models(dataset: EvalDataset, include_sensitivity: bool = False) -> list[tuple[V4AlphaModel, str]]:
    """The pre-registered v4-alpha variants (no parameter fitted on labels).

    GR4H thresholds: 0.4 / 0.7 / 1.0 x the simulated Q2 (whole simulated
    series, no label), as ``bw_rule_gr4h_apriori``.
    """
    table = candidate_table(dataset)
    base = risk_v4.default_v4_params()
    gr4h_th = label_free_q_thresholds(dataset.times, table["q_gr4h"])
    models = [
        (V4AlphaModel(base, "q_eyraud", name=V4_HEADLINE), "label-free (pre-registered)"),
        (V4AlphaModel(replace(base, backwater_source="confluence"), "q_eyraud", name="v4a_joint_zc"),
         "label-free (pre-registered)"),
        (V4AlphaModel(replace(base, runoff=risk_v4.runoff_thresholds(risk_v4.CAUDEAU_Q2_M3S), runoff_source="caudeau"),
                      "q_caudeau", name="v4a_caudeau"), "label-free (pre-registered)"),
        (V4AlphaModel(replace(base, runoff=risk_v4.GradedThresholds(0.0, *gr4h_th), runoff_source="gr4h"),
                      "q_gr4h", name="v4a_gr4h"), "label-free (pre-registered)"),
        (V4AlphaModel(replace(base, runoff=risk_v4.runoff_thresholds(risk_v4.GLOFAS_Q2_M3S), runoff_source="glofas"),
                      "q_glofas", name="v4a_glofas"), "label-free (pre-registered)"),
        (V4AlphaModel(replace(base, use_groundwater=True), "q_eyraud", name="v4a_joint+gw"),
         "label-free (pre-registered)"),
        (V4AlphaModel(replace(base, use_overbank=False), "q_eyraud", name="v4a_joint_no_overbank"),
         "label-free (ablation)"),
    ]
    if include_sensitivity:
        models.append((V4AlphaModel(base, "q_eyraud_lag0", name="v4a_joint[Eyraud lag 0, optimistic]"),
                       "label-free (sensitivity)"))
    return models


def climatology_check(dataset: EvalDataset) -> list[str]:
    """Recompute the pre-registered thresholds (offline cache) and compare with risk_v4."""
    from scripts.eval import climatology

    lines = []
    try:
        res = climatology.compute(climatology.download_series(client=None))
    except Exception as exc:  # cache missing: informative only
        return [f"- climatology not recomputed: {exc!r}"]
    expected = {
        "eyraud_q2_m3s": risk_v4.EYRAUD_Q2_M3S, "caudeau_q2_m3s": risk_v4.CAUDEAU_Q2_M3S,
        "glofas_q2_m3s": risk_v4.GLOFAS_Q2_M3S,
    }
    for k, v in expected.items():
        lines.append(f"- {k}: recomputed {_fmt(res.get(k), 2)}, risk_v4 {v:g}")
    lines.append(f"- pessac_t_year_levels_m (T=2/5/10): recomputed "
                 f"{tuple(round(x, 2) for x in res.get('pessac_t_year_levels_m') or ())}, "
                 f"risk_v4 {risk_v4.PESSAC_T_YEAR_LEVELS_M}")
    lines.append(f"- piezo_percentiles_ngf (50/90/97/99.5): recomputed "
                 f"{tuple(round(x, 2) for x in res.get('piezo_percentiles_ngf') or ())}, "
                 f"risk_v4 {risk_v4.PIEZO_PERCENTILES_NGF}")
    table = candidate_table(dataset)
    if has_feature(table, "z_conf"):
        from scripts.eval.climatology import quantile_mapped_thresholds

        zc = quantile_mapped_thresholds(table["h_aval"], table["z_conf"], PESSAC_THRESHOLDS_M)
        lines.append(f"- confluence thresholds (quantile-mapped): recomputed {tuple(round(x, 2) for x in zc)}, "
                     f"risk_v4 {risk_v4.CONFLUENCE_THRESHOLDS_NGF}")
    gr = label_free_q_thresholds(dataset.times, table["q_gr4h"])
    lines.append(f"- GR4H runoff thresholds (0.4/0.7/1.0 x simulated Q2): {tuple(round(x, 2) for x in gr)} m3/s")
    return lines


# Unlabelled periods worth a look (research 2026-10-26), peak times approximate
EXTRA_PERIODS = {
    "U 2023-11-03 (Ciaran)": datetime(2023, 11, 3, 12),
    "U 2024-05-15": datetime(2024, 5, 15, 12),
    "U 2025-01-28": datetime(2025, 1, 28, 12),
    "U 2026-02-14 (Pessac record)": datetime(2026, 2, 14, 11),
}


def mechanism_table_markdown(dataset: EvalDataset, events: Sequence[EvalEvent], model: V4AlphaModel) -> str:
    """Max of each v4-alpha mechanism and raw input over [peak - 72h, peak + 6h] per event."""
    from app.services.models_experimental import _graded_vec

    table = candidate_table(dataset)
    p = model.params
    sb = _graded_vec(table["h_aval"], p.backwater)
    sr = _graded_vec(table[model.runoff_feature], p.runoff)
    so = _graded_vec(table["h_aval"], p.overbank)
    sg = _graded_vec(table["piezo"], p.groundwater)
    joint = np.where(np.isnan(sb) | np.isnan(sr), np.nan, np.minimum(sb, sr))
    lag0 = table.get("q_eyraud_lag0")
    times = np.array(dataset.times, dtype="datetime64[s]")
    cols = [("joint", joint), ("backwater", sb), ("runoff", sr), ("overbank", so), ("gw", sg),
            ("H_aval m", table["h_aval"]), ("Q Eyraud lag7 m3/s", table["q_eyraud"]),
            ("Q Eyraud lag0 m3/s", lag0 if lag0 is not None else np.full(len(times), np.nan)),
            ("Z_c NGF", table["z_conf"]), ("piezo NGF", table["piezo"]), ("GloFAS m3/s", table["q_glofas"]),
            ("rain 72h mm", table["rain_72h"])]
    lines = ["| Event | Outcome | " + " | ".join(c for c, _ in cols) + " |", "|---|---|" + "---|" * len(cols)]
    items = [(e.key, e.outcome.value, e.peak_time) for e in events]
    items += [(k, "unlabelled", t) for k, t in EXTRA_PERIODS.items()]
    for key, outcome, peak in items:
        lo, hi = np.datetime64(peak - timedelta(hours=LEAD_H)), np.datetime64(peak + timedelta(hours=LAG_H))
        m = (times >= lo) & (times <= hi)
        cells = []
        for _, arr in cols:
            seg = np.asarray(arr, dtype=float)[m]
            cells.append(_fmt(float(np.nanmax(seg)), 1) if seg.size and np.isfinite(seg).any() else "-")
        lines.append(f"| {key} | {outcome} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def timing_table_markdown(dataset: EvalDataset, start: datetime, end: datetime, step_h: int = 6) -> str:
    """Inputs every ``step_h`` hours over [start, end] (label timing checks; Eyraud WITHOUT lag)."""
    keys = [("rain_6h", "rain 6h mm"), ("rain_72h", "rain 72h mm"), ("q_eyraud_lag0_m3s", "Q Eyraud m3/s"),
            ("q_caudeau_m3s", "Q Caudeau lag7 m3/s"), ("pessac_h", "Pessac m"), ("pessac_max_12h", "H_aval m"),
            ("libourne_max_12h", "Libourne env m"), ("piezo_ngf", "piezo NGF"), ("glofas_lidoire_m3s", "GloFAS m3/s")]
    lines = ["| Hour (UTC) | " + " | ".join(k[1] for k in keys) + " |", "|---|" + "---|" * len(keys)]
    for r in dataset.rows:
        if start <= r.timestamp <= end and r.timestamp.hour % step_h == 0:
            lines.append(f"| {r.timestamp:%Y-%m-%d %H} | " + " | ".join(
                _fmt(r.values.get(k), 1) for k, _ in keys) + " |")
    return "\n".join(lines)


def series_peak(
    dataset: EvalDataset, key: str, start: datetime, end: datetime,
) -> tuple[Optional[datetime], Optional[float]]:
    best = (None, None)
    for r in dataset.rows:
        v = r.values.get(key)
        if start <= r.timestamp <= end and v is not None and (best[1] is None or v > best[1]):
            best = (r.timestamp, v)
    return best


def gate_sensitivity(dataset: EvalDataset) -> list[Candidate]:
    """v3.3 x backwater gate with other a-priori constants (robustness, not a fit)."""
    out = []
    out += [Candidate(m.name, protocol, 0, replay(m, dataset), "score", model_thresholds(m))
            for m, protocol in v4_alpha_models(dataset, include_sensitivity=True)[-1:]]
    for min_factor in (0.3, 0.5, 0.7):
        for h_low, h_full in ((2.5, 3.5), (3.0, 3.8), (3.4, 4.2)):
            m = V33BackwaterModel(h_low=h_low, h_full=h_full, min_factor=min_factor,
                                  name=f"v3.3+bw[g={min_factor:g},{h_low:g}-{h_full:g}m]")
            out.append(Candidate(m.name, "label-free", 0, replay(m, dataset), "score", model_thresholds(m)))
    return out


def harness_loeo_crosscheck(dataset: EvalDataset, events: Sequence[EvalEvent], period) -> dict:
    """scripts.eval.loeo.leave_one_event_out on the fitted candidates (held-out event judged only)."""

    def wrap(fit):
        def f(ds, train_events):
            return fit(ds, train_events, train_mask=mask_around_missing_events(ds, train_events, events),
                       period=period)
        return f

    out = {}
    fits = [
        ("bw_rule_api_cv", lambda ds, ev, **kw: fit_backwater_rule(ds, ev, forcing="api_fast", **kw)),
        ("bw_rule_gr4h_cv", lambda ds, ev, **kw: fit_backwater_rule(ds, ev, forcing="q_gr4h", **kw)),
        ("lr_api_haval_cv", lambda ds, ev, **kw: fit_logistic(ds, ev, ("api_fast", "h_aval"), **kw)),
        ("lr_gr4h_haval_cv", lambda ds, ev, **kw: fit_logistic(ds, ev, ("q_gr4h", "h_aval"), **kw)),
    ]
    if has_feature(candidate_table(dataset), "q_eyraud"):
        fits += [
            ("bw_rule_eyraud_cv", lambda ds, ev, **kw: fit_backwater_rule(ds, ev, forcing="q_eyraud", **kw)),
            ("lr_eyraud_haval_cv", lambda ds, ev, **kw: fit_logistic(ds, ev, ("q_eyraud", "h_aval"), **kw)),
        ]
    for name, fit in fits:
        res = leave_one_event_out(wrap(fit), dataset, events)
        out[name] = res.to_dict()
    return out


def run(dataset: EvalDataset, out_dir: Path, events=None, period=None, crosscheck: bool = True,
        sensitivity: bool = True) -> dict:
    from scripts.eval import report as rep

    if events is None:
        events, period = load_events()
    blocks = event_blocks(dataset, events)
    candidates = build_candidates(dataset, events, period, blocks)
    config = ContingencyConfig()
    merged_config = replace(config, merge_gap_h=12)
    rows, evals = comparison_rows(candidates, dataset, events, period, blocks, config)
    merged_rows, _ = comparison_rows(candidates, dataset, events, period, blocks, merged_config, with_ci=False)
    loeo = harness_loeo_crosscheck(dataset, events, period) if crosscheck else {}
    sens_rows: list[dict] = []
    if sensitivity:
        sens_rows, _ = comparison_rows(gate_sensitivity(dataset), dataset, events, period, blocks,
                                       merged_config, with_ci=False)

    md = [
        "# Candidate models vs v3.3 and baselines (hourly causal replay)",
        "",
        f"- Period {dataset.meta.get('start')} -> {dataset.meta.get('end')} ({len(dataset)} hours); labels "
        f"{period.start:%Y-%m-%d} -> {period.end:%Y-%m-%d}; 5 floods, 8 labelled negatives, 1 unknown."
        if period else "",
        "- Fitted models: blocked leave-one-event-out (blocks: " + ", ".join(b.key for b in blocks)
        + "); every metric below is out-of-sample for them.",
        "- v3.3 is in-sample (hand-tuned on these episodes). Label-free models use config thresholds or "
        "unlabelled feature quantiles only.",
        "- POD CI: Clopper-Pearson 95%. AUC CI: 90% block bootstrap over the CV blocks. FA raw counts "
        "unlabelled alerts (labels not exhaustive).",
        "",
        "## Event-based contingency (alert runs unmerged)",
        "",
        comparison_markdown(rows),
        "",
        "## Same, alert runs separated by <= 12h merged",
        "",
        comparison_markdown(merged_rows),
        "",
        "## Alert per labelled event (highest level in [peak-72h, peak+6h], lead of the first alert hour)",
        "",
        event_matrix_markdown(evals, events),
        "",
    ]
    if sens_rows:
        md += [
            "## Sensitivity (runs merged <= 12h): v4-alpha without the Eyraud publication lag, v3.4 backwater gate",
            "",
            "Eyraud lag 0 uses data published ~7 h later in reality (optimistic bound). Gate: g = factor below "
            "the low height, 1 above the full height (Pessac 12.5h max).",
            "",
            comparison_markdown(sens_rows),
            "",
        ]
    v4 = next((m for m, _ in v4_alpha_models(dataset) if m.name == V4_HEADLINE), None)
    md += ["## v4-alpha pre-registered thresholds (climatology recomputed from the cache)", ""]
    md += climatology_check(dataset)
    md += ["", f"## v4-alpha mechanisms per event ({V4_HEADLINE}, max over [peak-72h, peak+6h])", "",
           "Scores 0-100 (15 yellow / 35 orange / 55 red); Eyraud lag7 = the model input (published "
           "~7 h late), lag0 = for reading only.", "",
           mechanism_table_markdown(dataset, events, v4), ""]
    for label, (t0, t1) in TIMING_WINDOWS.items():
        if dataset.times and t0 >= dataset.times[0] and t1 <= dataset.times[-1]:
            peaks = []
            for key, name in (("q_eyraud_lag0_m3s", "Eyraud Q"), ("pessac_h", "Pessac H"), ("rain_1h", "rain 1h")):
                t, v = series_peak(dataset, key, t0, t1)
                if t is not None:
                    peaks.append(f"{name} max {v:.2f} at {t:%Y-%m-%d %H}h UTC")
            md += [f"## Timing check: {label}", "", "- " + "; ".join(peaks), "",
                   timing_table_markdown(dataset, t0, t1), ""]
    md += [
        "## Fitted parameters per fold",
        "",
    ]
    for c in candidates:
        if c.folds:
            md.append(f"- **{c.name}**: " + "; ".join(
                f"{f['block']}: " + ", ".join(
                    f"{k}={_fmt(v, 3) if isinstance(v, float) else v}" for k, v in f.items()
                    if k not in ("block", "start")
                ) for f in c.folds
            ))
        elif c.params:
            md.append(f"- **{c.name}**: {c.params}")
    if loeo:
        md += ["", "## Harness leave_one_event_out cross-check (held-out event judged only)", ""]
        for name, res in loeo.items():
            lv = res["levels"]
            md.append(f"- **{name}**: " + "; ".join(
                f"{level}: floods {v['hits']}/{v['n_floods']}, labelled negatives alerted "
                f"{v['labelled_negatives_alerted']}/{v['n_negatives']}, median lead {_fmt(v['median_lead_h'], 0)}h"
                for level, v in lv.items()
            ))
    md.append("")

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.md").write_text("\n".join(md), encoding="utf-8")
    payload = {
        "rows": rows, "merged_rows": merged_rows, "gate_sensitivity_rows": sens_rows, "loeo_crosscheck": loeo,
        "blocks": [{"key": b.key, "start": b.start, "end": b.end} for b in blocks],
        "candidates": [{"name": c.name, "protocol": c.protocol, "n_fitted": c.n_fitted, "kind": c.kind,
                        "thresholds": c.thresholds, "folds": c.folds, "params": c.params} for c in candidates],
    }
    rep.write_json(out_dir / "metrics.json", payload)
    rep.write_csv(out_dir / "oos_scores.csv", [
        {"timestamp": t.isoformat(), **{c.name: c.scores[i] for c in candidates}}
        for i, t in enumerate(dataset.times)
    ])
    return payload


def main(argv: Optional[list[str]] = None) -> int:
    from scripts.eval import report as rep
    from scripts.eval.dataset import build_dataset

    p = argparse.ArgumentParser(prog="python -m scripts.eval.candidates", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--offline", action="store_true", help="cache only, no download")
    p.add_argument("--no-crosscheck", action="store_true", help="skip the harness LOEO cross-check")
    p.add_argument("--out", type=Path, default=rep.OUT_DIR / "candidates")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    t0 = time.time()
    dataset = build_dataset(offline=args.offline)
    print(f"dataset: {len(dataset)} hours ({time.time() - t0:.0f}s)")
    payload = run(dataset, args.out, crosscheck=not args.no_crosscheck)
    print(comparison_markdown(payload["rows"]))
    print(f"\nwrote {args.out}/summary.md, metrics.json, oos_scores.csv ({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
