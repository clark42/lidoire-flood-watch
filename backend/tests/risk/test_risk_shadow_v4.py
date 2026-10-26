"""Shadow model 4.0a: written by the hourly job and the opt-in backfill; v3.4 and 3.4-gate unchanged."""

from datetime import timedelta

import pytest

from app.services import influx_client, risk_shadow, risk_shadow_v4, risk_v2
from risk_scenario import build_scenario
from test_risk_shadow import _ShadowInflux

V4 = "4.0a"


def _eyraud_rows(t0, published_until):
    """Eyraud Q every 15 min until ``published_until``: 30 m3/s peak 9 h before t0."""
    rows, t = [], t0 - timedelta(hours=40)
    while t <= published_until:
        q = 30.0 if abs((t - (t0 - timedelta(hours=9))).total_seconds()) < 1800 else 6.0
        rows.append({"timestamp": t, "value": q, "source": "hubeau-tr"})
        t += timedelta(minutes=15)
    return rows


@pytest.fixture
def world(monkeypatch):
    sc = build_scenario()
    stub = _ShadowInflux(sc)
    for name in ("query_river_levels", "query_rainfall", "query_tides", "query_risk_scores",
                 "write_risk_score", "write_points_batch"):
        monkeypatch.setattr(influx_client, name, getattr(stub, name))
    inputs = risk_v2.RealtimeInputs(
        timestamp=sc.t0, tide=sc.tide, rainfall=sc.rainfall, rainfall_raw=sc.rainfall,
        stations=sc.stations_data,
    )

    async def fake_gather(now=None):
        return inputs

    monkeypatch.setattr(risk_v2, "gather_realtime_inputs", fake_gather)

    state = {"flow": _eyraud_rows(sc.t0, sc.t0 - timedelta(hours=7)), "fail": False, "calls": []}

    async def eyraud(start, end):
        state["calls"].append((start, end))
        if state["fail"]:
            raise RuntimeError("influx down")
        return [r for r in state["flow"] if start <= r["timestamp"] < end]

    async def gw(start, end):
        if state["fail"]:
            raise RuntimeError("influx down")
        return []

    monkeypatch.setattr(risk_shadow_v4, "load_eyraud_rows", eyraud)
    monkeypatch.setattr(risk_shadow_v4, "load_groundwater_rows", gw)
    return sc, stub, inputs, state


def _by_version(stub, version):
    return [w for w in stub.of("risk_score_shadow") if w["model_version"] == version]


async def test_shadow_v4_realtime_job_stores_third_series(world):
    sc, stub, inputs, state = world
    risk, _ = await risk_v2.compute_current_risk(store=True)

    assert risk.model_version == "3.4"
    assert [(w["score"], w["model_version"]) for w in stub.of("risk_score")] == [(risk.score, "3.4")]
    [gate] = _by_version(stub, "3.4-gate")
    [v4] = _by_version(stub, V4)
    assert v4["source"] == "realtime" and v4["timestamp"] == sc.t0
    f = v4["factors"]
    # same H_aval as the 3.4-gate shadow (Pessac 12.5 h max = 3.2 m)
    assert f["h_aval_m"] == gate["factors"]["h_aval_m"] == pytest.approx(3.2)
    assert f["h_aval_m"] == inputs.features().backwater_level_m
    # realtime: every point stored at t (published until t - 7 h) over [t - 19.5 h, t]
    assert f["q_eyraud_m3s"] == 30.0
    expected = risk_shadow_v4.compute_shadow_v4(risk_shadow_v4.build_v4_features(
        sc.t0, f["h_aval_m"], risk_shadow_v4.eyraud_points_from_rows(state["flow"]), [], realtime=True,
    ))
    assert v4["score"] == expected.score and v4["level"] == expected.level.value
    assert f["runoff_available"] == 1.0 and f["groundwater_available"] == 0.0
    assert f["joint"] == pytest.approx(min(f["backwater"], f["runoff"]))
    # the Eyraud read covers the window plus the daily-fallback day, up to t
    (start, end), = state["calls"]
    assert start <= sc.t0 - timedelta(hours=19.5 + 24) and end > sc.t0


async def test_shadow_v4_failure_never_breaks_v34_or_gate(world):
    sc, stub, inputs, state = world
    state["fail"] = True
    risk, _ = await risk_v2.compute_current_risk(store=True)
    assert risk.score == risk_v2.risk_from_inputs(inputs).score
    assert len(stub.of("risk_score")) == 1
    assert len(_by_version(stub, "3.4-gate")) == 1
    assert _by_version(stub, V4) == []


async def test_shadow_v4_compute_error_never_breaks_v34_or_gate(world, monkeypatch):
    _, stub, inputs, _ = world

    def boom(*a, **k):
        raise ValueError("bad thresholds")

    monkeypatch.setattr(risk_shadow_v4, "compute_shadow_v4", boom)
    risk, _ = await risk_v2.compute_current_risk(store=True)
    assert risk.score == risk_v2.risk_from_inputs(inputs).score
    assert len(stub.of("risk_score")) == 1 and len(_by_version(stub, "3.4-gate")) == 1
    assert _by_version(stub, V4) == []


async def test_shadow_v4_missing_eyraud_is_recorded_unavailable(world):
    _, stub, _, state = world
    state["flow"] = []
    await risk_v2.compute_current_risk(store=True)
    [v4] = _by_version(stub, V4)
    f = v4["factors"]
    assert f["runoff_available"] == 0.0 and "q_eyraud_m3s" not in f and "joint" not in f
    assert v4["score"] == round(f["overbank"], 2)  # risk_v4 default: remaining branch


def _strip(writes):
    return [(w["measurement"], w["model_version"], w["source"], w["timestamp"], w["score"], w["level"], w["factors"])
            for w in writes]


async def test_v34_and_gate_unchanged_by_the_v4_shadow(world, monkeypatch):
    """Regression: the realtime and backfill v3.4 / 3.4-gate points are identical with and without 4.0a."""
    sc, stub, _, _ = world
    start = sc.t0 - timedelta(hours=6)

    async def run():
        stub.writes.clear()
        await risk_v2.compute_current_risk(store=True)
        await risk_v2.backfill_risk_scores(start, sc.t0, shadow=True)
        return _strip(w for w in stub.writes if w["model_version"] != V4)

    with_v4 = await run()
    assert _by_version(stub, V4)  # the v4 path really ran

    async def no_v4(*a, **k):
        return False

    monkeypatch.setattr(risk_v2, "store_shadow_v4_score", no_v4)
    monkeypatch.setattr(risk_shadow_v4, "history_point", lambda *a, **k: None)
    without_v4 = await run()
    assert _by_version(stub, V4) == []
    assert with_v4 == without_v4
    # and the 3.4-gate is still exactly v3.4 x gate(H_aval)
    gates = [w for w in stub.writes if w["model_version"] == "3.4-gate" and w["source"] == "backfill"]
    base = {w["timestamp"]: w["score"] for w in stub.of("risk_score") if w["source"] == "backfill"}
    assert len(gates) == 7
    for g in gates:
        assert g["score"] == pytest.approx(base[g["timestamp"]] * risk_shadow.backwater_gate(g["factors"]["h_aval_m"]))


async def test_shadow_v4_backfill_is_opt_in_and_hourly(world):
    sc, stub, _, state = world
    state["flow"] = _eyraud_rows(sc.t0, sc.t0)  # history: everything stored, the 7 h lag is applied
    start = sc.t0 - timedelta(hours=3)

    await risk_v2.backfill_risk_scores(start, sc.t0)
    assert stub.of("risk_score_shadow") == []
    assert state["calls"] == []  # v4 inputs not even read

    stub.writes.clear()
    stored = await risk_v2.backfill_risk_scores(start, sc.t0, shadow=True)
    assert stored == 4  # v3.4 count only
    v4 = _by_version(stub, V4)
    assert [w["timestamp"] for w in v4] == [start + timedelta(hours=i) for i in range(4)]
    assert {w["source"] for w in v4} == {"backfill"}
    pts = risk_shadow_v4.eyraud_points_from_rows(state["flow"])
    for w in v4:
        assert w["factors"]["q_eyraud_m3s"] == risk_shadow_v4.runoff_q_at(pts, w["timestamp"], realtime=False)
    # one read per 30-day window
    assert len(state["calls"]) == 1


async def test_shadow_v4_backfill_window_without_inputs_skips_only_v4(world):
    sc, stub, _, state = world
    state["fail"] = True
    start = sc.t0 - timedelta(hours=3)
    assert await risk_v2.backfill_risk_scores(start, sc.t0, shadow=True) == 4
    assert len(stub.of("risk_score")) == 4 and len(_by_version(stub, "3.4-gate")) == 4
    assert _by_version(stub, V4) == []


async def test_shadow_v4_backfill_compute_error_skips_only_v4(world, monkeypatch):
    sc, stub, _, _ = world

    def boom(*a, **k):
        raise ValueError("bad thresholds")

    monkeypatch.setattr(risk_shadow_v4, "compute_shadow_v4", boom)
    start = sc.t0 - timedelta(hours=3)
    assert await risk_v2.backfill_risk_scores(start, sc.t0, shadow=True) == 4
    assert len(stub.of("risk_score")) == 4 and len(_by_version(stub, "3.4-gate")) == 4
    assert _by_version(stub, V4) == []
