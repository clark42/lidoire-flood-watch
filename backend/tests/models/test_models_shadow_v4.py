"""Shadow model 4.0a: production feature builder + compute_risk_v4 == offline candidate v4a_joint."""

import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.services import influx_client, risk_shadow_v4, risk_v2, risk_v4
from app.services.collectors import hydro_indicators
from app.services.risk_core import StationConfig, backwater_level
from scripts.eval import replay
from scripts.eval import dataset as ds_mod
from scripts.eval.candidates import v4_alpha_models
from scripts.eval.dataset import EvalDataset, RawInputs, build_rows

T0 = datetime(2024, 1, 1)
PESSAC = StationConfig("P555001001", "Pessac", "Dordogne", 0.30, (3.0, 3.8, 4.5))


def _walk(rng, start, end, base, lo, hi, sigma, steps=(5, 15, 15, 30, 60, 90)):
    """Irregular bounded random walk (a few multi-hour gaps)."""
    pts, t, v = [], start, base
    while t <= end:
        pts.append((t, round(v, 3)))
        step = rng.choice(steps) if rng.random() > 0.01 else rng.randint(180, 900)
        t += timedelta(minutes=step)
        v = min(hi, max(lo, v + rng.gauss(0.0, sigma)))
    return pts


def _synthetic(seed):
    """Pessac levels, Eyraud Q (with a daily QmnJ fallback gap) and daily piezometer levels."""
    rng = random.Random(seed)
    end = T0 + timedelta(days=14)
    pessac = _walk(rng, T0, end, 4.0, 1.0, 7.5, 0.08)
    q = _walk(rng, T0, end, 8.0, 0.2, 35.0, 0.6)
    # A 2-day hole in the variable-step Q, filled by daily values (QmnJ in prod, D 00:00)
    hole = (T0 + timedelta(days=6), T0 + timedelta(days=8))
    q = [(ts, v) for ts, v in q if not hole[0] <= ts < hole[1]]
    daily_q = [(hole[0] + timedelta(days=i), round(rng.uniform(5.0, 25.0), 3)) for i in range(2)]
    piezo = [((T0 + timedelta(days=i)).date(), round(6.3 + rng.uniform(0.0, 1.2), 3))
             for i in range(15) if i not in (9, 10, 11, 12)]  # 4 missing days: max age exercised
    return pessac, q, daily_q, piezo


def _eval_rows(pessac, q, daily_q, piezo, start, end):
    raw = RawInputs(
        station_points={PESSAC.station_id: pessac},
        rain_by_point={"a": {T0 + timedelta(hours=i): 0.0 for i in range(24 * 15)}},
        # offline daily fallback: stamped at the end of the day (D+1 00:00)
        discharge={"eyraud": sorted(q + [(ts + timedelta(days=1), v) for ts, v in daily_q])},
        piezo_daily=piezo,
    )
    rows = build_rows(raw, start, end, (PESSAC,), PESSAC.station_id)
    return EvalDataset(rows, (PESSAC,), PESSAC.station_id, {})


def _prod_rows(q, daily_q, piezo):
    """The stored rows as the collectors write them (river_flow / groundwater)."""
    flow = [{"timestamp": ts, "value": v, "source": "hydroportail"} for ts, v in q]
    flow += [{"timestamp": ts, "value": v, "source": hydro_indicators.SOURCE_HUBEAU_ELAB} for ts, v in daily_q]
    gw = [{"timestamp": datetime(d.year, d.month, d.day), "value": v, "source": hydro_indicators.SOURCE_ADES_DAILY}
          for d, v in piezo]
    return flow, gw


def _models(dataset):
    return {m.name: m for m, _ in v4_alpha_models(dataset)}


def test_shadow_v4_constants_match_eval_and_collectors():
    assert risk_shadow_v4.SHADOW_V4_MODEL_VERSION == risk_v4.MODEL_VERSION_V4 == "4.0a"
    assert ds_mod.HYDRO_Q_SERIES["eyraud"] == ("P540401002", risk_shadow_v4.EYRAUD_PUBLICATION_LAG_H)
    assert ds_mod.ROLLING_WINDOW_H == risk_shadow_v4.RUNOFF_WINDOW_H
    assert ds_mod.PIEZO_LAG_AFTER_DAY_H == risk_shadow_v4.PIEZO_LAG_AFTER_DAY_H
    assert ds_mod.DAILY_INPUT_MAX_AGE_DAYS == risk_shadow_v4.DAILY_INPUT_MAX_AGE_DAYS
    assert risk_shadow_v4.FLOW_MEASUREMENT == hydro_indicators.FLOW_MEASUREMENT
    assert risk_shadow_v4.GROUNDWATER_MEASUREMENT == hydro_indicators.GROUNDWATER_MEASUREMENT
    assert risk_shadow_v4.SOURCE_FLOW_DAILY == hydro_indicators.SOURCE_HUBEAU_ELAB
    assert risk_shadow_v4.SOURCE_GROUNDWATER_DAILY == hydro_indicators.SOURCE_ADES_DAILY
    from app.config import get_settings

    s = get_settings()
    eyraud = next(x for x in s.indicator_flow_stations if x["key"] == risk_shadow_v4.EYRAUD_KEY)
    assert eyraud["id"] == "P540401002"
    assert any(x["key"] == risk_shadow_v4.GROUNDWATER_KEY and x["code_bss"] == ds_mod.PIEZO_BSS
               for x in s.indicator_groundwater_stations)


def test_shadow_v4_params_are_the_eval_v4a_joint():
    pessac, q, daily_q, piezo = _synthetic(1)
    dataset = _eval_rows(pessac, q, daily_q, piezo, T0 + timedelta(days=1), T0 + timedelta(days=2))
    models = _models(dataset)
    assert models["v4a_joint"].params == risk_shadow_v4.shadow_v4_params()
    assert models["v4a_joint"].runoff_feature == "q_eyraud"
    assert risk_shadow_v4.shadow_v4_params().use_groundwater is False


@pytest.mark.parametrize("seed", [3, 4, 5])
def test_shadow_v4_production_builder_equals_offline_candidate(seed):
    pessac, q, daily_q, piezo = _synthetic(seed)
    start, end = T0 + timedelta(hours=12), T0 + timedelta(days=14)
    dataset = _eval_rows(pessac, q, daily_q, piezo, start, end)
    models = _models(dataset)
    joint = replay(models["v4a_joint"], dataset)
    with_gw = replay(models["v4a_joint+gw"], dataset)

    flow_rows, gw_rows = _prod_rows(q, daily_q, piezo)
    eyraud = risk_shadow_v4.eyraud_points_from_rows(flow_rows)
    daily = risk_shadow_v4.groundwater_daily_from_rows(gw_rows)
    assert daily == piezo
    params_gw = replace(risk_shadow_v4.shadow_v4_params(), use_groundwater=True)

    n_scored = n_joint = n_overbank = n_no_runoff = n_gw = 0
    for row, off, off_gw in zip(dataset.rows, joint, with_gw):
        h = backwater_level(pessac, row.timestamp)
        feats = risk_shadow_v4.build_v4_features(row.timestamp, h, eyraud, daily, realtime=False)
        assert feats.backwater_level_m == row.values["pessac_max_12h"]
        assert feats.runoff_q_m3s == row.values["q_eyraud_max_12h"]
        assert feats.groundwater_ngf == row.values["piezo_ngf"]
        bd = risk_shadow_v4.compute_shadow_v4(feats)
        assert bd.score == off
        assert risk_v4.compute_risk_v4(feats, params_gw).score == off_gw
        if off is not None:
            n_scored += 1
            n_joint += bd.dominant == "joint" and bd.score > 0
            n_overbank += bd.dominant == "overbank" and bd.score > 0
            n_no_runoff += feats.runoff_q_m3s is None
            n_gw += feats.groundwater_ngf is not None
    # the generated rows exercise every branch and the missing inputs
    assert n_scored > 250 and n_joint > 0 and n_overbank > 0 and n_no_runoff > 0
    assert 0 < n_gw < n_scored


def test_shadow_v4_realtime_window_equals_offline_with_7h_latency():
    """Realtime uses what is stored at t: with a 7 h publication latency the window is the offline one."""
    rng = random.Random(7)
    q = _walk(rng, T0, T0 + timedelta(days=5), 8.0, 0.2, 35.0, 0.6)
    for _ in range(2000):
        t = T0 + timedelta(minutes=rng.randint(0, 5 * 24 * 60))
        stored_at_t = [p for p in q if p[0] <= t - timedelta(hours=7)]
        assert risk_shadow_v4.runoff_q_at(stored_at_t, t, realtime=True) == \
            risk_shadow_v4.runoff_q_at(q, t, realtime=False)
    # fresher points (publication sooner than 7 h) are used in realtime, never in history
    t = T0 + timedelta(days=2)
    pts = [(t - timedelta(hours=8), 5.0), (t - timedelta(hours=1), 20.0)]
    assert risk_shadow_v4.runoff_q_at(pts, t, realtime=True) == 20.0
    assert risk_shadow_v4.runoff_q_at(pts, t, realtime=False) == 5.0
    # bounds: t - 19.5 h counts, earlier does not
    edge = [(t - timedelta(hours=19.5, seconds=1), 30.0), (t - timedelta(hours=19.5), 4.0)]
    assert risk_shadow_v4.runoff_q_at(edge, t, realtime=False) == 4.0
    assert risk_shadow_v4.runoff_q_at(edge[:1], t, realtime=False) is None


def test_shadow_v4_groundwater_hourly_fallback_and_daily_preference():
    d0 = datetime(2026, 2, 10)
    rows = [
        {"timestamp": d0, "value": 7.0, "source": "ades-daily"},
        {"timestamp": d0 + timedelta(hours=5), "value": 9.9, "source": "ades-tr"},  # daily wins
        {"timestamp": d0 + timedelta(days=1, hours=1), "value": 7.2, "source": "ades-tr"},
        {"timestamp": d0 + timedelta(days=1, hours=13), "value": 7.4, "source": "ades-tr"},
    ]
    daily = risk_shadow_v4.groundwater_daily_from_rows(rows)
    assert daily == [(d0.date(), 7.0), ((d0 + timedelta(days=1)).date(), pytest.approx(7.3))]
    # day D used from D+1 12:00
    assert risk_shadow_v4.daily_value_at(daily, d0 + timedelta(days=1, hours=11, minutes=59)) is None
    assert risk_shadow_v4.daily_value_at(daily, d0 + timedelta(days=1, hours=12)) == 7.0
    assert risk_shadow_v4.daily_value_at(daily, d0 + timedelta(days=2, hours=12)) == pytest.approx(7.3)
    assert risk_shadow_v4.daily_value_at(daily, d0 + timedelta(days=6, hours=12)) is None


# --- production backfill path ------------------------------------------------------------

class _Store:
    """In-memory influx_client: Pessac levels for v3.4, Eyraud / piezometer for 4.0a."""

    def __init__(self, pessac, flow_rows, gw_rows):
        self.pessac, self.flow_rows, self.gw_rows = pessac, flow_rows, gw_rows
        self.writes: list[dict] = []

    async def query_river_levels(self, station_id, start, end):
        pts = self.pessac if station_id == PESSAC.station_id else []
        return [{"timestamp": ts.replace(tzinfo=timezone.utc), "height_m": h} for ts, h in pts if start <= ts < end]

    async def query_rainfall(self, start, end, source=None):
        return []

    async def query_tides(self, port, start, end):
        return []

    async def query_risk_scores(self, start, end, model_version=None):
        return []

    async def write_points_batch(self, points, chunk_size=5000):
        self.writes.extend(influx_client.point_as_dict(p) for p in points)
        return len(points)

    async def eyraud(self, start, end):
        return [r for r in self.flow_rows if start <= r["timestamp"] < end]

    async def gw(self, start, end):
        return [r for r in self.gw_rows if start <= r["timestamp"] < end]


async def test_shadow_v4_backfill_path_equals_offline_candidate(monkeypatch):
    pessac, q, daily_q, piezo = _synthetic(11)
    flow_rows, gw_rows = _prod_rows(q, daily_q, piezo)
    store = _Store(pessac, flow_rows, gw_rows)
    for name in ("query_river_levels", "query_rainfall", "query_tides", "query_risk_scores", "write_points_batch"):
        monkeypatch.setattr(influx_client, name, getattr(store, name))
    monkeypatch.setattr(risk_shadow_v4, "load_eyraud_rows", store.eyraud)
    monkeypatch.setattr(risk_shadow_v4, "load_groundwater_rows", store.gw)

    start, end = T0 + timedelta(hours=12), T0 + timedelta(days=14)
    # small windows: the window cut must not change any score
    await risk_v2.backfill_risk_scores(start, end, shadow=True, chunk_days=2.5)
    v4 = {w["timestamp"]: w for w in store.writes
          if w["measurement"] == "risk_score_shadow" and w["tags"]["model_version"] == "4.0a"}

    dataset = _eval_rows(pessac, q, daily_q, piezo, start, end)
    offline = replay(_models(dataset)["v4a_joint"], dataset)
    expected = {row.timestamp: off for row, off in zip(dataset.rows, offline) if off is not None}
    assert len(expected) > 250
    assert {ts: w["fields"]["score"] for ts, w in v4.items()} == expected
    some = next(iter(v4.values()))
    assert some["tags"] == {"model_version": "4.0a", "source": "backfill"}
    assert some["fields"]["dominant"] in ("joint", "overbank")
