"""Replay builder: no future data in the causal mode (and the leaky modes are flagged)."""

import math
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from app.services.risk_core import StationConfig
from scripts.eval.dataset import (
    BuildOptions,
    RawInputs,
    build_rows,
    daily_station_feature,
    interpolate_points,
    leakage_flags,
)
from scripts.eval.models import V33Model

START = datetime(2024, 2, 1)
END = datetime(2024, 2, 8)
CUT = datetime(2024, 2, 5, 12)  # data after CUT is perturbed

PESSAC = StationConfig("P555001001", "Pessac", "Dordogne", 0.30, (3.0, 3.8, 4.5))
ABZAC = StationConfig("P726151001", "Abzac", "Isle", 0.25, (2.2, 3.0, 4.0))
CONFIGS = (PESSAC, ABZAC)


def _level_series(base: float, amp: float) -> list[tuple[datetime, float]]:
    """Variable-step points: 20 min steps with a few multi-hour gaps."""
    pts = []
    t = START - timedelta(days=8)
    i = 0
    while t <= END + timedelta(days=1):
        h = base + amp * math.sin(i / 40.0) + 0.002 * i
        pts.append((t, round(h, 3)))
        i += 1
        # a 5h gap every ~2 days, else 20 minutes
        t += timedelta(hours=5) if i % 150 == 0 else timedelta(minutes=20)
    return pts


def _raw() -> RawInputs:
    rain = {}
    for pid, scale in (("amont", 1.0), ("aval", 0.5)):
        series = {}
        t = START - timedelta(days=8)
        k = 0
        while t <= END + timedelta(days=4):
            series[t] = scale * (2.0 if k % 30 < 8 else 0.0)
            t += timedelta(hours=1)
            k += 1
        rain[pid] = series
    sea = []
    t = START - timedelta(days=9)
    k = 0
    while t <= END + timedelta(days=3):
        sea.append((t, 2.0 * math.sin(2 * math.pi * k / 12.42)))
        t += timedelta(hours=1)
        k += 1
    return RawInputs(
        station_points={PESSAC.station_id: _level_series(3.5, 0.8), ABZAC.station_id: _level_series(2.4, 0.6)},
        rain_by_point=rain,
        sea_level=sea,
    )


def _perturb_after(raw: RawInputs, cut: datetime) -> RawInputs:
    """Every OBSERVED series changed after ``cut`` (tides are predictions: kept)."""
    return RawInputs(
        station_points={
            sid: [(t, h + 2.0 if t > cut else h) for t, h in pts]
            for sid, pts in raw.station_points.items()
        },
        station_daily_max={
            sid: [(d, h + 2.0 if datetime(d.year, d.month, d.day) + timedelta(days=1) > cut else h)
                  for d, h in daily]
            for sid, daily in raw.station_daily_max.items()
        },
        rain_by_point={
            pid: {t: (mm * 10 + 5 if t > cut else mm) for t, mm in s.items()}
            for pid, s in raw.rain_by_point.items()
        },
        sea_level=list(raw.sea_level),
    )


def _rows(raw, options=BuildOptions()):
    return build_rows(raw, START, END, CONFIGS, PESSAC.station_id, options)


def test_evaluation_causal_rows_do_not_depend_on_future_data():
    raw = _raw()
    rows = _rows(raw)
    rows_p = _rows(_perturb_after(raw, CUT))
    assert len(rows) == len(rows_p) == 7 * 24 + 1
    model = V33Model()
    n_past = 0
    changed_after = 0
    for r, rp in zip(rows, rows_p):
        assert r.timestamp == rp.timestamp
        if r.timestamp <= CUT:
            n_past += 1
            assert r.features == rp.features, r.timestamp
            assert r.values == rp.values, r.timestamp
            assert model.score(r) == model.score(rp)
        elif r.features != rp.features:
            changed_after += 1
    assert n_past > 24 * 3
    # sanity: the perturbation is visible right after the cut
    assert changed_after > 0
    first_after = next(r for r in rows_p if r.timestamp > CUT)
    assert first_after.values["rain_1h"] > 4


def test_evaluation_causal_features_present_and_rich():
    rows = _rows(_raw())
    r = rows[50]
    assert {s.station_id for s in r.features.stations} == {PESSAC.station_id, ABZAC.station_id}
    assert r.values["pessac_h"] == r.features.stations[0].height_m
    assert r.values["rain_24h"] == r.features.rain_24h_mm
    assert r.values["rain_72h"] == r.features.rain_72h_mm
    assert r.features.rain_7d_mm is not None
    assert r.features.tide_available and r.features.hours_to_high_tide is not None
    assert r.features.rain_forecast_24h_mm == 0.0
    for key in ("pessac_max_12h", "n_rising", "n_above_orange", "rain_1h", "rain_168h"):
        assert key in r.values


def test_evaluation_causal_mode_holds_level_across_archive_gap():
    """A 5h archive gap does not drop the station (hold <= 12h, points <= t)."""
    raw = _raw()
    pts = raw.station_points[PESSAC.station_id]
    gaps = [(a, b) for a, b in zip(pts, pts[1:])
            if (b[0] - a[0]) >= timedelta(hours=5) and START < a[0] < END - timedelta(hours=6)]
    assert gaps
    (t_gap, h_gap), _ = gaps[0]
    rows = {r.timestamp: r for r in _rows(raw)}
    t_inside = t_gap.replace(minute=0, second=0) + timedelta(hours=4)
    assert rows[t_inside].values["pessac_h"] == h_gap
    # production max age (3h) would drop it: the hold is a documented eval choice
    rows_3h = {r.timestamp: r for r in _rows(raw, BuildOptions(max_age_hours=3.0))}
    assert rows_3h[t_inside].values["pessac_h"] is None


def test_evaluation_interp_mode_uses_next_point_and_is_flagged():
    flags = leakage_flags(BuildOptions(level_mode="interp"))
    assert flags["causal"] is False and flags["notes"]
    assert leakage_flags(BuildOptions())["causal"] is True
    assert leakage_flags(BuildOptions(forecast_rain="oracle"))["causal"] is False

    t0 = datetime(2024, 2, 5, 11)
    pts = [(t0, 1.0), (t0 + timedelta(hours=2), 3.0)]
    grid = interpolate_points(pts)
    assert (datetime(2024, 2, 5, 12), 2.0) in grid  # depends on the next point
    grid2 = interpolate_points([(t0, 1.0), (t0 + timedelta(hours=2), 5.0)])
    assert (datetime(2024, 2, 5, 12), 3.0) in grid2


def test_evaluation_oracle_rain_reads_the_future():
    raw = _raw()
    rows = _rows(raw, BuildOptions(forecast_rain="oracle"))
    rows_p = _rows(_perturb_after(raw, CUT), BuildOptions(forecast_rain="oracle"))
    at_cut = next(i for i, r in enumerate(rows) if r.timestamp == CUT)
    assert rows[at_cut].features.rain_forecast_24h_mm != rows_p[at_cut].features.rain_forecast_24h_mm


def test_evaluation_daily_max_fallback_is_causal():
    cfg = PESSAC
    daily = [(date(2024, 2, d), 3.0 + 0.1 * d) for d in range(1, 8)]
    # during Feb 5 only Feb 4's max (complete at Feb 5 00:00) is known
    f = daily_station_feature(cfg, daily, datetime(2024, 2, 5, 18))
    assert f.height_m == pytest.approx(3.4)
    assert f.rate_cm_h == pytest.approx(10 / 24)
    assert daily_station_feature(cfg, daily, datetime(2024, 2, 6, 0)).height_m == pytest.approx(3.5)
    assert daily_station_feature(cfg, daily, datetime(2024, 2, 1, 23)) is None
    # stale: last complete day too old
    assert daily_station_feature(cfg, daily, datetime(2024, 2, 12, 0)) is None

    raw = RawInputs(station_daily_max={cfg.station_id: daily}, rain_by_point={}, sea_level=[])
    rows = build_rows(raw, datetime(2024, 2, 3), datetime(2024, 2, 6), (cfg,), cfg.station_id,
                      BuildOptions(use_tide=False))
    raw_p = replace(raw, station_daily_max={cfg.station_id: [
        (d, h + 5 if d >= date(2024, 2, 5) else h) for d, h in daily
    ]})
    rows_p = build_rows(raw_p, datetime(2024, 2, 3), datetime(2024, 2, 6), (cfg,), cfg.station_id,
                        BuildOptions(use_tide=False))
    for r, rp in zip(rows, rows_p):
        if r.timestamp < datetime(2024, 2, 6):
            assert r.features == rp.features
        assert r.values["n_daily_fallback"] == 1.0
    assert rows[-1].features != rows_p[-1].features  # Feb 5 max known at Feb 6 00:00
    assert not rows[0].features.tide_available
