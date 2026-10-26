"""Does Pessac add information once the Lidoire runoff (Eyraud Q) and rain are known?

Run from ``backend/``::

    python -m scripts.eval.added_value [--offline] [--out data/eval_out/added_value]

Nested L2 logistic regressions on "flood within the next 24 h"
(:func:`scripts.eval.candidates.fit_logistic`, same blocked
leave-one-event-out protocol as the candidates): every score is
out-of-sample. For each feature set: hourly ROC-AUC, partial AUC up to a
false-positive rate of 5% (normalised by 0.05, so 1 = perfect), and the
paired block-bootstrap interval of the AUC difference with the base set
(Eyraud + rain 72 h). Run twice: with the 5 labelled floods, and without
the 2023-03-11 infiltration flood (mechanism since sealed, excluded like an
unknown episode).

The per-event table gives the max of each input over [peak - 72 h, peak + 6 h]
and, per input, the gap between the lowest flood and the highest labelled
negative (> 0: that input alone separates the labelled events).

Five floods: every difference below is indicative, none is significant.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from scripts.eval.candidates import (
    LEAD_H,
    LAG_H,
    blocked_cv,
    candidate_table,
    event_blocks,
    fit_logistic,
)
from scripts.eval.hourly import HourlyTargetConfig, hourly_targets, roc_auc
from scripts.eval.truth import EvalEvent, load_events

logger = logging.getLogger("scripts.eval.added_value")

BASE = ("q_eyraud", "rain_72h")
FEATURE_SETS: tuple[tuple[str, ...], ...] = (
    ("rain_72h",),
    ("q_eyraud",),
    BASE,
    BASE + ("h_aval",),
    BASE + ("z_conf",),
    BASE + ("api_slow",),
    BASE + ("piezo",),
    ("rain_72h", "h_aval"),
    ("h_aval",),
)
EVENT_INPUTS = (("q_eyraud_lag0", "Q Eyraud (no lag) m3/s"), ("q_eyraud", "Q Eyraud lag 7 h m3/s"),
                ("rain_72h", "rain 72 h mm"), ("api_slow", "API slow"), ("h_aval", "H_aval m"),
                ("piezo", "piezo m NGF"))
INFILTRATION_KEY = "2023-03-11"


def partial_auc(scores: np.ndarray, targets: np.ndarray, max_fpr: float = 0.05) -> Optional[float]:
    """Area under the ROC curve for FPR in [0, max_fpr], divided by max_fpr (ties as one step)."""
    ok = ~np.isnan(scores) & (targets >= 0)
    s, y = scores[ok], targets[ok]
    n_pos, n_neg = int(y.sum()), int((1 - y).sum())
    if n_pos == 0 or n_neg == 0:
        return None
    order = np.argsort(-s, kind="mergesort")
    s, y = s[order], y[order]
    distinct = np.r_[np.flatnonzero(np.diff(s)), len(s) - 1]
    tpr = np.r_[0.0, np.cumsum(y)[distinct] / n_pos]
    fpr = np.r_[0.0, np.cumsum(1 - y)[distinct] / n_neg]
    stop = np.searchsorted(fpr, max_fpr, side="right")
    xs = list(fpr[:stop]) + [max_fpr]
    ys = list(tpr[:stop]) + [float(np.interp(max_fpr, fpr, tpr))]
    area = sum((x1 - x0) * (y0 + y1) / 2 for x0, x1, y0, y1 in zip(xs, xs[1:], ys, ys[1:]))
    return float(area / max_fpr)


def paired_auc_diff(
    a: np.ndarray, b: np.ndarray, y: np.ndarray, blocks, n_boot: int = 300, seed: int = 20261026,
) -> tuple[float, float]:
    """5-95% block-bootstrap interval of AUC(a) - AUC(b) on the same resampled hours."""
    per_block = []
    for blk in blocks:
        idx = np.arange(blk.start, blk.end)
        per_block.append(idx[(y[idx] >= 0) & ~np.isnan(a[idx]) & ~np.isnan(b[idx])])
    rng = np.random.default_rng(seed)
    diffs = []
    for _ in range(n_boot):
        idx = np.concatenate([per_block[i] for i in rng.integers(0, len(per_block), len(per_block))])
        ra, rb = roc_auc(a[idx], y[idx]), roc_auc(b[idx], y[idx])
        if ra is not None and rb is not None:
            diffs.append(ra - rb)
    lo, hi = np.percentile(diffs, (5, 95)) if diffs else (math.nan, math.nan)
    return float(lo), float(hi)


def _arr(v: Sequence[Optional[float]]) -> np.ndarray:
    return np.array([np.nan if x is None else x for x in v], dtype=float)


def without_infiltration(events: Sequence[EvalEvent]) -> list[EvalEvent]:
    return [replace(e, positive=None) if e.key == INFILTRATION_KEY else e for e in events]


def nested_models(dataset, events, period) -> list[dict]:
    blocks = event_blocks(dataset, events)
    y = np.array([-1 if t is None else t for t in hourly_targets(dataset.times, events, period,
                                                                HourlyTargetConfig())], dtype=int)
    scores = {}
    for fs in FEATURE_SETS:
        t0 = time.time()
        name = "+".join(fs)
        r = blocked_cv(lambda ds, ev, fs=fs, **kw: fit_logistic(ds, ev, fs, **kw), dataset, events, blocks,
                       period, name)
        scores[fs] = _arr(r.scores)
        logger.info("blocked CV %s (%.0fs)", name, time.time() - t0)
    base = scores[BASE]
    rows = []
    for fs in FEATURE_SETS:
        s = scores[fs]
        ok = (y >= 0) & ~np.isnan(s)
        auc = roc_auc(s[ok], y[ok])
        diff = paired_auc_diff(s, base, y, blocks) if fs != BASE else None
        rows.append({"features": "+".join(fs), "auc": auc, "pauc_5": partial_auc(s, y),
                     "auc_minus_base_ci90": diff, "n_pos": int(y[ok].sum()), "n_hours": int(ok.sum())})
    return rows


def event_table(dataset, events) -> tuple[list[dict], dict]:
    table = candidate_table(dataset)
    times = np.array(dataset.times, dtype="datetime64[s]")
    rows = []
    for e in events:
        lo = np.datetime64(e.peak_time - timedelta(hours=LEAD_H))
        hi = np.datetime64(e.peak_time + timedelta(hours=LAG_H))
        m = (times >= lo) & (times <= hi)
        row = {"key": e.key, "outcome": e.outcome.value}
        for k, _ in EVENT_INPUTS:
            seg = table[k][m]
            row[k] = float(np.nanmax(seg)) if seg.size and np.isfinite(seg).any() else None
        rows.append(row)
    gaps = {}
    for k, _ in EVENT_INPUTS:
        fl = [r[k] for r, e in zip(rows, events) if e.positive is True and r[k] is not None]
        neg = [r[k] for r, e in zip(rows, events) if e.positive is False and r[k] is not None]
        fl_ok = [r[k] for r, e in zip(rows, events)
                 if e.positive is True and e.key != INFILTRATION_KEY and r[k] is not None]
        gaps[k] = {
            "min_flood": min(fl) if fl else None, "max_negative": max(neg) if neg else None,
            "gap": (min(fl) - max(neg)) if fl and neg else None,
            "gap_without_infiltration": (min(fl_ok) - max(neg)) if fl_ok and neg else None,
        }
    return rows, gaps


def _f(v, d=3) -> str:
    return "-" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.{d}f}"


def markdown(results: dict, ev_rows: list[dict], gaps: dict) -> str:
    md = ["# Added value of Pessac (H_aval) given Eyraud Q and rain (blocked LOEO, out-of-sample)", ""]
    for label, rows in results.items():
        md += [f"## {label}", "",
               "| Features | ROC-AUC | pAUC (FPR <= 5%) | AUC - base [90% CI] |", "|---|---|---|---|"]
        for r in rows:
            ci = r["auc_minus_base_ci90"]
            md.append(f"| {r['features']} | {_f(r['auc'])} | {_f(r['pauc_5'])} | "
                      + (f"{ci[0]:+.3f} to {ci[1]:+.3f}" if ci else "(base)") + " |")
        md.append("")
    md += ["## Inputs per labelled event (max over [peak - 72 h, peak + 6 h])", "",
           "| Event | Outcome | " + " | ".join(n for _, n in EVENT_INPUTS) + " |",
           "|---|---|" + "---|" * len(EVENT_INPUTS)]
    for r in ev_rows:
        md.append(f"| {r['key']} | {r['outcome']} | " + " | ".join(_f(r[k], 1) for k, _ in EVENT_INPUTS) + " |")
    md += ["", "| Input | lowest flood | highest labelled negative | gap | gap without 2023-03-11 |",
           "|---|---|---|---|---|"]
    for k, n in EVENT_INPUTS:
        g = gaps[k]
        md.append(f"| {n} | {_f(g['min_flood'], 2)} | {_f(g['max_negative'], 2)} | {_f(g['gap'], 2)} | "
                  f"{_f(g['gap_without_infiltration'], 2)} |")
    md.append("")
    return "\n".join(md)


def run(dataset, out_dir: Path) -> dict:
    from scripts.eval import report as rep

    events, period = load_events()
    results = {
        "5 floods": nested_models(dataset, events, period),
        "4 floods (2023-03-11 infiltration excluded)": nested_models(dataset, without_infiltration(events), period),
    }
    ev_rows, gaps = event_table(dataset, events)
    md = markdown(results, ev_rows, gaps)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.md").write_text(md, encoding="utf-8")
    rep.write_json(out_dir / "metrics.json", {"nested": results, "events": ev_rows, "gaps": gaps})
    return {"markdown": md}


def main(argv: Optional[list[str]] = None) -> int:
    from scripts.eval import report as rep
    from scripts.eval.dataset import build_dataset

    p = argparse.ArgumentParser(prog="python -m scripts.eval.added_value", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--offline", action="store_true")
    p.add_argument("--out", type=Path, default=rep.OUT_DIR / "added_value")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    t0 = time.time()
    payload = run(build_dataset(offline=args.offline), args.out)
    print(payload["markdown"])
    print(f"wrote {args.out}/summary.md, metrics.json ({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
