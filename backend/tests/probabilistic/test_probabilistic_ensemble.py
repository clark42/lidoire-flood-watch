"""Ensemble basin rain: parsing, basin average, exceedance probabilities, runs.

``ensemble_icon_d2.json`` is a trimmed real answer (2026-10-26, icon_d2, the
3 basin points, 30 hours, control + 19 members).
"""

import json
import pathlib
from datetime import datetime, timedelta

import httpx
import numpy as np
import pytest
import respx

from app.services import influx_client
from app.services.collectors import ensemble_rain as ens

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
D2 = json.loads((FIXTURES / "ensemble_icon_d2.json").read_text())
T0 = datetime(2026, 10, 26, 0, 0)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    ens._runs.clear()
    ens._last_stored_run.clear()
    yield
    ens._runs.clear()
    ens._last_stored_run.clear()


def synthetic(hours: int, members: int, rate):
    """Times T0+1h..T0+hours and a (hours, members) matrix of hourly rain."""
    times = [T0 + timedelta(hours=h) for h in range(1, hours + 1)]
    return times, np.array([[rate(h, m) for m in range(members)] for h in range(hours)], dtype=float)


def test_ensemble_thresholds_are_the_v34_ones():
    th = ens.rain_thresholds()
    assert th == {24: (25.0, 40.0, 60.0), 72: (50.0, 80.0, 120.0)}


def test_ensemble_parse_real_answer():
    times, cube = ens.parse_ensemble(D2)
    assert cube.shape == (3, 30, 20)  # points, hours, members (control first)
    assert times[0] == T0 and times[-1] == T0 + timedelta(hours=29)
    assert cube[1, 5, 0] == D2[1]["hourly"]["precipitation"][5]
    assert cube[2, 7, 19] == D2[2]["hourly"]["precipitation_member19"][7]
    # Single location answers are dicts
    t1, c1 = ens.parse_ensemble(D2[0])
    assert c1.shape == (1, 30, 20) and t1 == times


def test_ensemble_basin_average_is_weighted_and_skips_missing_points():
    cube = np.array([[[1.0, 1.0]], [[3.0, np.nan]], [[np.nan, np.nan]]])  # 3 points, 1 h, 2 members
    avg = ens.basin_average(cube, [1.0, 3.0, 5.0])
    assert avg.tolist() == [[2.5, 1.0]]
    assert np.isnan(ens.basin_average(np.full((2, 1, 1), np.nan), [1, 1])).all()


def test_ensemble_window_probabilities():
    # Member m rains m mm/h during 24 h -> 24 * m mm; 10 members
    times, basin = synthetic(72, 10, lambda h, m: float(m))
    stats = ens.window_stats(times, basin, T0, 24, (25.0, 40.0, 60.0))
    totals = np.arange(10) * 24.0  # 0, 24, 48, ... 216
    assert stats["prob"] == {"25": 0.8, "40": 0.8, "60": 0.7}  # strict ">", 24 mm is not > 25
    assert stats["p50"] == pytest.approx(np.percentile(totals, 50))
    assert stats["members"] == 10 and stats["max"] == 216.0
    stats72 = ens.window_stats(times, basin, T0, 72, (50.0, 80.0, 120.0))
    assert stats72["prob"] == {"50": 0.9, "80": 0.8, "120": 0.8}  # 72 * m mm
    # Window beyond the data: None
    assert ens.window_stats(times, basin, T0 + timedelta(hours=1), 72, (50.0,)) is None


def test_ensemble_window_needs_complete_members():
    times, basin = synthetic(24, 4, lambda h, m: 2.0)
    basin[5, 0] = np.nan
    stats = ens.window_stats(times, basin, T0, 24, (25.0,))
    assert stats["members"] == 3 and stats["prob"] == {"25": 1.0}
    basin[5, 1] = basin[5, 2] = np.nan
    assert ens.window_stats(times, basin, T0, 24, (25.0,)) is None


def test_ensemble_preceding_hour_convention():
    # Rain labelled T0 (= the hour BEFORE T0) is outside the window (T0, T0+24]
    times = [T0 + timedelta(hours=h) for h in range(0, 25)]
    basin = np.zeros((25, 2))
    basin[0, :] = 100.0
    stats = ens.window_stats(times, basin, T0, 24, (25.0,))
    assert stats["max"] == 0.0


def test_ensemble_run_points_anchored_on_init():
    times, basin = synthetic(96, 5, lambda h, m: 1.0)
    pts = ens.run_points("ecmwf_ifs025", times, basin, T0, True, ens.rain_thresholds())
    windows = [(p["tags"]["window"], p["tags"]["lead"], p["timestamp"]) for p in pts]
    assert windows == [
        ("24h", "24", T0 + timedelta(hours=24)),
        ("24h", "48", T0 + timedelta(hours=48)),
        ("24h", "72", T0 + timedelta(hours=72)),
        ("24h", "96", T0 + timedelta(hours=96)),
        ("72h", "72", T0 + timedelta(hours=72)),
        ("72h", "96", T0 + timedelta(hours=96)),
        ("run", "0", T0),
    ]
    first = pts[0]
    assert first["measurement"] == "ensemble_rain"
    assert first["tags"]["run"] == "2026-10-26T00Z" and first["tags"]["model"] == "ecmwf_ifs025"
    assert first["fields"]["prob_gt_25"] == 0.0 and first["fields"]["p50"] == 24.0
    assert pts[4]["fields"]["prob_gt_50"] == 1.0
    assert pts[-1]["fields"] == {"members": 5, "horizon_hours": 96.0, "run_init_known": 1}
    for p in pts:
        for value in p["tags"].values():
            influx_client.validate_tag_value(value)


def test_ensemble_run_init_from_meta():
    now = datetime(2026, 10, 26, 21, 0)
    fresh = {"last_run_initialisation_time": 1792972800, "data_end_time": 1794279600}
    assert ens.run_init_from_meta(fresh, now) == datetime(2026, 10, 26, 0, 0)
    stale = {"last_run_initialisation_time": 1791244800, "data_end_time": 1791680400}  # DWD, 2026-10-06
    assert ens.run_init_from_meta(stale, now) is None
    assert ens.run_init_from_meta(None, now) is None


def test_ensemble_horizons_blend_models():
    times, wet = synthetic(72, 4, lambda h, m: 2.0)      # 48 mm / 24 h, all members
    _, dry = synthetic(72, 4, lambda h, m: 0.0)
    runs = {"a": {"times": times, "basin": wet}, "b": {"times": times[:30], "basin": dry[:30]}}
    models = [{"id": "a"}, {"id": "b"}]
    hz = ens.horizons(runs, models, T0, ens.rain_thresholds(), max_days=4)
    day0 = hz["24h"][0]
    assert day0["lead_start_h"] == 0 and day0["lead_end_h"] == 24
    assert day0["models"]["a"]["prob"]["40"] == 1.0 and day0["models"]["b"]["prob"]["40"] == 0.0
    assert day0["blend"] == {"prob": {"25": 0.5, "40": 0.5, "60": 0.0}, "models": ["a", "b"]}
    # Day 1 is only covered by model a
    assert hz["24h"][1]["blend"]["models"] == ["a"]
    assert len(hz["24h"]) == 3 and len(hz["72h"]) == 1


@respx.mock
async def test_ensemble_route_and_job(monkeypatch):
    now = datetime(2026, 10, 26, 1, 30)
    monkeypatch.setattr(ens, "_utcnow", lambda: now)
    settings = ens.get_settings()
    monkeypatch.setattr(settings, "ensemble_rain_models", [
        {"id": "icon_d2", "label": "ICON-D2-EPS", "meta": "dwd_icon_d2_eps", "forecast_days": 3},
    ])
    respx.get(url__startswith="https://ensemble-api.open-meteo.com/data/").mock(
        return_value=httpx.Response(200, json={"last_run_initialisation_time": 1791244800, "data_end_time": 1791680400})
    )
    api = respx.get(ens.ENSEMBLE_API_URL).mock(return_value=httpx.Response(200, json=D2))

    async def must_not_write(points, chunk_size=5000):
        raise AssertionError("route path must not write")

    monkeypatch.setattr(influx_client, "write_points_batch", must_not_write)
    body = await ens.get_ensemble_rain()
    assert body["models"][0]["run_init_known"] is False  # stale DWD meta.json
    assert body["models"][0]["run_init"] == datetime(2026, 10, 26, 1, 0)
    assert body["thresholds"] == {"24h": [25.0, 40.0, 60.0], "72h": [50.0, 80.0, 120.0]}
    assert body["horizons"]["24h"][0]["start"] == datetime(2026, 10, 26, 1, 0)
    params = dict(api.calls[0].request.url.params)
    assert params["latitude"].count(",") == 2 and params["models"] == "icon_d2"

    written = []

    async def write(points, chunk_size=5000):
        written.extend(points)
        return len(points)

    monkeypatch.setattr(influx_client, "write_points_batch", write)
    count = await ens.collect_and_store_ensemble_rain()
    assert count == len(written) and written[-1]["tags"]["window"] == "run"
    assert written[0]["tags"] == {"model": "icon_d2", "run": "2026-10-26T01Z", "window": "24h", "lead": "24"}
