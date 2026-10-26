"""Model interface for the evaluation, the production model and simple baselines.

A candidate model is any object with:

- ``name: str``
- ``score(row: FeatureRow) -> Optional[float]``: risk score 0-100 (or a
  probability 0-1 when ``kind == "probability"``); None = cannot score this
  hour (treated as "no alert" and left out of the hourly metrics).

Optional attributes:

- ``kind``: ``"score"`` (default) or ``"probability"``;
- ``thresholds``: {level name: alert threshold on the model's own scale};
  default {"orange": 35, "red": 55} for scores. Probability models must
  declare theirs;
- ``reset()``: called once before each chronological replay. Rows are fed
  in time order, so a stateful model (e.g. a rainfall-runoff reservoir fed
  with ``row.values["rain_1h"]``) only ever sees the current and past rows.

``row.values`` (all causal, see dataset.py): ``h_<station_id>``,
``rate_<station_id>`` (cm/h), ``pessac_h``, ``pessac_rate_cm_h``,
``pessac_max_12h``, ``pessac_mean_12h`` (12.5h window), ``n_stations``,
``n_rising``, ``n_above_yellow``, ``n_above_orange``, ``rain_1h``, ``rain_3h``,
``rain_6h``, ``rain_12h``, ``rain_24h``, ``rain_48h``, ``rain_72h``,
``rain_168h`` (mm, basin mean), ``rain_hours_72h``, ``tide_available``,
``tide_coefficient``, ``hours_to_high_tide``, ``n_daily_fallback``, and the
v4-alpha inputs (publication lags applied, see dataset.py):
``q_<eyraud|caudeau|pessac>_m3s`` / ``_max_12h``, ``q_eyraud_lag0_*``
(sensitivity only), ``libourne_h``, ``libourne_max_12h``, ``z_conf_ngf``,
``piezo_ngf``, ``glofas_lidoire_m3s``.
``row.features`` is the production ``RiskFeatures`` (input of compute_risk).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol, Sequence, runtime_checkable

from app.config import get_settings
from app.services.risk_core import MODEL_VERSION, ModelParams, compute_risk, get_model_params
from scripts.eval.contingency import ContingencyConfig, ContingencyResult, evaluate_scores
from scripts.eval.dataset import EvalDataset, FeatureRow
from scripts.eval.hourly import HourlyResult, HourlyTargetConfig, hourly_metrics, hourly_targets
from scripts.eval.truth import EvalEvent, LabelPeriod, event_window

DEFAULT_THRESHOLDS: dict[str, float] = {"orange": 35.0, "red": 55.0}


@runtime_checkable
class Model(Protocol):
    """Minimal candidate model interface (see module docstring)."""

    name: str

    def score(self, row: FeatureRow) -> Optional[float]:
        ...


def model_kind(model: Model) -> str:
    return getattr(model, "kind", "score") or "score"


def model_thresholds(model: Model, override: Optional[Mapping[str, float]] = None) -> dict[str, float]:
    if override:
        return dict(override)
    th = getattr(model, "thresholds", None)
    if th:
        return dict(th)
    if model_kind(model) == "probability":
        raise ValueError(f"probability model {model.name!r} must declare thresholds")
    return dict(DEFAULT_THRESHOLDS)


def piecewise_score(value: Optional[float], thresholds: Sequence[float]) -> Optional[float]:
    """Monotone map (yellow, orange, red) -> (15, 35, 55), linear beyond red, capped 100."""
    if value is None:
        return None
    yellow, orange, red = thresholds
    if value <= 0:
        return 0.0
    if value < yellow:
        return 15.0 * value / yellow
    if value < orange:
        return 15.0 + 20.0 * (value - yellow) / (orange - yellow)
    if value < red:
        return 35.0 + 20.0 * (value - orange) / (red - orange)
    return min(100.0, 55.0 + 20.0 * (value - red) / (red - orange))


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

@dataclass
class V33Model:
    """Production score: risk_core.compute_risk on the production features.

    Hours without any recent station are not scored (as in the backfill).
    """

    params: Optional[ModelParams] = None
    name: str = f"v{MODEL_VERSION}"
    kind: str = "score"

    def score(self, row: FeatureRow) -> Optional[float]:
        if not row.features.stations:
            return None
        return compute_risk(row.features, self.params or get_model_params()).score


@dataclass
class PessacLevelModel:
    """Baseline (a): Pessac height only, against its own yellow/orange/red thresholds."""

    thresholds_m: Optional[tuple[float, float, float]] = None
    name: str = "pessac_level"
    kind: str = "score"

    def __post_init__(self):
        if self.thresholds_m is None:
            s = get_settings()
            cfg = next(c for c in s.hubeau_stations if c["id"] == s.hubeau_primary_station)
            th = cfg["thresholds"]
            self.thresholds_m = (th["yellow"], th["orange"], th["red"])

    def score(self, row: FeatureRow) -> Optional[float]:
        return piecewise_score(row.values.get("pessac_h"), self.thresholds_m)


@dataclass
class Rain72hModel:
    """Baseline (b): basin rain over the last 72h only (model thresholds 50/80/120 mm)."""

    thresholds_mm: Optional[tuple[float, float, float]] = None
    name: str = "rain_72h"
    kind: str = "score"

    def __post_init__(self):
        if self.thresholds_mm is None:
            self.thresholds_mm = tuple(get_model_params().rain_72h_thresholds)

    def score(self, row: FeatureRow) -> Optional[float]:
        return piecewise_score(row.values.get("rain_72h"), self.thresholds_mm)


def default_models() -> list[Model]:
    return [V33Model(), PessacLevelModel(), Rain72hModel()]


# ---------------------------------------------------------------------------
# Replay + evaluation
# ---------------------------------------------------------------------------

def replay(model: Model, dataset: EvalDataset) -> list[Optional[float]]:
    """Scores hour by hour in chronological order (reset() first if any)."""
    reset = getattr(model, "reset", None)
    if callable(reset):
        reset()
    out: list[Optional[float]] = []
    for row in dataset.rows:
        s = model.score(row)
        out.append(None if s is None else float(s))
    return out


@dataclass
class ModelEvaluation:
    name: str
    kind: str
    thresholds: dict[str, float]
    n_scored: int
    contingency: dict[str, ContingencyResult]
    hourly: HourlyResult
    event_table: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "thresholds": self.thresholds,
            "n_scored": self.n_scored,
            "contingency": {k: v.summary() for k, v in self.contingency.items()},
            "hourly": self.hourly.to_dict(),
        }

    def to_dict(self) -> dict:
        out = self.summary()
        out["contingency"] = {k: v.to_dict() for k, v in self.contingency.items()}
        out["event_table"] = self.event_table
        return out


def event_table(
    dataset: EvalDataset,
    scores: Sequence[Optional[float]],
    events: Sequence[EvalEvent],
    thresholds: Mapping[str, float],
    config: ContingencyConfig = ContingencyConfig(),
) -> list[dict]:
    """Per labelled event: score at peak, max in window, conditions at peak.

    Replaces the former validate_model.py / analyze_episodes.py printouts.
    """
    times = dataset.times
    rows = []
    for ev in events:
        w_start, w_end = event_window(ev, config.lead_window_h, config.lag_window_h)
        idx = [i for i, t in enumerate(times) if w_start <= t <= w_end]
        peak_i = dataset.hour_index(ev.peak_time)
        window_scores = [(times[i], scores[i]) for i in idx if scores[i] is not None]
        best = max(window_scores, key=lambda x: x[1]) if window_scores else (None, None)
        entry = {
            "key": ev.key,
            "outcome": ev.outcome.value,
            "positive": ev.positive,
            "severity": ev.severity,
            "score_at_peak": scores[peak_i] if peak_i is not None else None,
            "max_score_window": best[1],
            "max_score_time": best[0].isoformat() if best[0] else None,
            "legacy_stored_score": ev.legacy_stored_score,
        }
        for level, th in thresholds.items():
            first = next((t for t, s in window_scores if s >= th), None)
            entry[f"first_{level}"] = first.isoformat() if first else None
            entry[f"lead_{level}_h"] = (
                (ev.peak_time - first).total_seconds() / 3600 if first else None
            )
        if peak_i is not None:
            v = dataset.rows[peak_i].values
            for k in ("pessac_h", "pessac_rate_cm_h", "pessac_max_12h", "n_stations", "n_rising",
                      "n_above_orange", "rain_24h", "rain_72h", "rain_168h", "tide_coefficient"):
                entry[k] = v.get(k)
        rows.append(entry)
    return rows


def evaluate_model(
    model: Model,
    dataset: EvalDataset,
    events: Optional[Sequence[EvalEvent]] = None,
    period: Optional[LabelPeriod] = None,
    thresholds: Optional[Mapping[str, float]] = None,
    config: ContingencyConfig = ContingencyConfig(),
    target_config: HourlyTargetConfig = HourlyTargetConfig(),
    scores: Optional[Sequence[Optional[float]]] = None,
) -> ModelEvaluation:
    """Replay ``model`` on ``dataset`` and compute every metric.

    ``events``/``period`` default to the labels file (app.services.labels).
    ``scores`` can be passed to skip the replay (already computed).
    """
    if events is None:
        from scripts.eval.truth import load_events

        events, default_period = load_events()
        period = period or default_period
    th = model_thresholds(model, thresholds)
    kind = model_kind(model)
    if scores is None:
        scores = replay(model, dataset)
    times = dataset.times
    contingency = {
        level: evaluate_scores(times, scores, events, value, config, period)
        for level, value in th.items()
    }
    targets = hourly_targets(times, events, period, target_config)
    return ModelEvaluation(
        name=model.name,
        kind=kind,
        thresholds=th,
        n_scored=sum(1 for s in scores if s is not None),
        contingency=contingency,
        hourly=hourly_metrics(scores, targets, kind),
        event_table=event_table(dataset, scores, events, th, config),
    )
