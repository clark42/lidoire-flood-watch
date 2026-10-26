"""CLI: ``cd backend && python -m scripts.eval build|replay|report [options]``.

- build:  download (or read from cache) the public archives and write the
          hourly feature dataset (dataset.csv + dataset_meta.json).
- replay: build + hourly scores of v3.3 and the baselines (replay.csv).
- report: replay + metrics (metrics.json, alerts.csv, events.csv,
          unlabelled_alerts.csv, summary.md).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import replace
from datetime import date
from pathlib import Path

from scripts.eval import report as rep
from scripts.eval.contingency import ContingencyConfig
from scripts.eval.dataset import DEFAULT_END, DEFAULT_START, BuildOptions, build_dataset, collect_raw_inputs
from scripts.eval.loeo import fit_pessac_threshold, leave_one_event_out
from scripts.eval.models import V33Model, default_models, evaluate_model, replay
from scripts.eval.truth import load_events

logger = logging.getLogger("scripts.eval")


def _parse_thresholds(values: list[str]) -> dict[str, float] | None:
    """``--threshold 35 --threshold red=55`` -> {"t35": 35, "red": 55}."""
    if not values:
        return None
    out: dict[str, float] = {}
    for v in values:
        if "=" in v:
            name, num = v.split("=", 1)
            out[name.strip()] = float(num)
        else:
            out[f"t{float(v):g}"] = float(v)
    return out


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m scripts.eval", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=("build", "replay", "report"))
    p.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    p.add_argument("--end", type=date.fromisoformat, default=DEFAULT_END)
    p.add_argument("--threshold", action="append", default=[],
                   help="alert threshold, repeatable: 35 or orange=35 (default orange=35 red=55)")
    p.add_argument("--offline", action="store_true", help="cache only, no download")
    p.add_argument("--refresh", action="store_true", help="re-download even if cached")
    p.add_argument("--level-mode", choices=("causal", "interp"), default="causal",
                   help="interp uses the next archived point (sensitivity only)")
    p.add_argument("--forecast-rain", choices=("none", "oracle"), default="none",
                   help="oracle = observed future rain as forecast (upper bound, leaks)")
    p.add_argument("--no-tide", action="store_true", help="tide inputs unavailable")
    p.add_argument("--tide-lag", type=float, default=None,
                   help="hours from the Gironde mouth to the confluence (default: settings)")
    p.add_argument("--merge-gap", type=int, default=0, help="merge alerts separated by <= N hours")
    p.add_argument("--lead-window", type=float, default=72.0)
    p.add_argument("--lag-window", type=float, default=6.0)
    p.add_argument("--strict-start", action="store_true",
                   help="only alerts STARTING in the window match (no ongoing alerts)")
    p.add_argument("--sensitivity", action="store_true",
                   help="report: also run v3.3 with interp levels, no tide and oracle rain")
    p.add_argument("--out", type=Path, default=rep.OUT_DIR)
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.end < args.start:
        print("--end must be after --start", file=sys.stderr)
        return 2

    t0 = time.time()
    options = BuildOptions(level_mode=args.level_mode, forecast_rain=args.forecast_rain,
                           use_tide=not args.no_tide, tide_lag_hours=args.tide_lag)
    raw, sources_meta = collect_raw_inputs(args.start, args.end, offline=args.offline, refresh=args.refresh)
    dataset = build_dataset(args.start, args.end, options, raw=raw, sources_meta=sources_meta)
    print(f"dataset: {len(dataset)} hours, coverage {dataset.meta['coverage']} ({time.time() - t0:.0f}s)")

    if args.command == "build":
        for path in rep.write_dataset(dataset, args.out):
            print(f"wrote {path}")
        return 0

    models = default_models()
    scores = {m.name: replay(m, dataset) for m in models}
    if args.command == "replay":
        rep.write_dataset(dataset, args.out)
        print(f"wrote {rep.write_replay(dataset, scores, args.out)}")
        return 0

    config = ContingencyConfig(lead_window_h=args.lead_window, lag_window_h=args.lag_window,
                               allow_ongoing=not args.strict_start, merge_gap_h=args.merge_gap)
    events, period = load_events()
    thresholds = _parse_thresholds(args.threshold)
    evaluations = [
        evaluate_model(m, dataset, events, period, thresholds, config, scores=scores[m.name])
        for m in models
    ]

    extra: list[str] = []
    labelling = None
    strict = replace(config, allow_ongoing=False)
    strict_evals = [
        evaluate_model(m, dataset, events, period, thresholds, strict, scores=scores[m.name])
        for m in models
    ]
    extra += ["## Variant: strict start (ongoing alerts do not match)", "",
              rep.metrics_table(strict_evals), ""]
    if args.merge_gap == 0:
        merged = replace(config, merge_gap_h=12)
        merged_evals = [
            evaluate_model(m, dataset, events, period, thresholds, merged, scores=scores[m.name])
            for m in models
        ]
        labelling = merged_evals[0]
        extra += ["## Variant: alerts separated by <= 12h merged (hysteresis)", "",
                  rep.metrics_table(merged_evals), ""]

    if args.sensitivity:
        sens = []
        for label, opts in (
            (f"{V33Model().name} [interp levels]", replace(options, level_mode="interp")),
            (f"{V33Model().name} [no tide]", replace(options, use_tide=False)),
            (f"{V33Model().name} [oracle rain forecast]", replace(options, forecast_rain="oracle")),
        ):
            ds = build_dataset(args.start, args.end, opts, raw=raw, sources_meta=sources_meta)
            model = V33Model(name=label)
            sens.append(evaluate_model(model, ds, events, period, thresholds, config))
        extra += ["## Sensitivity (v3.3, same labels; interp/oracle use future data)", "",
                  rep.metrics_table(sens), "", rep.hourly_table(sens), ""]

    # LOEO demo with the 1-parameter example fit (template for candidate models)
    loeo = leave_one_event_out(fit_pessac_threshold, dataset, events, config=config).to_dict()
    lv = loeo["levels"].get("fitted", {})
    extra += [
        "## Leave-one-event-out demo (loeo.fit_pessac_threshold, 1 parameter)", "",
        f"- held-out floods alerted: {lv.get('hits')}/{lv.get('n_floods')}; held-out labelled "
        f"negatives alerted: {lv.get('labelled_negatives_alerted')}/{lv.get('n_negatives')}; "
        f"median lead {lv.get('median_lead_h')} h",
        "- folds: " + ", ".join(
            f"{f['held_out']} ({f['outcome']}): {'alert' if f['matched'].get('fitted') else 'no alert'}"
            for f in loeo["folds"]
        ),
        "",
    ]

    rep.write_dataset(dataset, args.out)
    rep.write_replay(dataset, scores, args.out)
    for path in rep.write_report(dataset, evaluations, args.out, extra, loeo=loeo, labelling=labelling):
        print(f"wrote {path}")
    print()
    print(rep.metrics_table(evaluations))
    print()
    print(rep.hourly_table(evaluations))
    print(f"\ndone in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
