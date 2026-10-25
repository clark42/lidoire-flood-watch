"""Model protocol, baselines, evaluate_model, hourly metrics and LOEO helper."""

from datetime import datetime, timedelta

import pytest

from app.models.schemas import EpisodeOutcome
from app.services.risk_core import RiskFeatures
from scripts.eval import (
    EvalDataset,
    FeatureRow,
    Model,
    PessacLevelModel,
    Rain72hModel,
    V33Model,
    evaluate_model,
    hourly_targets,
    leave_one_event_out,
    piecewise_score,
    replay,
    roc_auc,
    training_targets,
)
from scripts.eval.hourly import HourlyTargetConfig, brier, hourly_metrics
from scripts.eval.truth import EvalEvent, LabelPeriod, binary_outcome

T0 = datetime(2024, 1, 1)
H = timedelta(hours=1)
N = 24 * 30


def _event(t, outcome):
    return EvalEvent(t.date().isoformat(), t, outcome, binary_outcome(outcome), 3)


FLOOD_A = _event(T0 + 5 * 24 * H, EpisodeOutcome.FLOODED)
FLOOD_B = _event(T0 + 15 * 24 * H, EpisodeOutcome.FLOODED)
NEAR = _event(T0 + 25 * 24 * H, EpisodeOutcome.NEAR_MISS)
EVENTS = [FLOOD_A, FLOOD_B, NEAR]
PERIOD = LabelPeriod(T0, T0 + (N - 1) * H)


def _dataset() -> EvalDataset:
    """x rises to 80 during the 30h before each flood and to 50 before the near miss."""
    rows = []
    for i in range(N):
        t = T0 + i * H
        x = 5.0
        for ev, top in ((FLOOD_A, 80.0), (FLOOD_B, 80.0), (NEAR, 50.0)):
            if ev.peak_time - 30 * H <= t <= ev.peak_time:
                x = top
        values = {"x": x, "pessac_h": x / 20, "rain_72h": x, "rain_1h": 1.0}
        rows.append(FeatureRow(t, RiskFeatures(timestamp=t, stations=(), primary_station_id="P"), values))
    return EvalDataset(rows, (), "P", {})


class XModel:
    name = "x"

    def __init__(self, threshold_shift=0.0):
        self.shift = threshold_shift
        self.seen = []
        self.resets = 0

    def reset(self):
        self.resets += 1
        self.seen = []

    def score(self, row):
        self.seen.append(row.timestamp)
        return row.values["x"] - self.shift


class ProbModel:
    name = "prob"
    kind = "probability"
    thresholds = {"high": 0.5}

    def score(self, row):
        return row.values["x"] / 100


def test_evaluation_protocol_and_replay_order():
    m = XModel()
    assert isinstance(m, Model)
    assert isinstance(V33Model(), Model) and isinstance(PessacLevelModel(), Model)
    ds = _dataset()
    scores = replay(m, ds)
    assert m.resets == 1
    assert m.seen == ds.times  # chronological, one call per row
    assert len(scores) == N


def test_evaluation_evaluate_model_metrics():
    ev = evaluate_model(XModel(), _dataset(), EVENTS, PERIOD)
    orange, red = ev.contingency["orange"], ev.contingency["red"]
    assert orange.hits == 2 and orange.misses == 0
    assert orange.false_alarms_labelled == 1 and orange.false_alarms_raw == 1  # near miss at 50
    assert red.false_alarms_raw == 0 and red.pod == 1.0
    assert orange.lead_times_h == [30.0, 30.0]
    assert ev.hourly.n_pos == 2 * 25
    assert ev.hourly.roc_auc > 0.9
    table = {r["key"]: r for r in ev.event_table}
    assert table[FLOOD_A.key]["max_score_window"] == 80.0
    assert table[FLOOD_A.key]["lead_orange_h"] == 30.0
    assert table[NEAR.key]["lead_red_h"] is None
    s = ev.summary()
    assert s["contingency"]["orange"]["pod"] == 1.0 and s["hourly"]["n_pos"] == 50


def test_evaluation_threshold_override_and_probability_models():
    ev = evaluate_model(XModel(), _dataset(), EVENTS, PERIOD, thresholds={"t60": 60})
    assert list(ev.contingency) == ["t60"]
    assert ev.contingency["t60"].false_alarms_raw == 0
    pev = evaluate_model(ProbModel(), _dataset(), EVENTS, PERIOD)
    assert pev.kind == "probability" and pev.hourly.brier_kind == "probability"
    assert pev.contingency["high"].hits == 2

    class BadProb:
        name = "bad"
        kind = "probability"

        def score(self, row):
            return 0.1

    with pytest.raises(ValueError):
        evaluate_model(BadProb(), _dataset(), EVENTS, PERIOD)


def test_evaluation_piecewise_score_and_baselines():
    th = (3.0, 3.8, 4.5)
    assert piecewise_score(None, th) is None
    assert piecewise_score(0, th) == 0.0
    assert piecewise_score(3.0, th) == pytest.approx(15)
    assert piecewise_score(3.8, th) == pytest.approx(35)
    assert piecewise_score(4.5, th) == pytest.approx(55)
    assert piecewise_score(20, th) == 100.0
    row = _dataset().rows[0]
    assert PessacLevelModel(thresholds_m=(0.1, 0.2, 0.3)).score(row) == pytest.approx(
        piecewise_score(0.25, (0.1, 0.2, 0.3)))
    assert Rain72hModel().thresholds_mm == (50.0, 80.0, 120.0)
    assert PessacLevelModel().thresholds_m == (3.0, 3.8, 4.5)


def test_evaluation_v33_model_skips_hours_without_station():
    assert V33Model().score(_dataset().rows[0]) is None


def test_evaluation_roc_auc_and_brier():
    assert roc_auc([1, 2, 3, 4], [0, 0, 1, 1]) == 1.0
    assert roc_auc([4, 3, 2, 1], [0, 0, 1, 1]) == 0.0
    assert roc_auc([1, 1, 1, 1], [0, 1, 0, 1]) == 0.5
    assert roc_auc([1, 2, 2, 3], [0, 0, 1, 1]) == pytest.approx(0.875)
    assert roc_auc([1, 2], [1, 1]) is None
    assert brier([0.0, 1.0], [0, 1]) == 0.0
    assert brier([0.5, 0.5], [0, 1]) == 0.25
    res = hourly_metrics([10, None, 90, 50], [0, 1, 1, None])
    assert res.n_hours == 2 and res.n_pos == 1 and res.roc_auc == 1.0
    assert res.brier == pytest.approx(((0.1) ** 2 + (0.1) ** 2) / 2)


def test_evaluation_hourly_targets_exclusions():
    unknown = _event(T0 + 10 * 24 * H, EpisodeOutcome.UNKNOWN)
    times = [T0 + i * H for i in range(N)]
    y = dict(zip(times, hourly_targets(times, [FLOOD_A, unknown], PERIOD)))
    p = FLOOD_A.peak_time
    assert y[p - 25 * H] == 0 and y[p - 24 * H] == 1 and y[p] == 1
    assert y[p + H] is None and y[p + 24 * H] is None and y[p + 25 * H] == 0
    u = unknown.peak_time
    assert y[u - 72 * H] is None and y[u + 24 * H] is None and y[u - 73 * H] == 0
    outside = hourly_targets([T0 - H], [FLOOD_A], PERIOD)
    assert outside == [None]
    y6 = hourly_targets(times, [FLOOD_A], PERIOD, HourlyTargetConfig(horizon_h=6))
    assert sum(v for v in y6 if v) == 7


def test_evaluation_leave_one_event_out_never_sees_held_out_label():
    ds = _dataset()
    calls = []

    def fit(dataset, train_events):
        keys = {e.key for e in train_events}
        calls.append(keys)
        # "fit": shift the threshold down when the near miss is not in training
        return XModel(threshold_shift=0.0 if NEAR.key in keys else -10.0)

    res = leave_one_event_out(fit, ds, EVENTS)
    assert len(res.folds) == 3
    for fold, keys in zip(res.folds, calls):
        assert fold.held_out not in keys
        assert len(keys) == 2
    by = {f.held_out: f for f in res.folds}
    assert by[FLOOD_A.key].matched == {"orange": True, "red": True}
    assert by[FLOOD_A.key].lead_time_h["orange"] == 30.0
    assert by[NEAR.key].matched["orange"] is True  # 50 + 10 >= 35
    assert by[NEAR.key].matched["red"] is True  # 60 >= 55 once the near miss is held out
    s = res.summary()
    assert s["levels"]["orange"]["pod"] == 1.0
    assert s["levels"]["red"]["labelled_negatives_alerted"] == 1


def test_evaluation_training_targets_pairs():
    pairs = training_targets(_dataset(), [FLOOD_A], PERIOD)
    assert all(y in (0, 1) for _, y in pairs)
    assert sum(y for _, y in pairs) == 25
    assert all(isinstance(r, FeatureRow) for r, _ in pairs)
