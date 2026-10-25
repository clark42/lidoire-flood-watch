"""Output files (CSV/JSON/markdown) of the evaluation, under backend/data/eval_out/."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from app.services.risk_core import compute_risk, get_model_params
from scripts.eval.dataset import EvalDataset
from scripts.eval.models import ModelEvaluation
from scripts.eval.sources import BACKEND_DIR

OUT_DIR = BACKEND_DIR / "data" / "eval_out"


def _json_default(o):
    if isinstance(o, datetime):
        return o.isoformat()
    if hasattr(o, "value"):
        return o.value
    raise TypeError(f"not serializable: {type(o)}")


def write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, default=_json_default)
    return path


def write_csv(path: Path, rows: Sequence[dict], fieldnames: Optional[Sequence[str]] = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = []
        for r in rows:
            for k in r:
                if k not in fieldnames:
                    fieldnames.append(k)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(fieldnames), extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: _cell(r.get(k)) for k in fieldnames})
    return path


def _cell(v):
    if isinstance(v, float):
        return f"{v:.4g}"
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return ";".join(str(x) for x in v)
    return v


def dataset_rows(dataset: EvalDataset) -> list[dict]:
    return [{"timestamp": r.timestamp.isoformat(), **r.values} for r in dataset.rows]


def write_dataset(dataset: EvalDataset, out_dir: Path = OUT_DIR) -> list[Path]:
    return [
        write_csv(out_dir / "dataset.csv", dataset_rows(dataset)),
        write_json(out_dir / "dataset_meta.json", dataset.meta),
    ]


def write_replay(
    dataset: EvalDataset,
    scores: dict[str, Sequence[Optional[float]]],
    out_dir: Path = OUT_DIR,
) -> Path:
    """Hourly scores of every model + the v3.3 breakdown (auditable)."""
    params = get_model_params()
    rows = []
    for i, row in enumerate(dataset.rows):
        entry: dict = {"timestamp": row.timestamp.isoformat()}
        for name, s in scores.items():
            entry[name] = s[i]
        if row.features.stations:
            bd = compute_risk(row.features, params)
            entry.update({
                "v33_level": bd.level.value,
                "v33_river_score": bd.river_score,
                "v33_conjunction": bd.conjunction_multiplier,
                "v33_dampener": bd.stability_dampener,
                "v33_n_active": bd.n_active,
                "v33_n_rising": bd.n_rising,
                "v33_n_above_orange": bd.n_above_orange,
                "v33_tide_available": bd.tide_available,
            })
        rows.append(entry)
    return write_csv(out_dir / "replay.csv", rows)


def _fmt(v, pct=False, digits=2) -> str:
    if v is None:
        return "-"
    if pct:
        return f"{100 * v:.1f}%" if 0 < v < 0.1 else f"{100 * v:.0f}%"
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def metrics_table(evaluations: Sequence[ModelEvaluation]) -> str:
    lines = [
        "| Model | Level (thr) | Hits/Floods | POD | Alerts | FA raw (lab+unlab) | FAR raw | FAR labelled | CSI raw | Lead median / min (h) | Alerts/year | Hours in alert |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for ev in evaluations:
        for level, c in ev.contingency.items():
            lines.append(
                f"| {ev.name} | {level} ({_fmt(c.threshold, digits=3)}) | {c.hits}/{c.hits + c.misses} | "
                f"{_fmt(c.pod, pct=True)} | {c.n_alerts_judged} | {c.false_alarms_raw} "
                f"({c.false_alarms_labelled}+{c.false_alarms_raw - c.false_alarms_labelled}) | "
                f"{_fmt(c.far_raw, pct=True)} | {_fmt(c.far_labelled, pct=True)} | {_fmt(c.csi_raw)} | "
                f"{_fmt(c.median_lead_h, digits=0)} / {_fmt(c.min_lead_h, digits=0)} | "
                f"{_fmt(c.alerts_per_year, digits=1)} | {_fmt(c.alert_hours_share, pct=True)} |"
            )
    return "\n".join(lines)


def hourly_table(evaluations: Sequence[ModelEvaluation]) -> str:
    lines = [
        "| Model | Hours (pos/neg) | ROC-AUC | Brier | Brier climatology | Brier kind |",
        "|---|---|---|---|---|---|",
    ]
    for ev in evaluations:
        h = ev.hourly
        lines.append(
            f"| {ev.name} | {h.n_pos}/{h.n_neg} | {_fmt(h.roc_auc, digits=3)} | "
            f"{_fmt(h.brier, digits=4)} | {_fmt(h.brier_reference, digits=4)} | {h.brier_kind} |"
        )
    return "\n".join(lines)


def events_markdown(ev: ModelEvaluation) -> str:
    levels = list(ev.thresholds)
    head = "| Event | Outcome | Score at peak | Max (window) | " + " | ".join(
        f"Lead {lv} (h)" for lv in levels
    ) + " | Pessac h (m) | n rising | Rain 72h (mm) | Legacy score |"
    lines = [head, "|" + "---|" * (8 + len(levels))]
    for e in ev.event_table:
        leads = " | ".join(_fmt(e.get(f"lead_{lv}_h"), digits=0) for lv in levels)
        lines.append(
            f"| {e['key']} | {e['outcome']} | {_fmt(e['score_at_peak'], digits=1)} | "
            f"{_fmt(e['max_score_window'], digits=1)} | {leads} | {_fmt(e.get('pessac_h'))} | "
            f"{_fmt(e.get('n_rising'), digits=0)} | {_fmt(e.get('rain_72h'), digits=0)} | "
            f"{_fmt(e['legacy_stored_score'], digits=1)} |"
        )
    return "\n".join(lines)


def alert_rows(evaluations: Sequence[ModelEvaluation]) -> list[dict]:
    rows = []
    for ev in evaluations:
        for level, c in ev.contingency.items():
            for a in c.alerts:
                rows.append({
                    "model": ev.name,
                    "level": level,
                    "threshold": c.threshold,
                    "start": a.alert.start.isoformat(),
                    "end": a.alert.end.isoformat(),
                    "duration_h": a.alert.n_hours,
                    "peak_time": a.alert.peak_time.isoformat(),
                    "peak_score": a.alert.peak_score,
                    "category": a.category,
                    "matched_events": ";".join(a.matched_keys),
                })
    return rows


def write_report(
    dataset: EvalDataset,
    evaluations: Sequence[ModelEvaluation],
    out_dir: Path = OUT_DIR,
    extra_sections: Sequence[str] = (),
    loeo: Optional[dict] = None,
    labelling: Optional[ModelEvaluation] = None,
) -> list[Path]:
    """Write metrics.json, alerts.csv, events.csv, unlabelled_alerts.csv, summary.md.

    ``labelling`` (e.g. v3.3 with alerts merged across short gaps) is the
    evaluation whose unlabelled alerts are listed in summary.md for the
    owner to label; unlabelled_alerts.csv always holds every model's.
    """
    paths = [
        write_json(out_dir / "metrics.json", {
            "dataset": dataset.meta,
            "models": [ev.to_dict() for ev in evaluations],
            "loeo": loeo,
        }),
        write_csv(out_dir / "alerts.csv", alert_rows(evaluations)),
        write_csv(out_dir / "events.csv", [
            {"model": ev.name, **row} for ev in evaluations for row in ev.event_table
        ]),
        write_csv(out_dir / "unlabelled_alerts.csv", [
            r for r in alert_rows(evaluations) if r["category"] == "unlabelled"
        ]),
    ]
    paths.append(out_dir / "summary.md")
    (out_dir / "summary.md").write_text(
        summary_markdown(dataset, evaluations, extra_sections, labelling), encoding="utf-8",
    )
    return paths


def summary_markdown(
    dataset: EvalDataset,
    evaluations: Sequence[ModelEvaluation],
    extra_sections: Sequence[str] = (),
    labelling: Optional[ModelEvaluation] = None,
) -> str:
    meta = dataset.meta
    cov = meta.get("coverage", {})
    v33 = next((e for e in evaluations if e.name.startswith("v3.3")), evaluations[0] if evaluations else None)
    parts = [
        "# Evaluation of the warning system (hourly replay)",
        "",
        f"- Period: {meta.get('start')} -> {meta.get('end')} ({meta.get('n_hours')} hours), "
        f"model v{meta.get('model_version')}",
        f"- Options: {json.dumps(meta.get('options'))}; causal (no future data): "
        f"{meta.get('leakage', {}).get('causal')}",
        f"- Coverage (share of hours): {json.dumps(cov)}",
        "- Unavailable inputs (excluded from the denominator, as in the backfill): "
        + ", ".join(meta.get("unavailable_inputs", [])),
        "- Ground truth: app/data/episodes_labels.json; flooded = positive, near_miss/false_positive = "
        "negative, unknown = excluded; alerts outside the annotated period are not judged.",
        "- Event match: alert start in [peak-72h, peak+6h] (or alert already on when the window opens).",
        "",
        "## Event-based contingency",
        "",
        metrics_table(evaluations),
        "",
        "FA raw counts every alert matching no flooded label (labelled negatives + unlabelled). "
        "**The labels are not exhaustive**: unlabelled alerts may be unannotated floods; FAR labelled "
        "only counts alerts on near_miss/false_positive labels.",
        "",
        "## Hourly 'flood within next 24h'",
        "",
        hourly_table(evaluations),
        "",
    ]
    if v33 is not None:
        parts += [f"## Labelled events ({v33.name})", "", events_markdown(v33), ""]
        source = labelling or v33
        level0 = next(iter(source.contingency), "")
        result = source.contingency[level0] if level0 else None
        unl = result.unlabelled_alerts if result else []
        merge = result.config.merge_gap_h if result else 0
        parts += [
            f"## Unlabelled {source.name} alerts ({level0}"
            + (f", runs merged across gaps <= {merge}h" if merge else "")
            + ") - to label",
            "",
            "Alerts in the annotated period that match no label (all models and levels, unmerged: "
            "unlabelled_alerts.csv).",
            "",
            "| Start | End | Hours | Peak score |",
            "|---|---|---|---|",
        ]
        parts += [
            f"| {a.alert.start:%Y-%m-%d %H:%M} | {a.alert.end:%Y-%m-%d %H:%M} | {a.alert.n_hours} | "
            f"{a.alert.peak_score:.1f} |"
            for a in unl
        ] or ["| none | | | |"]
        parts.append("")
    parts += list(extra_sections)
    return "\n".join(parts) + "\n"
