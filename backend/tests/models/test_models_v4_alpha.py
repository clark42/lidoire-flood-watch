"""v4-alpha: pure mechanism score (risk_v4), eval wrapper equivalence, pre-registered thresholds."""

import math
from dataclasses import replace
from datetime import datetime, timedelta

import numpy as np
import pytest

from app.models.schemas import RiskLevel
from app.services import models_experimental as mx
from app.services import risk_v4 as v4
from app.services.risk_core import RiskFeatures
from scripts.eval import FeatureRow, replay

T0 = datetime(2024, 1, 1)
H = timedelta(hours=1)
TH = v4.GradedThresholds(0.0, 3.0, 3.8, 4.5)


def test_v4_graded_score_anchors_and_cap():
    assert v4.graded_score(None, TH) is None
    assert v4.graded_score(float("nan"), TH) is None
    assert v4.graded_score(-1.0, TH) == 0.0
    assert v4.graded_score(1.5, TH) == pytest.approx(7.5)
    assert v4.graded_score(3.0, TH) == pytest.approx(15.0)
    assert v4.graded_score(3.8, TH) == pytest.approx(35.0)
    assert v4.graded_score(4.5, TH) == pytest.approx(55.0)
    assert v4.graded_score(30.0, TH) == 100.0
    anchored = v4.GradedThresholds(6.3, 6.8, 7.0, 7.2)
    assert v4.graded_score(6.0, anchored) == 0.0
    assert v4.graded_score(6.55, anchored) == pytest.approx(7.5)
    xs = np.linspace(-1, 8, 200)
    ys = [v4.graded_score(float(x), TH) for x in xs]
    assert all(b >= a for a, b in zip(ys, ys[1:]))
    with pytest.raises(ValueError):
        v4.GradedThresholds(0.0, 3.0, 3.0, 4.0)


def test_v4_confluence_stage_datums():
    assert v4.confluence_stage(None, 4.0) is None
    assert v4.confluence_stage(4.0, None) is None
    z = v4.confluence_stage(4.0, 4.5)
    assert z == pytest.approx(0.8 * (4.0 + 3.33) + 0.2 * (4.5 - 1.17))


def test_v4_preregistered_defaults():
    p = v4.default_v4_params()
    assert p.model_version == v4.MODEL_VERSION_V4 == "4.0a"
    assert p.backwater.as_tuple() == (0.0, 3.0, 3.8, 4.5)
    assert p.runoff.as_tuple() == pytest.approx((0.0, 0.4 * v4.EYRAUD_Q2_M3S, 0.7 * v4.EYRAUD_Q2_M3S,
                                                 v4.EYRAUD_Q2_M3S))
    assert p.overbank.as_tuple()[1:] == v4.PESSAC_T_YEAR_LEVELS_M
    assert p.groundwater.as_tuple() == v4.PIEZO_PERCENTILES_NGF
    assert p.use_overbank and not p.use_groundwater and p.backwater_source == "pessac"
    for value in (v4.EYRAUD_Q2_M3S, v4.CAUDEAU_Q2_M3S, v4.GLOFAS_Q2_M3S, *v4.PESSAC_T_YEAR_LEVELS_M,
                  *v4.PIEZO_PERCENTILES_NGF, *v4.CONFLUENCE_THRESHOLDS_NGF):
        assert math.isfinite(value)
    with pytest.raises(ValueError):
        v4.V4Params(backwater_source="libourne")


def test_v4_joint_needs_backwater_and_runoff():
    p = v4.default_v4_params()
    q2 = v4.EYRAUD_Q2_M3S
    # high Dordogne, dry Lidoire (2024-05-09 pattern): no joint alert
    dry = v4.compute_risk_v4(v4.V4Features(backwater_level_m=4.6, runoff_q_m3s=0.3), p)
    assert dry.joint_score < 15 and dry.score < 35
    # Lidoire flood on a low Dordogne: no alert either
    low = v4.compute_risk_v4(v4.V4Features(backwater_level_m=1.5, runoff_q_m3s=1.2 * q2), p)
    assert low.score < 15 and low.dominant == "joint"
    # both: orange or above, joint dominates
    both = v4.compute_risk_v4(v4.V4Features(backwater_level_m=4.0, runoff_q_m3s=0.8 * q2), p)
    assert both.level in (RiskLevel.ORANGE, RiskLevel.RED) and both.dominant == "joint"
    assert both.joint_score == pytest.approx(min(v4.graded_score(4.0, p.backwater),
                                                 v4.graded_score(0.8 * q2, p.runoff)), abs=0.01)
    assert set(both.mechanisms) == set(v4.MECHANISMS)
    assert both.mechanisms["groundwater"].enabled is False


def test_v4_overbank_branch_alone():
    p = v4.default_v4_params()
    q5 = v4.PESSAC_T_YEAR_LEVELS_M[1]
    b = v4.compute_risk_v4(v4.V4Features(backwater_level_m=q5, runoff_q_m3s=0.0), p)
    assert b.score == pytest.approx(35.0) and b.dominant == "overbank" and b.level == RiskLevel.ORANGE
    # overbank switched off: a dry Dordogne flood scores 0
    off = v4.compute_risk_v4(v4.V4Features(backwater_level_m=q5, runoff_q_m3s=0.0), replace(p, use_overbank=False))
    assert off.score == 0.0
    # runoff missing: overbank still scores, joint flagged unavailable
    miss = v4.compute_risk_v4(v4.V4Features(backwater_level_m=7.9), p)
    assert miss.joint_score is None and miss.score >= 55 and "runoff" in miss.unavailable
    assert any("joint branch unavailable" in n for n in miss.notes)


def test_v4_groundwater_switch_and_missing_inputs():
    p = replace(v4.default_v4_params(), use_groundwater=True)
    hi = v4.PIEZO_PERCENTILES_NGF[3]
    b = v4.compute_risk_v4(v4.V4Features(backwater_level_m=1.0, runoff_q_m3s=0.1, groundwater_ngf=hi), p)
    assert b.dominant == "groundwater" and b.score == pytest.approx(55.0)
    none = v4.compute_risk_v4(v4.V4Features(), v4.default_v4_params())
    assert none.score is None and none.level is None
    assert set(none.unavailable) == {"backwater", "runoff", "overbank"}
    d = b.to_dict()
    assert d["model_version"] == "4.0a" and d["mechanisms"]["groundwater"]["enabled"] is True


def test_v4_confluence_source_uses_zc():
    p = replace(v4.default_v4_params(), backwater_source="confluence")
    q2 = v4.EYRAUD_Q2_M3S
    zc_orange = v4.CONFLUENCE_THRESHOLDS_NGF[1]
    b = v4.compute_risk_v4(v4.V4Features(backwater_level_m=0.5, confluence_stage_ngf=zc_orange,
                                         runoff_q_m3s=2 * q2), p)
    assert b.joint_score == pytest.approx(35.0)
    assert b.mechanisms["backwater"].input_value == zc_orange
    with pytest.raises(ValueError):
        v4.compute_risk_v4(v4.V4Features(backwater_level_m=1.0), replace(p, confluence=None))


# ---------------------------------------------------------------------------
# Eval wrapper vs module vs independent vectorised implementation
# ---------------------------------------------------------------------------

def _rows(n=24 * 30, seed=7):
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        t = T0 + i * H

        def maybe(v):
            return None if rng.random() < 0.05 else float(v)

        values = {
            "rain_1h": float(rng.exponential(0.5)), "rain_24h": 10.0, "rain_72h": 20.0,
            "pessac_max_12h": maybe(rng.uniform(0.0, 8.0)),
            "q_eyraud_max_12h": maybe(rng.exponential(8.0)),
            "q_eyraud_lag0_max_12h": maybe(rng.exponential(8.0)),
            "q_caudeau_max_12h": maybe(rng.exponential(5.0)),
            "glofas_lidoire_m3s": maybe(rng.exponential(4.0)),
            "z_conf_ngf": maybe(rng.uniform(4.0, 9.0)),
            "piezo_ngf": maybe(rng.uniform(6.0, 7.5)),
        }
        rows.append(FeatureRow(t, RiskFeatures(timestamp=t, stations=(), primary_station_id="P"), values))
    return rows


class _DS:
    def __init__(self, rows):
        self.rows = rows


@pytest.mark.parametrize("variant", ["base", "confluence", "gw", "no_overbank", "caudeau", "glofas", "gr4h"])
def test_v4_eval_wrapper_equals_module_and_vectorised(variant):
    rows = _rows()
    base = v4.default_v4_params()
    runoff = "q_eyraud"
    params = base
    if variant == "confluence":
        params = replace(base, backwater_source="confluence")
    elif variant == "gw":
        params = replace(base, use_groundwater=True)
    elif variant == "no_overbank":
        params = replace(base, use_overbank=False)
    elif variant == "caudeau":
        runoff, params = "q_caudeau", replace(base, runoff=v4.runoff_thresholds(v4.CAUDEAU_Q2_M3S))
    elif variant == "glofas":
        runoff, params = "q_glofas", replace(base, runoff=v4.runoff_thresholds(v4.GLOFAS_Q2_M3S))
    elif variant == "gr4h":
        runoff, params = "q_gr4h", replace(base, runoff=v4.GradedThresholds(0.0, 0.2, 0.4, 0.6))
    model = mx.V4AlphaModel(params, runoff, name=f"v4a_{variant}")
    scores = replay(model, _DS(rows))
    table = mx.feature_table(rows)
    vec = mx.v4_alpha_scores(table, params, runoff)
    for i, (s, sv) in enumerate(zip(scores, vec)):
        if s is None:
            assert math.isnan(sv), i
        else:
            assert s == pytest.approx(sv, abs=1e-9), i
    # the wrapper is exactly the module on the same features
    ext = mx.CandidateFeatures()
    for row, s in zip(rows[:200], scores[:200]):
        f = ext.update(row.timestamp, row.values)
        if runoff == "q_gr4h":
            break
        direct = v4.compute_risk_v4(model.v4_features(f), params).score
        assert direct == s
    assert sum(1 for s in scores if s is not None) > 0.8 * len(rows)


def test_v4_wrapper_rejects_unknown_runoff():
    with pytest.raises(ValueError):
        mx.V4AlphaModel(v4.default_v4_params(), "rain_24h")
