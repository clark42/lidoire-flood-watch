"""Alerting operating points under an alert budget (notifier policy study).

Run from ``backend/``::

    python -m scripts.eval.budget [--offline] [--out data/eval_out/budget]

The owner accepts about 5-10 alerts per year (alert runs separated by
<= 12 h merged) and prefers false alarms over misses. For each operating
point (one series at one threshold, or a combination of series), this
module reports on the hourly causal replay:

- alerts per year (runs merged at 12 h, whole replay) and the number of
  ntfy "alert" messages the notifier would send (rise to the trigger level,
  downgrade after ``hold`` hours, :func:`simulate_notifier` mirrors
  ``app.services.notifier.level_event`` on the two-state rule series), and
  for v3.4 the messages of the real ``level_event`` under each
  :data:`POLICIES` (``notify_min_level`` + hold, :func:`replay_notifier`;
  for red, identical to the simulation at >= 55, see the tests);
- POD with its Clopper-Pearson 95% interval, labelled FAR (alerts matching
  only near_miss / false_positive labels), raw FAR;
- lead time (peak - alert start) against the labelled peaks, median / min,
  and the min minus the peak time precision (worst case within the
  owner's stated uncertainty).

Series: v3.4 (production, in-sample), the stored shadow 3.4-gate
(v3.4 x backwater gate) and v4a_joint (pre-registered, label-free; Eyraud Q
with its 7 h publication lag). Combinations are hourly booleans (OR / AND
of exceedances). Nothing here is fitted: thresholds are listed a priori,
but choosing the best one among them on the same 5 floods is in-sample.
"""

from __future__ import annotations

import argparse
import logging
import math
import statistics
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional, Sequence

from scripts.eval.contingency import HOURS_PER_YEAR, ContingencyConfig, ContingencyResult, evaluate_scores
from scripts.eval.truth import EvalEvent, LabelPeriod, load_events

logger = logging.getLogger("scripts.eval.budget")

MERGE_GAP_H = 12
BUDGET_PER_YEAR = (5.0, 10.0)

Scores = Sequence[Optional[float]]
Rule = Callable[[dict[str, Optional[float]]], Optional[bool]]


@dataclass(frozen=True)
class OperatingPoint:
    name: str
    rule: Rule
    inputs: tuple[str, ...]
    trigger_level: str = "orange"  # which notifier level the rule maps to


def above(series: str, threshold: float) -> OperatingPoint:
    def rule(v):
        s = v.get(series)
        return None if s is None else s >= threshold
    return OperatingPoint(f"{series} >= {threshold:g}", rule, (series,))


def any_of(*points: OperatingPoint) -> OperatingPoint:
    def rule(v):
        res = [p.rule(v) for p in points]
        if any(r is True for r in res):
            return True
        return None if all(r is None for r in res) else False
    return OperatingPoint(" OR ".join(p.name for p in points), rule, tuple(i for p in points for i in p.inputs))


def all_of(*points: OperatingPoint) -> OperatingPoint:
    def rule(v):
        res = [p.rule(v) for p in points]
        if any(r is None for r in res):
            return None
        return all(res)
    return OperatingPoint(" AND ".join(p.name for p in points), rule, tuple(i for p in points for i in p.inputs))


def indicator(point: OperatingPoint, series: dict[str, Scores], n: int) -> list[Optional[float]]:
    """100 when the rule fires, 0 when not, None when no input is scored."""
    out: list[Optional[float]] = []
    for i in range(n):
        r = point.rule({k: series[k][i] for k in point.inputs})
        out.append(None if r is None else (100.0 if r else 0.0))
    return out


def simulate_notifier(
    times: Sequence[datetime], fired: Sequence[Optional[float]], hold_h: float,
) -> tuple[int, int]:
    """(alert messages, all level messages) of the notifier on a two-state series.

    Mirrors ``notifier.level_event``: a rise to the trigger level is sent at
    once, a drop only after it has held ``hold_h`` hours (a rise during the
    hold cancels it, no message). Hours without a score are skipped (no
    stored score = no event), as in production.
    """
    level = 0
    pending_since: Optional[datetime] = None
    alerts = messages = 0
    hold = timedelta(hours=hold_h)
    for t, s in zip(times, fired):
        if s is None:
            continue
        r = 1 if s >= 50 else 0
        if r > level:
            level, pending_since = r, None
            alerts += 1
            messages += 1
        elif r == level:
            pending_since = None
        else:
            since = pending_since or t
            if t - since < hold:
                pending_since = since
            else:
                level, pending_since = r, None
                messages += 1
    return alerts, messages


@dataclass(frozen=True)
class NotifierPolicy:
    """Production notifier settings replayed by :func:`replay_notifier`."""

    name: str
    min_level: str
    hold_h: float
    notify_yellow: bool = False

    def settings(self):
        from app.config import Settings

        # Init values win over the environment: the replay never reads .env
        return Settings(
            _env_file=None, notify_min_level=self.min_level,
            notify_downgrade_hold_hours=self.hold_h, notify_yellow=self.notify_yellow,
        )


# Default of the code, and the owner-approved production policy (Helm values)
POLICIES = (
    NotifierPolicy("orange, hold 3 h (default)", "orange", 3.0),
    NotifierPolicy("orange, hold 12 h", "orange", 12.0),
    NotifierPolicy("red, hold 12 h (production)", "red", 12.0),
)


def replay_notifier(times: Sequence[datetime], scores: Scores, settings) -> tuple[int, int]:
    """(alert messages, all level messages) of the real ``notifier.level_event``.

    Same counting as :func:`simulate_notifier`, but the levels, the minimum
    level (``notify_min_level``), ``notify_yellow`` and the downgrade hold
    are those of the production code. An alert is a delivered rise (orange
    -> red counts again under the orange minimum); hours without a score are
    skipped.
    """
    from app.services import notifier as nt
    from app.services.risk_core import score_to_level

    state = nt.NotifierState()
    ctx = nt.Context()
    alerts = messages = 0
    for t, s in zip(times, scores):
        if s is None:
            continue
        before = state.level
        event = nt.level_event(state, t, float(s), score_to_level(float(s)).value, ctx, settings)
        if event is None:
            continue
        if event.notification is not None:
            messages += 1
            if nt.level_rank(event.updates.get("level", before)) > nt.level_rank(before):
                alerts += 1
        state.apply(event.updates)
    return alerts, messages


def policy_rows(times: Sequence[datetime], scores: Scores, policies=POLICIES) -> list[dict]:
    """Real-notifier alerts / messages per year on the v3.4 replay, per policy."""
    years = len(times) / HOURS_PER_YEAR
    rows = []
    for p in policies:
        a, m = replay_notifier(times, scores, p.settings())
        rows.append({"policy": p.name, "alerts_per_year": a / years, "messages_per_year": m / years})
    return rows


def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    from scripts.eval.candidates import clopper_pearson as cp

    return cp(k, n, alpha)


@dataclass
class PointResult:
    name: str
    merged: ContingencyResult
    unmerged: ContingencyResult
    notifier_alerts_per_year: dict[float, float]
    notifier_messages_per_year: dict[float, float]
    leads: dict[str, Optional[float]]
    precision: dict[str, Optional[float]]

    def row(self) -> dict:
        r = self.merged
        n_fl = r.hits + r.misses
        lo, hi = clopper_pearson(r.hits, n_fl) if n_fl else (None, None)
        worst = [
            self.leads[k] - (self.precision.get(k) or 0.0)
            for k in self.leads if self.leads[k] is not None
        ]
        return {
            "point": self.name,
            "alerts_per_year_merged": r.alerts_per_year,
            "alerts_per_year_unmerged": self.unmerged.alerts_per_year,
            "notifier_alerts_per_year": self.notifier_alerts_per_year,
            "notifier_messages_per_year": self.notifier_messages_per_year,
            "hits": r.hits, "n_floods": n_fl, "pod": r.pod, "pod_ci95": (lo, hi),
            "alerts_judged": r.n_alerts_judged, "fa_labelled": r.false_alarms_labelled,
            "fa_raw": r.false_alarms_raw, "far_labelled": r.far_labelled, "far_raw": r.far_raw,
            "csi_raw": r.csi_raw, "lead_median_h": r.median_lead_h, "lead_min_h": r.min_lead_h,
            "lead_min_minus_precision_h": min(worst) if worst else None,
            "leads_h": self.leads,
            "in_budget": r.alerts_per_year is not None and r.alerts_per_year <= BUDGET_PER_YEAR[1],
        }


def evaluate_point(
    point: OperatingPoint,
    series: dict[str, Scores],
    times: Sequence[datetime],
    events: Sequence[EvalEvent],
    period: Optional[LabelPeriod],
    holds: Sequence[float] = (3.0, 12.0),
) -> PointResult:
    fired = indicator(point, series, len(times))
    base = ContingencyConfig()
    merged = evaluate_scores(times, fired, events, 50.0, replace(base, merge_gap_h=MERGE_GAP_H), period)
    unmerged = evaluate_scores(times, fired, events, 50.0, base, period)
    years = len(times) / HOURS_PER_YEAR
    notif_a, notif_m = {}, {}
    for h in holds:
        a, m = simulate_notifier(times, fired, h)
        notif_a[h], notif_m[h] = a / years, m / years
    floods = [e for e in events if e.positive is True]
    by_key = {e.key: e for e in merged.events}
    leads = {e.key: (by_key[e.key].lead_time_h if e.key in by_key and by_key[e.key].matched else None)
             for e in floods}
    precision = {e.key: e.peak_time_precision_hours for e in floods}
    return PointResult(point.name, merged, unmerged, notif_a, notif_m, leads, precision)


def default_points() -> list[OperatingPoint]:
    v3 = [above("v3.4", t) for t in (35, 45, 50, 55, 60, 65, 75, 85)]
    gate = [above("3.4-gate", t) for t in (35, 45, 55)]
    v4 = [above("v4a_joint", t) for t in (15, 25, 35, 55)]
    combos = [
        any_of(above("v3.4", 55), above("v4a_joint", 35)),
        any_of(above("v3.4", 65), above("v4a_joint", 35)),
        any_of(above("v3.4", 75), above("v4a_joint", 35)),
        any_of(above("v3.4", 45), above("v4a_joint", 35)),
        any_of(above("3.4-gate", 55), above("v4a_joint", 35)),
        all_of(above("v3.4", 35), above("v4a_joint", 15)),
        all_of(above("v3.4", 55), above("v4a_joint", 15)),
        all_of(above("v3.4", 35), above("3.4-gate", 35)),
    ]
    return v3 + gate + v4 + combos


def replay_series(dataset) -> dict[str, list[Optional[float]]]:
    from app.services import risk_v4
    from app.services.models_experimental import V33BackwaterModel, V4AlphaModel
    from scripts.eval.models import V33Model, replay

    out = {}
    for name, model in (
        ("v3.4", V33Model()),
        ("3.4-gate", V33BackwaterModel()),
        ("v4a_joint", V4AlphaModel(risk_v4.default_v4_params(), "q_eyraud", name="v4a_joint")),
    ):
        t0 = time.time()
        out[name] = replay(model, dataset)
        logger.info("replayed %s (%.0fs)", name, time.time() - t0)
    return out


def _f(v, digits=1, pct=False) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    return f"{100 * v:.0f}%" if pct else f"{v:.{digits}f}"


def budget_markdown(rows: Sequence[dict], flood_keys: Sequence[str]) -> str:
    head = ("| Operating point | Alerts/yr (12 h merge) | ntfy alerts/yr (hold 3 h / 12 h) | POD [95% CI] "
            "| FA lab / raw | FAR lab | FAR raw | Lead med / min (h) | Min - precision (h) | "
            + " | ".join(f"Lead {k}" for k in flood_keys) + " |")
    lines = [head, "|" + "---|" * (9 + len(flood_keys))]
    for r in rows:
        lo, hi = r["pod_ci95"]
        ci = f" [{100 * lo:.0f}-{100 * hi:.0f}]" if lo is not None else ""
        na = r["notifier_alerts_per_year"]
        lines.append(
            f"| {r['point']}{' (in budget)' if r['in_budget'] else ''} | {_f(r['alerts_per_year_merged'])} | "
            f"{_f(na.get(3.0))} / {_f(na.get(12.0))} | {r['hits']}/{r['n_floods']}{ci} | "
            f"{r['fa_labelled']} / {r['fa_raw']} | {_f(r['far_labelled'], pct=True)} | {_f(r['far_raw'], pct=True)} | "
            f"{_f(r['lead_median_h'], 0)} / {_f(r['lead_min_h'], 0)} | {_f(r['lead_min_minus_precision_h'], 0)} | "
            + " | ".join(_f(r["leads_h"].get(k), 0) for k in flood_keys) + " |"
        )
    return "\n".join(lines)


def run(dataset, out_dir: Path, events=None, period=None, series=None) -> dict:
    from scripts.eval import report as rep

    if events is None:
        events, period = load_events()
    series = series or replay_series(dataset)
    times = dataset.times
    results = [evaluate_point(p, series, times, events, period) for p in default_points()]
    rows = [r.row() for r in results]
    policies = policy_rows(times, series["v3.4"])
    floods = [e for e in events if e.positive is True]
    keys = [e.key for e in floods]
    md = [
        "# Alert budget: operating points (hourly causal replay)",
        "",
        f"- Period {dataset.meta.get('start')} -> {dataset.meta.get('end')} ({len(dataset)} h, "
        f"{len(times) / HOURS_PER_YEAR:.2f} years); budget {BUDGET_PER_YEAR[0]:g}-{BUDGET_PER_YEAR[1]:g} "
        f"alerts/yr (runs merged at {MERGE_GAP_H} h).",
        "- Floods (peak UTC, +/- precision h): " + ", ".join(
            f"{e.key} {e.peak_time:%H:%M} +/-{_f(e.peak_time_precision_hours, 1)}" for e in floods),
        "- Lead = peak - alert start (negative: the alert starts after the labelled peak); "
        "'Min - precision' subtracts each flood's peak time uncertainty.",
        "- ntfy alerts: simulated rises of the notifier to the trigger level, with the downgrade hold "
        "NOTIFY_DOWNGRADE_HOLD_HOURS = 3 h (today) or 12 h.",
        "- v3.4 is in-sample; 3.4-gate and v4a_joint are label-free; picking the best row on 5 floods is in-sample.",
        "",
        budget_markdown(rows, keys),
        "",
        "## Real notifier (notifier.level_event) on the v3.4 replay",
        "",
        "Alerts = delivered rises (under the orange minimum, orange -> red counts again).",
        "",
        "| Policy | ntfy alerts/yr | ntfy level messages/yr |",
        "|---|---|---|",
        *(f"| {p['policy']} | {_f(p['alerts_per_year'])} | {_f(p['messages_per_year'])} |" for p in policies),
        "",
    ]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.md").write_text("\n".join(md), encoding="utf-8")
    rep.write_json(out_dir / "metrics.json", {"rows": rows, "policies": policies})
    return {"rows": rows, "policies": policies, "markdown": "\n".join(md)}


def main(argv: Optional[list[str]] = None) -> int:
    from scripts.eval import report as rep
    from scripts.eval.dataset import build_dataset

    p = argparse.ArgumentParser(prog="python -m scripts.eval.budget", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--offline", action="store_true", help="cache only, no download")
    p.add_argument("--out", type=Path, default=rep.OUT_DIR / "budget")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    t0 = time.time()
    dataset = build_dataset(offline=args.offline)
    payload = run(dataset, args.out)
    print(payload["markdown"])
    print(f"\nwrote {args.out}/summary.md, metrics.json ({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
