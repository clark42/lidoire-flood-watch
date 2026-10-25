"""Offline, reproducible evaluation of the flood warning system.

Run from ``backend/``::

    python -m scripts.eval build   [--start 2022-11-01 --end 2026-10-25]
    python -m scripts.eval replay  [...]
    python -m scripts.eval report  [--threshold orange=35 --threshold red=55]

Raw downloads are cached in ``backend/data/eval_cache/`` and outputs written
to ``backend/data/eval_out/`` (both gitignored). No InfluxDB needed.

Interface for candidate models (see :mod:`scripts.eval.models`)::

    from scripts.eval import Model, FeatureRow, EvalDataset, build_dataset, evaluate_model

    class MyModel:
        name = "my_model"
        kind = "score"                       # or "probability" (+ thresholds)
        thresholds = {"orange": 35, "red": 55}
        def reset(self): ...                 # optional, before each replay
        def score(self, row: FeatureRow) -> float | None: ...

    ds = build_dataset(offline=True)         # from cache
    ev = evaluate_model(MyModel(), ds)       # -> ModelEvaluation
    ev.summary()
"""

from scripts.eval.contingency import (
    AlertEpisode,
    ContingencyConfig,
    ContingencyResult,
    alert_episodes,
    evaluate_alerts,
    evaluate_scores,
)
from scripts.eval.dataset import (
    BuildOptions,
    EvalDataset,
    FeatureRow,
    RawInputs,
    build_dataset,
    build_rows,
)
from scripts.eval.hourly import HourlyTargetConfig, hourly_metrics, hourly_targets, roc_auc
from scripts.eval.loeo import (
    FitFn,
    LoeoResult,
    PessacThresholdModel,
    fit_pessac_threshold,
    leave_one_event_out,
    training_targets,
)
from scripts.eval.models import (
    Model,
    ModelEvaluation,
    PessacLevelModel,
    Rain72hModel,
    V33Model,
    default_models,
    evaluate_model,
    piecewise_score,
    replay,
)
from scripts.eval.truth import EvalEvent, LabelPeriod, binary_outcome, load_events

__all__ = [
    "AlertEpisode",
    "BuildOptions",
    "ContingencyConfig",
    "ContingencyResult",
    "EvalDataset",
    "EvalEvent",
    "FeatureRow",
    "FitFn",
    "HourlyTargetConfig",
    "LabelPeriod",
    "LoeoResult",
    "Model",
    "ModelEvaluation",
    "PessacLevelModel",
    "PessacThresholdModel",
    "Rain72hModel",
    "RawInputs",
    "V33Model",
    "alert_episodes",
    "binary_outcome",
    "build_dataset",
    "build_rows",
    "default_models",
    "evaluate_alerts",
    "evaluate_model",
    "evaluate_scores",
    "fit_pessac_threshold",
    "hourly_metrics",
    "hourly_targets",
    "leave_one_event_out",
    "load_events",
    "piecewise_score",
    "replay",
    "roc_auc",
    "training_targets",
]
