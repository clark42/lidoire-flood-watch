"""v4-beta inputs of the replay: ERA5 windows, Eyraud rise, archived forecast, no future data."""

from datetime import datetime, timedelta

import numpy as np
import pytest

from app.services.models_experimental import V4BetaModel
from app.services.risk_core import StationConfig
from scripts.eval import runoff_nowcast as rn
from scripts.eval import sources
from scripts.eval.dataset import RawInputs, build_rows

PESSAC = StationConfig("P555001001", "Pessac", "Dordogne", 0.30, (3.0, 3.8, 4.5))
START = datetime(2024, 2, 1)
END = datetime(2024, 2, 6)
H = timedelta(hours=1)


def test_v4b_past_window_sums_and_missing_hours():
    rain = np.array([1.0, 2.0, np.nan, 4.0, 5.0, 6.0])
    s2 = rn.past_window_sums(rain, 2)
    assert np.isnan(s2[0]) and s2[1] == 3.0
    assert np.isnan(s2[2]) and np.isnan(s2[3])  # a missing hour invalidates the window
    assert s2[4] == 9.0 and s2[5] == 11.0
    fwd = rn.forward_rain_sums(np.arange(6, dtype=float), 2)
    assert fwd[0] == 1.0 + 2.0 and np.isnan(fwd[-1])


def test_v4b_latest_values_and_forward_max():
    pts = [(datetime(2024, 1, 1, 0), 1.0), (datetime(2024, 1, 1, 3), 5.0), (datetime(2024, 1, 2, 0), 2.0)]
    grid = rn.hourly_grid(datetime(2024, 1, 1, 0), datetime(2024, 1, 1, 20))
    lv = rn.latest_values(pts, grid, 12.0)
    assert lv[0] == 1.0 and lv[2] == 1.0 and lv[3] == 5.0 and lv[15] == 5.0 and np.isnan(lv[16])
    fm = rn.forward_max(pts, grid, 6)
    assert fm[0] == 5.0 and fm[3] == 5.0 and fm[4] == 5.0
    assert fm[18] == 2.0  # next point within [t, t + 6 h]


def test_v4b_previous_runs_forward_sum_split_and_window():
    t = datetime(2024, 3, 1, 12)
    day1 = {t + k * H: 1.0 for k in range(-5, 30)}
    day2 = {t + k * H: 10.0 for k in range(-5, 30)}
    assert rn.previous_runs_forward_sum(day1, day2, t) == 20 * 1.0 + 4 * 10.0
    # the label t itself (rain already observed) is never read: changing it changes nothing
    assert rn.previous_runs_forward_sum({**day1, t: 99.0}, day2, t) == 60.0
    missing = dict(day2)
    missing.pop(t + 22 * H)
    assert rn.previous_runs_forward_sum(day1, missing, t) is None


def _raw(shift: float = 0.0, cut=None) -> RawInputs:
    def bump(ts, v):
        return v + shift if cut is not None and ts > cut else v

    q = []
    ts = START - timedelta(days=9)
    i = 0
    while ts <= END + timedelta(days=2):
        q.append((ts, bump(ts, 2.0 + 3.0 * np.sin(i / 30.0) ** 2)))
        ts += timedelta(minutes=30)
        i += 1
    era5, rain = {}, {}
    ts = START - timedelta(days=40)
    k = 0
    while ts <= END + timedelta(days=3):
        mm = 3.0 if k % 20 < 5 else 0.0
        era5[ts] = bump(ts, mm)
        rain[ts] = mm
        ts += H
        k += 1
    pessac = [(START - timedelta(days=9) + i * timedelta(minutes=20), 3.0) for i in range(16 * 72)]
    prev = {t: 2.0 for t in (START + k * H for k in range(-48, 24 * 9))}
    return RawInputs(
        station_points={PESSAC.station_id: pessac},
        rain_by_point={"amont": rain},
        discharge={"eyraud": q},
        rain_era5_by_point={"amont": era5, "aval": era5},
        rain_prev_runs={"day1": {"amont": prev, "aval": prev}, "day2": {"amont": prev, "aval": prev}},
    )


def test_v4b_rows_values_and_lags():
    rows = build_rows(_raw(), START, END, (PESSAC,), PESSAC.station_id)
    r = next(x for x in rows if x.timestamp == datetime(2024, 2, 3, 12))
    q = dict(_raw().discharge["eyraud"])
    assert r.values["q_eyraud_m3s"] == pytest.approx(q[datetime(2024, 2, 3, 5)])
    assert r.values["q_eyraud_lag10_m3s"] == pytest.approx(q[datetime(2024, 2, 3, 2)])
    assert r.values["q_eyraud_lag3_m3s"] == pytest.approx(q[datetime(2024, 2, 3, 9)])
    era5 = _raw().rain_era5_by_point["amont"]
    t = r.timestamp
    for h in (6, 24, 72, 720):
        assert r.values[f"era5_rain_{h}h"] == pytest.approx(sum(era5[t - k * H] for k in range(h)))
    assert r.values["fc_rain_24h_prev"] == pytest.approx(48.0)


def test_v4b_rows_do_not_depend_on_future_observations():
    cut = datetime(2024, 2, 3, 12)
    rows = build_rows(_raw(), START, END, (PESSAC,), PESSAC.station_id)
    rows_p = build_rows(_raw(shift=7.0, cut=cut), START, END, (PESSAC,), PESSAC.station_id)
    model = V4BetaModel("fc")
    keys = ("q_eyraud_m3s", "q_eyraud_lag10_m3s", "q_eyraud_max_12h", "era5_rain_6h", "era5_rain_24h",
            "era5_rain_72h", "era5_rain_720h", "fc_rain_24h_prev")
    for a, b in zip(rows, rows_p):
        if a.timestamp <= cut:
            assert all(a.values[k] == b.values[k] for k in keys), a.timestamp
            assert model.score(a) == model.score(b), a.timestamp
        elif a.timestamp < cut + 7 * H:
            # Eyraud still blind (publication lag), ERA5 sees the perturbation at once
            assert a.values["q_eyraud_m3s"] == b.values["q_eyraud_m3s"]
    after = next(b for b in rows_p if b.timestamp == cut + H)
    before = next(a for a in rows if a.timestamp == cut + H)
    assert after.values["era5_rain_6h"] == pytest.approx(before.values["era5_rain_6h"] + 7.0)


def test_v4b_previous_runs_download_keys(tmp_path):
    cache = sources.RawCache(tmp_path)
    pt = {"id": "amont", "lat": 44.92, "lon": 0.05}
    cache.save("openmeteo_prevruns", "meteofrance_seamless_amont_2024-01-01_2024-12-31", {
        "hourly": {"time": ["2024-02-01T00:00", "2024-02-01T01:00"],
                   "precipitation_previous_day1": [None, 1.5], "precipitation_previous_day2": [0.5, 0.0]}})
    out = sources.download_previous_runs_rain([pt], "meteofrance_seamless", datetime(2024, 1, 1).date(),
                                              datetime(2024, 12, 31).date(), cache, client=None)
    assert out["precipitation_previous_day1"]["amont"] == {datetime(2024, 2, 1, 1): 1.5}
    assert out["precipitation_previous_day2"]["amont"] == {datetime(2024, 2, 1, 0): 0.5,
                                                           datetime(2024, 2, 1, 1): 0.0}
