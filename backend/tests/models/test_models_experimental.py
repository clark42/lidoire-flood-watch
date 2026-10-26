"""Experimental candidate models: forcing indices, GR4H, rules, logistic fit, causality."""

import math
from datetime import datetime, timedelta

import numpy as np
import pytest

from app.services import models_experimental as mx
from app.services.risk_core import RiskFeatures
from scripts.eval import FeatureRow, piecewise_score, replay
from scripts.eval.dataset import EvalDataset

T0 = datetime(2024, 1, 1)
H = timedelta(hours=1)


def _rows(n=24 * 20, rain=None, h=None):
    rain = rain or (lambda i: 2.0 if 100 <= i < 110 else 0.0)
    h = h or (lambda i: 2.0 + (2.5 if 120 <= i < 200 else 0.0))
    rows = []
    for i in range(n):
        t = T0 + i * H
        r = [rain(j) for j in range(max(0, i - 71), i + 1)]
        values = {"rain_1h": rain(i), "rain_24h": sum(r[-24:]), "rain_72h": sum(r),
                  "pessac_max_12h": h(i), "pessac_h": h(i)}
        rows.append(FeatureRow(t, RiskFeatures(timestamp=t, stations=(), primary_station_id="P"), values))
    return rows


def test_models_experimental_not_imported_by_production():
    import pathlib

    app_dir = pathlib.Path(mx.__file__).resolve().parents[1]
    offenders = [
        p for p in app_dir.rglob("*.py")
        if p.name != "models_experimental.py" and "models_experimental" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []
    # v4-alpha (risk_v4): only the shadow module "4.0a" and the offline candidate wrapper may import it
    allowed_v4 = {"risk_v4.py", "risk_shadow_v4.py", "models_experimental.py"}
    offenders_v4 = sorted(
        p.name for p in app_dir.rglob("*.py")
        if p.name not in allowed_v4 and "risk_v4" in p.read_text(encoding="utf-8")
    )
    assert offenders_v4 == []
    # v3.4, its forecast and the notifier never reference the v4 code, even through the shadow module
    for name in ("risk_core.py", "forecast.py", "notifier.py", "risk_shadow.py",
                 "episode_detector.py", "outlook.py", "forecast_archive.py"):
        text = (app_dir / "services" / name).read_text(encoding="utf-8")
        assert "risk_v4" not in text and "risk_shadow_v4" not in text, name


def test_v4_code_not_loaded_by_v34_forecast_or_notifier():
    """Importing the production model, forecast and notifier (and risk_v2, which
    imports the shadow module lazily) never loads risk_v4 / risk_shadow_v4."""
    import subprocess
    import sys

    code = (
        "import sys\n"
        "import app.services.risk_core, app.services.risk_v2, app.services.forecast, "
        "app.services.notifier, app.services.risk_shadow, app.services.episode_detector\n"
        "loaded = sorted(m for m in sys.modules if m in "
        "('app.services.risk_v4', 'app.services.risk_shadow_v4', 'app.services.models_experimental'))\n"
        "print(','.join(loaded))\n"
    )
    import pathlib

    backend = pathlib.Path(mx.__file__).resolve().parents[2]
    out = subprocess.run([sys.executable, "-c", code], cwd=backend, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""


def test_models_piecewise_matches_harness():
    th = (3.0, 3.8, 4.5)
    for v in (None, -1.0, 0.0, 1.0, 3.0, 3.5, 3.8, 4.2, 4.5, 6.0, 30.0):
        assert mx.piecewise_score(v, th) == piecewise_score(v, th)
    assert mx.piecewise_score(float("nan"), th) is None
    y, o, r = mx.steps_from_orange(4.0)
    assert o == 4.0 and y == pytest.approx(4.0 * 3.0 / 3.8) and r == pytest.approx(4.0 * 4.5 / 3.8)


def test_models_api_decay_and_reset():
    api = mx.AntecedentPrecipitationIndex(0.9)
    api.update(10.0)
    for _ in range(24):
        v = api.update(0.0)
    assert v == pytest.approx(10.0 * 0.9, rel=1e-9)
    assert api.update(-5.0) <= v  # negative rain ignored
    api.reset()
    assert api.value == 0.0
    with pytest.raises(ValueError):
        mx.AntecedentPrecipitationIndex(1.0)


def test_models_pet_climatology():
    jan = mx.climatological_pet_mm_h(datetime(2024, 1, 15)) * 24
    jul = mx.climatological_pet_mm_h(datetime(2024, 7, 15)) * 24
    assert 0.1 < jan < 0.5 and 3.5 < jul < 3.9
    total = sum(mx.climatological_pet_mm_h(datetime(2023, 1, 1) + timedelta(days=d)) * 24 for d in range(365))
    assert 650 < total < 800


def test_models_gr4h_unit_hydrographs_and_steady_state():
    g = mx.GR4H(mx.GR4HParams(x4=16.0))
    assert g.uh1.sum() == pytest.approx(1.0) and g.uh2.sum() == pytest.approx(1.0)
    assert len(g.uh1) == 16 and len(g.uh2) == 32
    # constant rain without PET: outflow tends to the rain rate (mass conservation, x2 = 0)
    q = [g.step(1.0, 0.0) for _ in range(24 * 400)]
    assert q[-1] == pytest.approx(1.0, rel=0.03)
    assert all(v >= 0 for v in q)
    # recession: monotone decrease once rain stops (after the UH delay)
    rec = [g.step(0.0, 0.0) for _ in range(24 * 10)]
    tail = rec[40:]
    assert all(b <= a + 1e-12 for a, b in zip(tail, tail[1:]))
    assert 0 < g.soil_filling <= 1


def test_models_gr4h_pet_dries_the_store():
    g = mx.GR4H()
    s0 = g.s
    for _ in range(24 * 30):
        g.step(0.0, 0.15)
    assert g.s < s0
    assert mx.mm_h_to_m3s(1.0) == pytest.approx(231.0 * 1e3 / 3600)


def test_models_features_are_causal():
    rows = _rows()
    cut = 150
    changed = _rows(rain=lambda i: (9.0 if i > cut else (2.0 if 100 <= i < 110 else 0.0)))
    a = mx.feature_table(rows)
    b = mx.feature_table(changed)
    for k in mx.FEATURE_NAMES:
        np.testing.assert_array_equal(a[k][: cut + 1], b[k][: cut + 1])
    assert not np.array_equal(a["api_fast"][cut + 1:], b["api_fast"][cut + 1:])


def test_models_extractor_resets_on_backward_timestamp():
    rows = _rows(n=200)
    ext = mx.CandidateFeatures()
    first = [ext.update(r.timestamp, r.values)["q_gr4h"] for r in rows]
    second = [ext.update(r.timestamp, r.values)["q_gr4h"] for r in rows]  # no explicit reset
    assert first == second


def test_models_backwater_rule_is_a_joint_exceedance():
    m = mx.BackwaterRuleModel(f_thresholds=(5.0, 10.0, 20.0), h_thresholds=(3.0, 3.8, 4.5), forcing="api_fast")
    assert m.score_features({"h_aval": 5.0, "api_fast": 2.0}) < 35  # backwater only
    assert m.score_features({"h_aval": 2.0, "api_fast": 50.0}) < 35  # forcing only
    assert 35 <= m.score_features({"h_aval": 4.0, "api_fast": 12.0}) < 55
    assert m.score_features({"h_aval": 4.6, "api_fast": 25.0}) >= 55
    assert m.score_features({"h_aval": None, "api_fast": 25.0}) is None
    with pytest.raises(ValueError):
        mx.BackwaterRuleModel(f_thresholds=(1, 2, 3), forcing="nope")
    ds = EvalDataset(_rows(), (), "P", {})
    s = replay(m, ds)
    assert len(s) == len(ds) and max(v for v in s if v is not None) >= 35


def test_models_v33_backwater_gate():
    m = mx.V33BackwaterModel()
    assert m.gate(2.0) == 0.5 and m.gate(3.0) == 0.5
    assert m.gate(3.4) == pytest.approx(0.75)
    assert m.gate(3.8) == 1.0 and m.gate(6.0) == 1.0
    assert m.gate(None) == 1.0 and m.gate(float("nan")) == 1.0
    assert m.score(_rows(n=1)[0]) is None  # no station -> not scored, like V33Model


def test_models_v33_backwater_never_exceeds_v33():
    from scripts.eval.dataset import build_rows, RawInputs
    from app.services.risk_core import StationConfig

    cfg = StationConfig("P555001001", "Pessac", "Dordogne", 0.30, (3.0, 3.8, 4.5))
    pts = [(T0 + i * 20 * timedelta(minutes=1), 2.0 + 2.5 * math.sin(i / 50.0) ** 2) for i in range(3 * 24 * 10)]
    raw = RawInputs(station_points={cfg.station_id: pts},
                    rain_by_point={"a": {T0 + i * H: (3.0 if i % 30 < 5 else 0.0) for i in range(24 * 10)}})
    rows = build_rows(raw, T0 + 24 * H, T0 + 9 * 24 * H, (cfg,), cfg.station_id)
    ds = EvalDataset(rows, (cfg,), cfg.station_id, {})
    from scripts.eval import V33Model

    base = replay(V33Model(), ds)
    gated = replay(mx.V33BackwaterModel(), ds)
    assert any(b is not None for b in base)
    for b, g, r in zip(base, gated, rows):
        if b is None:
            assert g is None
            continue
        assert g <= b + 1e-9
        if r.values["pessac_max_12h"] >= 3.8:
            assert g == pytest.approx(b)


def test_models_logistic_fit_recovers_and_shrinks():
    rng = np.random.default_rng(0)
    n = 20000
    x = rng.normal(size=(n, 2))
    eta = -1.0 + 1.5 * x[:, 0] - 0.8 * x[:, 1]
    y = (rng.random(n) < 1 / (1 + np.exp(-eta))).astype(float)
    fit = mx.fit_logistic_l2(x, y, l2=1e-6)
    assert fit.converged
    slopes = fit.coef / fit.std  # back to the raw scale
    assert slopes[0] == pytest.approx(1.5, abs=0.08) and slopes[1] == pytest.approx(-0.8, abs=0.08)
    strong = mx.fit_logistic_l2(x, y, l2=1e7)
    assert np.all(np.abs(strong.coef) < 0.01)
    p_mean = 1 / (1 + math.exp(-strong.intercept))
    assert p_mean == pytest.approx(y.mean(), abs=0.01)  # intercept is not penalised


def test_models_logistic_model_scores_match_vectorised():
    rows = _rows()
    table = mx.feature_table(rows)
    x = mx.design_matrix(table, ("api_fast", "h_aval"))
    y = (table["api_fast"] > 10).astype(float) * (table["h_aval"] > 3)
    fit = mx.fit_logistic_l2(x, y, l2=1.0)
    m = mx.logistic_model_from_fit(fit, ("api_fast", "h_aval"), name="lr")
    assert m.kind == "probability" and m.thresholds == {"orange": 0.05, "red": 0.20}
    ds = EvalDataset(rows, (), "P", {})
    s = np.array(replay(m, ds), dtype=float)
    np.testing.assert_allclose(s, m.predict_matrix(table), rtol=1e-10)
    assert m.score_features({"api_fast": None, "h_aval": 3.0}) is None
