"""v4-beta (pre-registered 2026-10-27): constants, regressors, branches, variants, climatology fit."""

import math
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from app.services import models_experimental as mx
from app.services import risk_v4 as v4
from scripts.eval import runoff_nowcast as rn
from scripts.eval import sources

PREREG = Path(rn.__file__).with_name("preregistration_v4beta.md")


def _values(**kw):
    base = {"q_eyraud_m3s": 2.0, "q_eyraud_lag10_m3s": 2.0, "q_eyraud_max_12h": 2.0,
            "era5_rain_6h": 0.0, "era5_rain_24h": 0.0, "era5_rain_72h": 0.0, "era5_rain_720h": 30.0,
            "pessac_max_12h": 3.0, "fc_rain_24h_prev": None}
    base.update(kw)
    return base


def test_v4b_preregistered_constants():
    assert mx.V4B_RUNOFF.as_tuple() == (0.0, 1.89, 4.72, 13.22)
    assert mx.V4B_RUNOFF_FRACTIONS == (0.10, 0.25, 0.70)
    assert v4.EYRAUD_Q2_M3S == 18.89 == rn.EYRAUD_Q2_M3S
    assert mx.V4B_OVERBANK.as_tuple() == (5.67, 6.67, 7.67, 8.67)
    # long profile: 3.1 km step crest 9.0 m NGF and mill ground 12.0 m NGF on the Pessac scale (zero 3.33)
    assert mx.V4B_STEP_CREST_M == pytest.approx(9.0 - v4.PESSAC_ZERO_NGF)
    assert mx.V4B_MILL_GROUND_M == pytest.approx(12.0 - v4.PESSAC_ZERO_NGF)
    assert tuple(mx.V4B_NOWCAST_COEF) == rn.NOWCAST_FEATURES
    assert tuple(mx.V4B_FORECAST_COEF) == rn.FORECAST_FEATURES
    assert tuple(mx.V4B_RAIN_ONLY_COEF) == rn.RAIN_ONLY_FEATURES


def test_v4b_coefficients_are_the_preregistered_ones():
    text = PREREG.read_text(encoding="utf-8")
    table = dict(re.findall(r"^\| (intercept|q7\*?r?\w*|d7|r6|r24|r72|w30) \| (-?\d+\.\d+) \|$", text, re.M))
    assert float(table["intercept"]) == mx.V4B_NOWCAST_INTERCEPT
    for k, c in mx.V4B_NOWCAST_COEF.items():
        assert float(table[k.replace("_", "*")]) == c, k
    for k, c in mx.V4B_FORECAST_COEF.items():
        assert f"{k.replace('_', '*')} {c:.6f}" in text, k
    for k, c in mx.V4B_RAIN_ONLY_COEF.items():
        assert f"{k} {c:.6f}" in text, k


def test_v4b_regressors_match_the_fit_transforms():
    rng = np.random.default_rng(1)
    for _ in range(50):
        q7, q10 = rng.uniform(0, 30, 2)
        r6 = rng.uniform(0, 30)
        r24 = r6 + rng.uniform(0, 40)
        r72 = r24 + rng.uniform(0, 60)
        r720 = r72 + rng.uniform(0, 200)
        f24 = rng.uniform(0, 50)
        x = mx.v4b_regressors({"q_eyraud_m3s": q7, "q_eyraud_lag10_m3s": q10, "era5_rain_6h": r6,
                               "era5_rain_24h": r24, "era5_rain_72h": r72, "era5_rain_720h": r720}, f24)
        ref = rn.transformed_features(q7, q10, r6, r24, r72, r720, f24)
        for k in rn.FORECAST_FEATURES:
            assert x[k] == pytest.approx(float(ref[k]), rel=1e-12, abs=1e-12), k
    # the vectorised fit predictor and the scalar model agree
    feats = rn.transformed_features(np.array([3.0]), np.array([2.0]), np.array([10.0]), np.array([25.0]),
                                    np.array([40.0]), np.array([120.0]))
    fit = {"features": rn.NOWCAST_FEATURES, "intercept": mx.V4B_NOWCAST_INTERCEPT,
           "coef": list(mx.V4B_NOWCAST_COEF.values())}
    q, src = mx.v4b_nowcast_q(mx.v4b_regressors({"q_eyraud_m3s": 3.0, "q_eyraud_lag10_m3s": 2.0,
                                                 "era5_rain_6h": 10.0, "era5_rain_24h": 25.0,
                                                 "era5_rain_72h": 40.0, "era5_rain_720h": 120.0}))
    assert src == "eyraud" and q == pytest.approx(float(rn.predict(fit, feats)[0]))


def test_v4b_dry_baseflow_is_green_and_rain_raises_the_runoff():
    m = mx.V4BetaModel()
    dry = m.breakdown(_values(q_eyraud_m3s=1.0, q_eyraud_lag10_m3s=1.0, q_eyraud_max_12h=1.0))
    assert dry["nowcast_source"] == "eyraud" and dry["score"] < 15
    assert dry["q_nowcast"] == pytest.approx(1.0, rel=0.15)  # persistence on a dry day
    wet = m.breakdown(_values(era5_rain_6h=25.0, era5_rain_24h=45.0, era5_rain_72h=60.0,
                              q_eyraud_m3s=4.0, q_eyraud_lag10_m3s=3.0, q_eyraud_max_12h=4.0))
    assert wet["q_nowcast"] > 3 * 4.0 and wet["score"] > 35
    assert wet["score"] == pytest.approx(wet["runoff"], abs=0.01) and wet["overbank"] == 0.0


def test_v4b_observed_eyraud_holds_the_score():
    m = mx.V4BetaModel()
    b = m.breakdown(_values(q_eyraud_max_12h=20.0))  # recession: published max still high
    assert b["q_runoff"] == 20.0 and b["score"] >= 55


def test_v4b_rain_only_fallback_and_missing_inputs():
    m = mx.V4BetaModel()
    b = m.breakdown(_values(q_eyraud_m3s=None, q_eyraud_lag10_m3s=None, q_eyraud_max_12h=None))
    assert b["nowcast_source"] == "rain_only" and b["score"] is not None
    b = m.breakdown(_values(q_eyraud_m3s=None, q_eyraud_lag10_m3s=None, q_eyraud_max_12h=None,
                            era5_rain_720h=None))
    assert b["nowcast_source"] == "none" and b["runoff"] is None
    assert b["score"] == b["overbank"] == 0.0  # Pessac below the step crest
    b = m.breakdown({})
    assert b["score"] is None


def test_v4b_pessac_only_above_the_step_crest():
    m = mx.V4BetaModel()
    assert m.breakdown(_values(pessac_max_12h=5.6))["overbank"] == 0.0
    assert m.breakdown(_values(pessac_max_12h=6.67))["overbank"] == pytest.approx(15.0)
    assert m.breakdown(_values(pessac_max_12h=7.67))["score"] == pytest.approx(35.0)
    assert m.breakdown(_values(pessac_max_12h=8.67))["score"] == pytest.approx(55.0)
    # V3 ignores Pessac entirely
    assert mx.V4BetaModel("runoff").breakdown(_values(pessac_max_12h=8.67))["overbank"] is None


def test_v4b_modulator_variant():
    base = _values(q_eyraud_max_12h=4.0, q_eyraud_m3s=4.0, q_eyraud_lag10_m3s=4.0)
    low = mx.V4BetaModel("mod").breakdown({**base, "pessac_max_12h": 5.0})
    assert low["modulator_g"] == 0.0 and low["score"] == mx.V4BetaModel().breakdown({**base, "pessac_max_12h": 5.0})["score"]
    mid = mx.V4BetaModel("mod").breakdown({**base, "pessac_max_12h": 7.17})
    assert mid["modulator_g"] == pytest.approx(0.5)
    assert mid["q_runoff"] == pytest.approx(2 * max(4.0, mid["q_nowcast"]))
    top = mx.V4BetaModel("mod").breakdown({**base, "pessac_max_12h": 9.5})
    assert top["modulator_g"] == mx.V4B_MOD_MAX_G


def test_v4b_forecast_variant_needs_the_archived_forecast():
    fc = mx.V4BetaModel("fc")
    none = fc.breakdown(_values())
    assert none["forecast"] is None and none["score"] == mx.V4BetaModel().breakdown(_values())["score"]
    wet = fc.breakdown(_values(fc_rain_24h_prev=60.0, q_eyraud_m3s=4.0, q_eyraud_lag10_m3s=4.0))
    assert wet["forecast"] is not None and wet["score"] >= wet["runoff"]
    assert wet["q_forecast"] > wet["q_nowcast"]
    # the primary never reads the forecast
    assert mx.V4BetaModel().breakdown(_values(fc_rain_24h_prev=60.0))["forecast"] is None


def test_v4b_report_end_to_end(tmp_path):
    from datetime import timedelta

    from app.models.schemas import EpisodeOutcome
    from app.services.risk_core import RiskFeatures
    from scripts.eval import FeatureRow
    from scripts.eval import v4beta
    from scripts.eval.dataset import EvalDataset
    from scripts.eval.truth import EvalEvent, LabelPeriod, binary_outcome

    t0, h, n = datetime(2024, 1, 1), timedelta(hours=1), 24 * 120
    events = []
    for day, outcome in ((30, EpisodeOutcome.FLOODED), (60, EpisodeOutcome.NEAR_MISS),
                         (90, EpisodeOutcome.FLOODED)):
        t = t0 + timedelta(days=day, hours=12)
        events.append(EvalEvent(t.date().isoformat(), t, outcome, binary_outcome(outcome), 3))
    q = np.full(n, 1.0)
    for e in events:
        i = int((e.peak_time - t0) / h)
        q[i - 12:i + 6] = 25.0 if e.positive else 6.0
    rows = []
    for k in range(n):
        t = t0 + k * h
        lag7, lag10 = q[max(0, k - 7)], q[max(0, k - 10)]
        values = {"q_eyraud_m3s": lag7, "q_eyraud_lag10_m3s": lag10, "q_eyraud_max_12h": lag7,
                  "q_eyraud_lag0_m3s": q[k], "q_eyraud_lag0_max_12h": q[k], "q_eyraud_lag3_m3s": q[max(0, k - 3)],
                  "era5_rain_6h": 0.0, "era5_rain_24h": 0.0, "era5_rain_72h": 0.0, "era5_rain_720h": 50.0,
                  "fc_rain_24h_prev": 0.0, "pessac_max_12h": 3.0, "pessac_h": 3.0, "rain_1h": 0.0,
                  "rain_24h": 0.0, "rain_72h": 0.0}
        rows.append(FeatureRow(t, RiskFeatures(timestamp=t, stations=(), primary_station_id="P"), values))
    ds = EvalDataset(rows, (), "P", {"start": t0.isoformat(), "end": (t0 + (n - 1) * h).isoformat()})
    payload = v4beta.run(ds, tmp_path, events, LabelPeriod(t0, t0 + (n - 1) * h))
    assert {d["candidate"].split(" ")[0] for d in payload["decisions"]} == set(v4beta.ELIGIBLE)
    primary = next(r for r in payload["lead_rows"] if r["label"] == "v4b >= 35 (orange)")
    assert primary["hits"] == 2 and primary["fa_labelled"] == 1  # the near miss reaches 6 m3/s
    assert (tmp_path / "summary.md").exists() and (tmp_path / "scores.csv").exists()


def test_v4b_unknown_variant():
    with pytest.raises(ValueError):
        mx.V4BetaModel("oracle")


def test_v4b_climatology_fit_matches_constants_when_cached():
    key = f"{rn.EYRAUD_ID}_Q_2010-07-01_2010-09-30"
    if not sources.RawCache().path("hydroportail", key).exists():
        pytest.skip("training cache not present (run python -m scripts.eval.runoff_nowcast)")
    res = rn.fit_climatology()
    for name, intercept, coef in (("nowcast", mx.V4B_NOWCAST_INTERCEPT, mx.V4B_NOWCAST_COEF),
                                  ("forecast", mx.V4B_FORECAST_INTERCEPT, mx.V4B_FORECAST_COEF),
                                  ("rain_only", mx.V4B_RAIN_ONLY_INTERCEPT, mx.V4B_RAIN_ONLY_COEF)):
        fit = res[name]
        assert fit["intercept"] == pytest.approx(intercept, abs=1e-6)
        assert fit["coef"] == pytest.approx(list(coef.values()), abs=1e-6)
    ops = rn.operating_points(res)
    anchors = ops["anchors_fraction_of_q2"]
    assert (anchors["yellow"], anchors["orange"], anchors["red"]) == mx.V4B_RUNOFF_FRACTIONS
    # training hours only: nothing on or after the evaluation start
    grid, *_, in_train, q_points = res["_arrays"]
    assert grid[in_train].max() < np.datetime64(datetime(2022, 9, 1), "s")
    assert max(t for t, _ in q_points) < datetime(2022, 10, 1)
    assert math.isfinite(res["nowcast"]["r2"]) and res["nowcast"]["r2"] > 0.8
