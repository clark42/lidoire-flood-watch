"""Pre-registered evaluation of v4-beta (``preregistration_v4beta.md``) against v3.4 red.

Run from ``backend/``::

    python -m scripts.eval.v4beta [--offline] [--out data/eval_out/v4beta]

Replays v3.4 (in-sample reference), v4a_joint (previous candidate), the
v4-beta primary, its 3 pre-registered variants and the non-eligible
diagnostics on the hourly causal dataset, then reports:

- the CLAUDE.md table columns (orange / red, alert runs unmerged and merged
  at 12 h, POD with its Clopper-Pearson 95% CI, AUC with its block-bootstrap
  90% CI);
- the lead per flood against the owner's hour-precise peaks (merged 12 h);
- the pre-registered decision rule (v4b orange vs v3.4 red, both recomputed
  here), applied to the primary only;
- variant V1 (archived forecast rain) on the sub-period covered by the
  Previous Runs archive (2024-01-20 onwards);
- the inputs of the v4-beta branches around each labelled event.

Nothing is fitted on labels here: every v4-beta constant comes from the
pre-registration (climatology 2010-09 -> 2022-08, long profile).
"""

from __future__ import annotations

import argparse
import logging
import math
import statistics
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from app.services import risk_v4
from app.services.models_experimental import V4AlphaModel, V4BetaModel
from scripts.eval.budget import above, evaluate_point
from scripts.eval.candidates import (
    Candidate,
    comparison_markdown,
    comparison_rows,
    event_blocks,
    v4_beta_models,
)
from scripts.eval.contingency import ContingencyConfig
from scripts.eval.models import V33Model, model_thresholds, replay
from scripts.eval.truth import EvalEvent, LabelPeriod, load_events

logger = logging.getLogger("scripts.eval.v4beta")

INFILTRATION_KEY = "2023-03-11"
MIN_USEFUL_LEAD_H = 6.0
BUDGET_MAX_PER_YEAR = 10.0
LEAD_SLACK_H = 6.0
FA_RATIO = 0.75
FC_SUBPERIOD_START = datetime(2024, 1, 20)
ELIGIBLE = ("v4b", "v4b_fc", "v4b_mod", "v4b_runoff")


def runoff_floods(events: Sequence[EvalEvent]) -> list[EvalEvent]:
    return [e for e in events if e.positive is True and e.key != INFILTRATION_KEY]


def point_summary(name: str, series: str, threshold: float, scores: dict, times, events, period) -> dict:
    res = evaluate_point(above(series, threshold), scores, times, events, period)
    row = res.row()
    row["label"] = name
    return row


def decision(candidate: dict, reference: dict, floods: Sequence[EvalEvent]) -> dict:
    """Pre-registered rule (section 4): ``candidate`` / ``reference`` are budget rows (merged 12 h)."""
    keys = [e.key for e in floods]
    c_leads = [candidate["leads_h"].get(k) for k in keys]
    r_leads = [reference["leads_h"].get(k) for k in keys]
    no_miss = all(v is not None for v in c_leads)
    c_med = statistics.median(c_leads) if no_miss else None
    r_med = statistics.median([v for v in r_leads if v is not None]) if any(v is not None for v in r_leads) else None
    c_fa, r_fa = candidate["fa_raw"], reference["fa_raw"]
    r1 = candidate["alerts_per_year_merged"] is not None and candidate["alerts_per_year_merged"] <= BUDGET_MAX_PER_YEAR
    r2 = no_miss
    r3 = no_miss and all(v >= MIN_USEFUL_LEAD_H for v in c_leads)
    r4a = no_miss and r_med is not None and c_med >= r_med and c_fa <= r_fa
    r4b = no_miss and r_med is not None and c_med >= r_med - LEAD_SLACK_H and c_fa <= FA_RATIO * r_fa
    return {
        "candidate": candidate["label"], "reference": reference["label"],
        "runoff_floods": keys, "candidate_leads_h": dict(zip(keys, c_leads)),
        "reference_leads_h": dict(zip(keys, r_leads)), "candidate_median_lead_h": c_med,
        "reference_median_lead_h": r_med, "candidate_fa_raw": c_fa, "reference_fa_raw": r_fa,
        "candidate_alerts_per_year": candidate["alerts_per_year_merged"],
        "R1_budget": r1, "R2_no_miss": r2, "R3_useful_lead": r3, "R4a": r4a, "R4b": r4b,
        "passes": bool(r1 and r2 and r3 and (r4a or r4b)),
    }


def decision_markdown(d: dict) -> list[str]:
    def f(v):
        return "-" if v is None else (f"{v:.0f}" if isinstance(v, (int, float)) else str(v))

    ok = {True: "yes", False: "NO"}
    return [
        f"- Candidate **{d['candidate']}** vs reference **{d['reference']}** (merged 12 h, labels v2).",
        "- Leads on the runoff floods (h): " + ", ".join(
            f"{k} {f(d['candidate_leads_h'][k])} vs {f(d['reference_leads_h'][k])}" for k in d["runoff_floods"]),
        f"- Median lead {f(d['candidate_median_lead_h'])} vs {f(d['reference_median_lead_h'])} h; raw false "
        f"alarms {d['candidate_fa_raw']} vs {d['reference_fa_raw']}; alerts/yr {d['candidate_alerts_per_year']:.1f}.",
        f"- R1 budget <= {BUDGET_MAX_PER_YEAR:g}/yr: {ok[d['R1_budget']]}; R2 4 runoff floods detected: "
        f"{ok[d['R2_no_miss']]}; R3 lead >= {MIN_USEFUL_LEAD_H:g} h each: {ok[d['R3_useful_lead']]}; "
        f"R4a (lead >= ref, FA <= ref): {ok[d['R4a']]}; R4b (lead >= ref - {LEAD_SLACK_H:g} h, "
        f"FA <= {FA_RATIO:g} x ref): {ok[d['R4b']]}.",
        f"- **Passes: {ok[d['passes']]}**",
    ]


def leads_markdown(rows: Sequence[dict], floods: Sequence[EvalEvent]) -> str:
    keys = [e.key for e in floods]
    head = ("| Operating point | Alerts/yr (12 h) | ntfy/yr (hold 3 h / 12 h) | POD | FA lab / raw | "
            "Lead med / min (h) | " + " | ".join(keys) + " |")
    lines = [head, "|" + "---|" * (6 + len(keys))]

    def f(v, d=0):
        return "-" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.{d}f}"

    for r in rows:
        na = r["notifier_alerts_per_year"]
        lines.append(
            f"| {r['label']} | {f(r['alerts_per_year_merged'], 1)} | {f(na.get(3.0), 1)} / {f(na.get(12.0), 1)} | "
            f"{r['hits']}/{r['n_floods']} | {r['fa_labelled']} / {r['fa_raw']} | "
            f"{f(r['lead_median_h'])} / {f(r['lead_min_h'])} | "
            + " | ".join(f(r["leads_h"].get(k)) for k in keys) + " |")
    return "\n".join(lines)


def mechanism_markdown(dataset, events: Sequence[EvalEvent]) -> str:
    """Per labelled event, max over [peak - 72 h, peak + 6 h] of the v4-beta branches and inputs."""
    model = V4BetaModel("fc", name="v4b_fc")
    cols = ("runoff", "forecast", "overbank", "q_obs", "q_nowcast", "q_forecast")
    times = dataset.times
    lines = ["| Event | Outcome | R | F (V1) | O | Q_obs lag 7 | Qhat_now | Qhat_fc | Eyraud lag 0 max | H_aval max |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for e in events:
        lo, hi = e.peak_time - timedelta(hours=72), e.peak_time + timedelta(hours=6)
        acc = {c: [] for c in cols}
        q0, hv = [], []
        for t, r in zip(times, dataset.rows):
            if lo <= t <= hi:
                b = model.breakdown(r.values)
                for c in cols:
                    if b.get(c) is not None:
                        acc[c].append(b[c])
                if r.values.get("q_eyraud_lag0_m3s") is not None:
                    q0.append(r.values["q_eyraud_lag0_m3s"])
                if r.values.get("pessac_max_12h") is not None:
                    hv.append(r.values["pessac_max_12h"])
        cells = [f"{max(acc[c]):.1f}" if acc[c] else "-" for c in cols]
        cells += [f"{max(q0):.1f}" if q0 else "-", f"{max(hv):.2f}" if hv else "-"]
        lines.append(f"| {e.key} | {e.outcome.value} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def subperiod(dataset, events, period, start: datetime):
    sub = dataset.subset(start, dataset.times[-1])
    ev = [e for e in events if e.peak_time >= start + timedelta(hours=72)]
    per = LabelPeriod(max(period.start, start), period.end) if period else None
    return sub, ev, per


def run(dataset, out_dir: Path, events=None, period=None) -> dict:
    from scripts.eval import report as rep

    if events is None:
        events, period = load_events()
    blocks = event_blocks(dataset, events)
    t0 = time.time()
    models = [(V33Model(), "in-sample, hand-tuned", 45),
              (V4AlphaModel(risk_v4.default_v4_params(), "q_eyraud", name="v4a_joint"),
               "label-free (pre-registered 2026-10-26)", 0)]
    models += [(m, p, 0) for m, p in v4_beta_models()]
    cands = []
    for m, protocol, n_fit in models:
        cands.append(Candidate(m.name, protocol, n_fit, replay(m, dataset), "score", model_thresholds(m)))
        logger.info("replayed %s (%.0fs)", m.name, time.time() - t0)
    scores = {c.name: c.scores for c in cands}
    config = ContingencyConfig()
    rows, _ = comparison_rows(cands, dataset, events, period, blocks, config)
    merged, _ = comparison_rows(cands, dataset, events, period, blocks, replace(config, merge_gap_h=12),
                                with_ci=False)

    times = dataset.times
    floods = [e for e in events if e.positive is True]
    v3_name = cands[0].name
    points = [("v3.4 >= 55 (red, reference)", v3_name, 55.0), ("v3.4 >= 35 (orange)", v3_name, 35.0),
              ("v4a_joint >= 35", "v4a_joint", 35.0)]
    for name in [c.name for c in cands if c.name.startswith("v4b")]:
        points += [(f"{name} >= 35 (orange)", name, 35.0), (f"{name} >= 55 (red)", name, 55.0)]
    lead_rows = [point_summary(lbl, s, th, scores, times, events, period) for lbl, s, th in points]
    ref = lead_rows[0]
    decisions = [decision(r, ref, runoff_floods(events)) for r in lead_rows
                 if r["label"].endswith("(orange)") and r["label"].split(" ")[0] in ELIGIBLE]

    sub, sub_ev, sub_per = subperiod(dataset, events, period, FC_SUBPERIOD_START)
    sub_idx = len(dataset) - len(sub)
    sub_scores = {k: v[sub_idx:] for k, v in scores.items()}
    sub_points = [("v3.4 >= 55 (red, reference)", v3_name, 55.0), ("v4b >= 35 (orange)", "v4b", 35.0),
                  ("v4b_fc >= 35 (orange)", "v4b_fc", 35.0), ("v4b_fc >= 55 (red)", "v4b_fc", 55.0)]
    sub_rows = [point_summary(lbl, s, th, sub_scores, sub.times, sub_ev, sub_per) for lbl, s, th in sub_points]
    sub_floods = [e for e in sub_ev if e.positive is True]

    md = [
        "# v4-beta (pre-registered 2026-10-27) vs v3.4 red",
        "",
        f"- Period {dataset.meta.get('start')} -> {dataset.meta.get('end')} ({len(dataset)} h); "
        f"{len(floods)} floods, {sum(1 for e in events if e.positive is False)} labelled negatives.",
        f"- {v3_name} = production v3.4 (compute_risk), in-sample; v4a_joint and every v4b are label-free.",
        "- Lead = labelled peak (hour-precise) - alert start; negative = alert after the flood.",
        "",
        "## Decision rule (primary v4b orange vs v3.4 red; variants are not decisive)",
        "",
    ]
    for d in decisions:
        md += decision_markdown(d) + [""]
    md += ["## Lead per flood and budget (alert runs merged at 12 h)", "",
           leads_markdown(lead_rows, floods), "",
           "## Event-based contingency (unmerged)", "", comparison_markdown(rows), "",
           "## Same, merged at 12 h", "", comparison_markdown(merged), "",
           f"## Variant V1 on the archived-forecast sub-period ({FC_SUBPERIOD_START:%Y-%m-%d} -> end)", "",
           leads_markdown(sub_rows, sub_floods), "",
           "## v4-beta branches and inputs per labelled event (max over [peak - 72 h, peak + 6 h])", "",
           "Scores 0-100; Q in Eyraud m3/s (anchors 1.89 / 4.72 / 13.22); F only from 2024-01-19.", "",
           mechanism_markdown(dataset, events), ""]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.md").write_text("\n".join(md), encoding="utf-8")
    payload = {"rows": rows, "merged_rows": merged, "lead_rows": lead_rows, "decisions": decisions,
               "subperiod_rows": sub_rows}
    rep.write_json(out_dir / "metrics.json", payload)
    rep.write_csv(out_dir / "scores.csv", [
        {"timestamp": t.isoformat(), **{c.name: c.scores[i] for c in cands}} for i, t in enumerate(times)
    ])
    payload["markdown"] = "\n".join(md)
    return payload


def main(argv: Optional[list[str]] = None) -> int:
    from scripts.eval import report as rep
    from scripts.eval.dataset import build_dataset

    p = argparse.ArgumentParser(prog="python -m scripts.eval.v4beta", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--offline", action="store_true")
    p.add_argument("--out", type=Path, default=rep.OUT_DIR / "v4beta")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    t0 = time.time()
    payload = run(build_dataset(offline=args.offline), args.out)
    print(payload["markdown"])
    print(f"\nwrote {args.out}/summary.md, metrics.json, scores.csv ({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
