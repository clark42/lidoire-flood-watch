"""Shadow model 3.3-gate: production gate == offline V33BackwaterModel."""

import math
import random
from datetime import datetime, timedelta

import pytest

from app.services import models_experimental as mx
from app.services import risk_shadow
from app.services.risk_core import StationConfig, compute_risk, get_model_params
from scripts.eval import V33Model, replay
from scripts.eval.dataset import ROLLING_WINDOW_H, EvalDataset, RawInputs, _window_values, build_rows

T0 = datetime(2024, 1, 1)
PESSAC = StationConfig("P555001001", "Pessac", "Dordogne", 0.30, (3.0, 3.8, 4.5))
BERGERAC = StationConfig("P514001001", "Bergerac", "Dordogne", 0.15, (2.5, 3.5, 4.5))


def _reference_gate(h_aval, h_low=3.0, h_full=3.8, min_factor=0.5):
    """Verbatim copy of the original V33BackwaterModel.gate (before the port)."""
    if h_aval is None or math.isnan(h_aval):
        return 1.0
    if h_aval <= h_low:
        return min_factor
    if h_aval >= h_full:
        return 1.0
    frac = (h_aval - h_low) / (h_full - h_low)
    return min_factor + (1.0 - min_factor) * frac


def _random_points(rng, start, end, base, amp):
    """Irregular random-walk level series (5-90 min steps, a few multi-hour gaps)."""
    pts, t, h = [], start, base
    while t <= end:
        pts.append((t, round(h, 3)))
        step = rng.choice([5, 15, 15, 30, 60, 90]) if rng.random() > 0.01 else rng.randint(180, 900)
        t += timedelta(minutes=step)
        h = min(base + amp, max(base - amp, h + rng.gauss(0.0, 0.04)))
    return pts


def test_shadow_gate_matches_reference_everywhere():
    rng = random.Random(1)
    values = [None, float("nan"), 3.0, 3.8, 3.0 - 1e-12, 3.8 + 1e-12, -5.0, 50.0]
    values += [rng.uniform(0.0, 7.0) for _ in range(20000)]
    values += [rng.uniform(2.9, 3.9) for _ in range(5000)]
    m = mx.V33BackwaterModel()
    for h in values:
        ref = _reference_gate(h)
        assert risk_shadow.backwater_gate(h) == ref
        assert m.gate(h) == ref
    # Non-default parameters (sensitivity runs of the harness) still match
    m2 = mx.V33BackwaterModel(h_low=2.5, h_full=4.0, min_factor=0.3)
    for h in values[:2000]:
        assert m2.gate(h) == _reference_gate(h, 2.5, 4.0, 0.3)


def test_shadow_backwater_level_matches_eval_window():
    rng = random.Random(2)
    pts = _random_points(rng, T0, T0 + timedelta(days=20), 3.3, 1.5)
    for _ in range(3000):
        t = T0 + timedelta(minutes=rng.randint(-600, 21 * 24 * 60))
        win = _window_values(pts, t, ROLLING_WINDOW_H)
        expected = max(win) if win else None
        assert risk_shadow.backwater_level(pts, t) == expected
    # boundaries: points exactly at t - 12.5h and at t count, t + 1s does not
    t = T0 + timedelta(hours=20)
    edge = [(t - timedelta(hours=12.5), 4.0), (t, 3.1), (t + timedelta(seconds=1), 9.9)]
    assert risk_shadow.backwater_level(edge, t) == 4.0
    assert risk_shadow.backwater_level(edge[1:], t) == 3.1
    assert risk_shadow.backwater_level(edge[2:], t) is None


@pytest.mark.parametrize("seed", [3, 4, 5])
def test_shadow_production_path_equals_offline_model(seed):
    """Production scoring (raw points -> H_aval -> gated v3.3) == replay of V33BackwaterModel."""
    rng = random.Random(seed)
    start, end = T0 + timedelta(hours=24), T0 + timedelta(days=12)
    raw = RawInputs(
        station_points={
            PESSAC.station_id: _random_points(rng, T0, end, 3.4, 1.4),
            BERGERAC.station_id: _random_points(rng, T0, end, 3.0, 1.5),
        },
        rain_by_point={"a": {T0 + timedelta(hours=i): (rng.expovariate(0.5) if rng.random() < 0.2 else 0.0)
                             for i in range(24 * 13)}},
    )
    configs = (PESSAC, BERGERAC)
    rows = build_rows(raw, start, end, configs, PESSAC.station_id)
    ds = EvalDataset(rows, configs, PESSAC.station_id, {})

    offline = replay(mx.V33BackwaterModel(), ds)
    base = replay(V33Model(), ds)
    params = get_model_params()
    n_gated = 0
    for row, off, b in zip(rows, offline, base):
        h_aval = risk_shadow.backwater_level(raw.station_points[PESSAC.station_id], row.timestamp)
        assert h_aval == row.values["pessac_max_12h"]
        prod = risk_shadow.gated_score(row.features, compute_risk(row.features, params).score, h_aval)
        if off is None:
            assert prod is None and b is None
            continue
        assert prod == pytest.approx(off, abs=1e-12)
        assert off == pytest.approx(b * _reference_gate(row.values["pessac_max_12h"]), abs=1e-12)
        n_gated += prod < b - 1e-9
    assert len([o for o in offline if o is not None]) > 200
    assert n_gated > 0  # the generated rows exercise the gate, not only the 1.0 plateau


def test_shadow_gated_score_skips_hours_without_station():
    row = build_rows(RawInputs(), T0, T0, (PESSAC,), PESSAC.station_id)[0]
    assert row.features.stations == ()
    assert risk_shadow.gated_score(row.features, 40.0, 3.0) is None
    assert mx.V33BackwaterModel().score(row) is None
