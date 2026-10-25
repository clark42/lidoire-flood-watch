"""Candidate comparison harness: CV blocks, no held-out leakage, fits, uncertainty, end-to-end run."""

from datetime import datetime, timedelta

import numpy as np
import pytest

from app.models.schemas import EpisodeOutcome
from app.services.risk_core import RiskFeatures
from scripts.eval import FeatureRow
from scripts.eval import candidates as C
from scripts.eval.contingency import ContingencyConfig, evaluate_scores
from scripts.eval.dataset import EvalDataset
from scripts.eval.truth import EvalEvent, LabelPeriod, binary_outcome

T0 = datetime(2024, 1, 1)
H = timedelta(hours=1)
N = 24 * 120


def _event(day, outcome, hour=12):
    t = T0 + timedelta(days=day, hours=hour)
    return EvalEvent(t.date().isoformat(), t, outcome, binary_outcome(outcome), 3)


FLOOD_A = _event(20, EpisodeOutcome.FLOODED)
FLOOD_B = _event(30, EpisodeOutcome.FLOODED)  # 10 days after A: same CV block
NEAR = _event(60, EpisodeOutcome.NEAR_MISS)
FLOOD_C = _event(90, EpisodeOutcome.FLOODED)
UNKNOWN = _event(105, EpisodeOutcome.UNKNOWN)
EVENTS = [FLOOD_A, FLOOD_B, NEAR, FLOOD_C, UNKNOWN]
PERIOD = LabelPeriod(T0, T0 + (N - 1) * H)


def _dataset() -> EvalDataset:
    """Floods: backwater (H 4.6 m) + 8 mm/h of rain during 6h, 30h before the peak.
    Near miss: same backwater, no rain. A lone rain burst on day 45 at low backwater."""
    rain = np.zeros(N)
    h = np.full(N, 2.0)
    for ev in (FLOOD_A, FLOOD_B, FLOOD_C):
        i = int((ev.peak_time - T0) / H)
        rain[i - 30:i - 24] = 8.0
        h[i - 36:i + 12] = 4.6
    i = int((NEAR.peak_time - T0) / H)
    h[i - 36:i + 12] = 4.6
    rain[45 * 24:45 * 24 + 6] = 8.0
    rows = []
    for k in range(N):
        t = T0 + k * H
        values = {
            "rain_1h": rain[k], "rain_24h": float(rain[max(0, k - 23):k + 1].sum()),
            "rain_72h": float(rain[max(0, k - 71):k + 1].sum()),
            "pessac_max_12h": h[k], "pessac_h": h[k],
        }
        rows.append(FeatureRow(t, RiskFeatures(timestamp=t, stations=(), primary_station_id="P"), values))
    return EvalDataset(rows, (), "P", {"start": T0.isoformat(), "end": (T0 + (N - 1) * H).isoformat()})


def test_models_event_blocks_cover_and_group():
    ds = _dataset()
    blocks = C.event_blocks(ds, EVENTS)
    assert [b.key for b in blocks] == [f"{FLOOD_A.key}+{FLOOD_B.key}", NEAR.key, FLOOD_C.key]
    assert blocks[0].start == 0 and blocks[-1].end == N
    assert all(a.end == b.start for a, b in zip(blocks, blocks[1:]))
    assert UNKNOWN.key in blocks[-1].keys  # unknown events ride along, never a block of their own
    for b in blocks:
        for e in b.events:
            assert ds.times[b.start] <= e.peak_time <= ds.times[b.end - 1]


def test_models_blocked_cv_never_sees_held_out_block():
    ds = _dataset()
    blocks = C.event_blocks(ds, EVENTS)
    calls = []

    class FoldModel:
        name = "fold"
        kind = "score"

        def __init__(self, fold):
            self.fold = fold

        def score(self, row):
            return float(self.fold)

    def fit(dataset, train_events, train_mask=None, period=None):
        calls.append(({e.key for e in train_events}, train_mask.copy()))
        return FoldModel(len(calls) - 1)

    res = C.blocked_cv(fit, ds, EVENTS, blocks, PERIOD, "spy")
    assert len(calls) == len(blocks)
    for k, (b, (keys, mask)) in enumerate(zip(blocks, calls)):
        assert not (keys & b.keys)  # no label of the held-out block
        assert not mask[b.start:b.end].any() and mask.sum() == N - (b.end - b.start)
        assert set(res.scores[b.start:b.end]) == {float(k)}  # block scored by its own fold model


def test_models_training_frame_csi():
    ds = _dataset()
    frame = C.TrainingFrame.build(ds, EVENTS, PERIOD, None)
    alert = np.zeros(N, dtype=bool)
    for ev in (FLOOD_A, FLOOD_C, NEAR):
        i = ds.index_of(ev.peak_time)
        alert[i - 20:i] = True
    i_unk = ds.index_of(UNKNOWN.peak_time)
    alert[i_unk - 5:i_unk] = True  # matches only the unknown event: not a false alarm
    csi, hits, fa, hours = frame.csi(alert)
    assert (hits, fa) == (2, 1) and csi == pytest.approx(2 / (3 + 1)) and hours == 65
    # same answer as the harness contingency
    scores = [100.0 if a else 0.0 for a in alert]
    ref = evaluate_scores(ds.times, scores, EVENTS, 50.0, ContingencyConfig(), PERIOD)
    assert ref.hits == hits and ref.false_alarms_raw == fa
    masked = C.TrainingFrame.build(ds, EVENTS, PERIOD, C.mask_excluding(N, 0, ds.index_of(FLOOD_B.peak_time) + 7))
    assert masked.csi(alert)[1:3] == (1, 1)


def test_models_fit_backwater_rule_finds_the_joint_rule():
    ds = _dataset()
    m = C.fit_backwater_rule(ds, EVENTS, forcing="rain_24h", period=PERIOD)
    info = m.fit_info
    assert info["train_hits"] == 3 and info["train_fa"] == 0
    assert 2.0 < info["h_star"] <= 4.6 and 0 < info["f_star"] <= 48.0
    ev = C.evaluate_model(m, ds, EVENTS, PERIOD)
    assert ev.contingency["orange"].hits == 3
    assert ev.contingency["orange"].false_alarms_raw == 0  # neither the near miss nor the lone burst


def test_models_fit_logistic_uses_training_targets_only():
    ds = _dataset()
    m = C.fit_logistic(ds, [FLOOD_A, NEAR], ("rain_24h", "h_aval"), period=PERIOD)
    assert m.fit_info["n_pos"] == 25 and m.fit_info["converged"]
    assert m.fit_info["coef_std"][0] > 0 and m.fit_info["coef_std"][1] > 0
    mask = C.mask_excluding(N, 0, N // 2)
    m2 = C.fit_logistic(ds, EVENTS, ("rain_24h", "h_aval"), train_mask=mask, period=PERIOD)
    assert m2.fit_info["n_rows"] < N // 2 and m2.fit_info["n_pos"] == 25  # only FLOOD_C left


def test_models_mask_around_missing_events():
    ds = _dataset()
    m = C.mask_around_missing_events(ds, [FLOOD_A, FLOOD_C, NEAR, UNKNOWN], EVENTS, days=2)
    i = ds.index_of(FLOOD_B.peak_time)
    assert not m[i - 48:i + 49].any() and m[i - 49] and m[i + 49]
    assert m.sum() == N - 97


def test_models_label_free_thresholds():
    # two complete hydrological years (Sep 2022 -> Aug 2023, Sep 2023 -> Aug 2024)
    q = np.ones(8760 + 8784)
    q[100], q[8760 + 100] = 10.0, 30.0
    times = [datetime(2022, 9, 1) + k * H for k in range(len(q))]
    maxima = C.hydro_year_maxima(times, q)
    assert sorted(maxima) == [10.0, 30.0]
    assert C.label_free_q_thresholds(times, q) == pytest.approx((0.4 * 20, 0.7 * 20, 20.0))
    y, o, r = C.label_free_quantile_thresholds(np.arange(1001.0))
    assert (y, o, r) == pytest.approx((900.0, 970.0, 995.0))


def test_models_clopper_pearson_and_winter_counts():
    lo, hi = C.clopper_pearson(5, 5)
    assert lo == pytest.approx(0.4782, abs=1e-3) and hi == 1.0
    lo, hi = C.clopper_pearson(4, 5)
    assert lo == pytest.approx(0.2836, abs=1e-3) and hi == pytest.approx(0.9949, abs=1e-3)
    assert C.clopper_pearson(0, 5)[1] == pytest.approx(0.5218, abs=1e-3)
    times = [datetime(2022, 10, 1) + k * H for k in range(24 * 365 * 3)]
    scores = [100.0 if t.day == 1 and t.hour == 0 else 0.0 for t in times]  # one alert per month
    res = evaluate_scores(times, scores, [], 50.0)
    assert C.alerts_per_winter(res, winters=(2022, 2023)) == 5.0


def test_models_block_bootstrap_auc():
    ds = _dataset()
    blocks = C.event_blocks(ds, EVENTS)
    targets = C.hourly_targets(ds.times, EVENTS, PERIOD)
    perfect = [float(t) if t is not None else 0.0 for t in targets]
    lo, hi = C.block_bootstrap_auc(perfect, targets, blocks, n_boot=50)
    assert lo == pytest.approx(1.0) and hi == pytest.approx(1.0)


def test_models_run_end_to_end(tmp_path):
    ds = _dataset()
    payload = C.run(ds, tmp_path, EVENTS, PERIOD, crosscheck=True, sensitivity=False)
    names = {r["model"] for r in payload["rows"]}
    assert {"v3.3", "pessac_level", "v3.3+backwater", "bw_rule_api_cv", "lr_api_haval_cv",
            "bw_rule_gr4h_apriori", "lr_gr4h_haval_cv"} <= names
    cv_rows = [r for r in payload["rows"] if r["model"] == "bw_rule_rain24_cv" and r["level"] == "orange"]
    assert cv_rows and cv_rows[0]["n_floods"] == 3
    assert set(payload["loeo_crosscheck"]) == {"bw_rule_api_cv", "bw_rule_gr4h_cv", "lr_api_haval_cv",
                                               "lr_gr4h_haval_cv"}
    text = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "blocked LOEO" in text and "| Event | Outcome |" in text
    assert (tmp_path / "metrics.json").exists() and (tmp_path / "oos_scores.csv").exists()
