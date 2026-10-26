"""v3.4 tide gate: tide factors scaled by the backwater level (Pessac max over 12.5 h)."""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from app.config import get_settings
from app.models.schemas import MultiStationData, RiverLevel, StationLevel, Tendency
from app.services import influx_client, risk_core, risk_v2
from app.services.forecast import ForecastInputs, forecast_backwater_level
from app.services.risk_core import (
    RiskFeatures,
    StationConfig,
    StationFeature,
    TideState,
    backwater_level,
    build_historical_features,
    compute_risk,
    tide_gate,
)

T = datetime(2024, 2, 10, 12, 0)
PESSAC = "P555001001"
PESSAC_TH = (3.0, 3.8, 4.5)


def station(sid=PESSAC, h=3.0, rate=0.0, w=0.3, th=PESSAC_TH):
    return StationFeature(sid, sid, "Dordogne", h, rate, w, th)


def features(stations, **kw):
    base = dict(
        timestamp=T, stations=tuple(stations), primary_station_id=PESSAC,
        rain_24h_mm=0.0, rain_72h_mm=0.0, rain_7d_mm=0.0,
        tide_available=True, tide_coefficient=110, hours_to_high_tide=0.5,
    )
    base.update(kw)
    return RiskFeatures(**base)


def test_gate_thresholds_are_the_pessac_yellow_orange_of_the_config():
    settings = get_settings()
    cfg = next(c for c in settings.hubeau_stations if c["id"] == settings.hubeau_primary_station)
    assert risk_core.get_model_params().tide_gate_thresholds == (
        cfg["thresholds"]["yellow"], cfg["thresholds"]["orange"],
    )


@pytest.mark.parametrize("level, gate", [
    (2.0, 0.0), (3.0, 0.0), (3.2, 0.25), (3.4, 0.5), (3.8, 1.0), (5.0, 1.0), (None, 1.0),
])
def test_tide_gate_is_linear_between_yellow_and_orange(level, gate):
    assert tide_gate(level, (3.0, 3.8)) == pytest.approx(gate)


def test_gate_scales_tide_contributions_only():
    st = [station(h=3.4)]
    closed = compute_risk(features(st, backwater_level_m=3.0))
    half = compute_risk(features(st, backwater_level_m=3.4))
    full = compute_risk(features(st, backwater_level_m=3.8))
    assert (closed.tide_gate, half.tide_gate, full.tide_gate) == (0.0, pytest.approx(0.5), 1.0)
    assert full.tide_score == 1.0 and full.tide_phase_score == 1.0
    assert half.tide_score == pytest.approx(0.5) and half.tide_phase_score == pytest.approx(0.5)
    assert closed.tide_score == 0.0 and closed.tide_phase_score == 0.0
    assert closed.score < half.score < full.score
    # Same denominator and same other factors
    assert closed.total_weight == full.total_weight
    assert closed.river_score == full.river_score
    assert full.backwater_level_m == 3.8


def test_conjunction_keeps_the_ungated_phase():
    # High river + high water: the "PM + river > 0.5" branch stays active when gated
    st = [station(h=4.5)]
    gated = compute_risk(features(st, backwater_level_m=3.0))
    open_ = compute_risk(features(st, backwater_level_m=4.5))
    assert gated.tide_phase_score == 0.0
    assert gated.conjunction_multiplier == open_.conjunction_multiplier > 1.0


def test_fallback_current_primary_level_then_ungated():
    # No backwater level: the current Pessac level is used (and noted)
    bd = compute_risk(features([station(h=3.4)]))
    assert bd.tide_gate == pytest.approx(0.5)
    assert bd.backwater_level_m == 3.4
    assert any("tide gate" in n for n in bd.notes)
    # No primary station at all: tide factors ungated (v3.3 behaviour)
    other = compute_risk(features([station("P726151001", h=2.0, w=0.25, th=(2.2, 3.0, 4.0))]))
    assert other.tide_gate == 1.0 and other.backwater_level_m is None
    assert other.tide_phase_score == 1.0


def test_backwater_level_trailing_max_points_le_t():
    pts = [
        (T - timedelta(hours=13), 5.0),  # out of the 12.5 h window
        (T - timedelta(hours=12, minutes=30), 3.6),  # window start (inclusive)
        (T - timedelta(hours=2), 3.1),
        (T, 3.2),
        (T + timedelta(hours=1), 6.0),  # future: ignored
    ]
    assert backwater_level(pts, T) == 3.6
    assert backwater_level(pts, T - timedelta(hours=14)) is None
    # Any series shape is accepted (dicts, aware timestamps)
    rows = [{"timestamp": (ts.isoformat() + "+00:00"), "height_m": h} for ts, h in pts]
    assert backwater_level(rows, T) == 3.6


def test_historical_features_carry_the_backwater_level():
    cfg = StationConfig(PESSAC, "Pessac", "Dordogne", 0.3, PESSAC_TH)
    pts = [(T - timedelta(hours=10), 3.9)] + [(T - timedelta(minutes=15 * k), 3.2) for k in range(8, -1, -1)]
    feats = build_historical_features(T, [cfg], {PESSAC: pts}, PESSAC, {}, TideState(True, 110, 0.5))
    assert feats.stations[0].height_m == 3.2
    assert feats.backwater_level_m == 3.9  # peak 10 h ago, Pessac now oscillating lower
    assert compute_risk(feats).tide_gate == 1.0


def _stations_data(height=3.2):
    level = RiverLevel(timestamp=T, station_id=PESSAC, station_name="Pessac", river="Dordogne", height_m=height)
    st = StationLevel(station_id=PESSAC, station_name="Pessac", river="Dordogne", current_level=level,
                      tendency=Tendency.STABLE, weight=0.3, threshold_yellow=3.0, threshold_orange=3.8,
                      threshold_red=4.5)
    return MultiStationData(timestamp=T, stations=[st], primary_station_id=PESSAC)


def test_realtime_merges_stored_primary_levels_with_latest_hubeau():
    stored = [{"timestamp": T - timedelta(hours=8), "height_m": 3.7}, {"timestamp": T - timedelta(hours=1), "height_m": 3.3}]
    pts = risk_v2.primary_level_points(_stations_data(3.2), stored)
    assert pts[-1] == (T, 3.2)
    from app.models.schemas import RainfallSummary

    rain = RainfallSummary(last_hour_mm=0, last_24h_mm=0, last_72h_mm=0, last_7d_mm=0,
                           forecast_24h_mm=0, forecast_72h_mm=0)
    feats = risk_v2.build_realtime_features(_stations_data(3.2), None, rain, T, primary_levels=stored)
    assert feats.backwater_level_m == 3.7
    # Without stored points the latest Hub'Eau level is the backwater level
    assert risk_v2.build_realtime_features(_stations_data(3.2), None, rain, T).backwater_level_m == 3.2


def _forecast_inputs(rate_cm_h=0.0):
    return ForecastInputs(
        t0=T,
        stations=(station(h=3.2, rate=rate_cm_h),),
        primary_station_id=PESSAC,
        station_delays={PESSAC: 4.0},
        rain_hourly={},
        primary_levels=[(T - timedelta(hours=6), 3.7), (T, 3.2)],
    )


def test_forecast_backwater_level_observed_then_projected():
    fi = _forecast_inputs()
    assert forecast_backwater_level(fi, 0) == 3.7  # = realtime value
    assert forecast_backwater_level(fi, 6) == 3.7  # observed peak still within 12.5 h
    assert forecast_backwater_level(fi, 7) == 3.2  # peak left the window, flat projection
    rising = _forecast_inputs(rate_cm_h=5.0)
    # Projected rise (5 cm/h for at most 12 h) dominates after a few hours
    assert forecast_backwater_level(rising, 12) == pytest.approx(3.2 + 0.6)
    assert forecast_backwater_level(rising, 30) == pytest.approx(3.2 + 0.6)


def test_readers_prefer_v34_over_v33():
    rows = [
        {"timestamp": T, "score": 20.0, "model_version": "3.3"},
        {"timestamp": T, "score": 30.0, "model_version": "3.4"},
        {"timestamp": T, "score": 10.0, "model_version": None},
    ]
    assert risk_core.MODEL_VERSION == "3.4"
    assert influx_client._current_risk_model_version() == "3.4"
    for preferred in (risk_core.MODEL_VERSION, None):
        assert [r["score"] for r in influx_client.select_risk_rows_by_version(rows, preferred)] == [30.0]


def test_gate_equals_the_evaluated_post_hoc_variant():
    """Production gate == eval variant (c): v3.3 score minus the gated-out tide contributions."""
    st = [station(h=3.2)]
    feats = features(st, backwater_level_m=3.2)
    p = risk_core.get_model_params()
    open_gate = compute_risk(feats, replace(p, tide_gate_thresholds=(-2.0, -1.0)))
    gated = compute_risk(feats, p)
    assert open_gate.tide_gate == 1.0 and gated.tide_gate == pytest.approx(0.25)
    w = p.weights
    lost = 0.75 * (w["tide"] * open_gate.tide_score + w["tide_phase"] * open_gate.tide_phase_score)
    mult = open_gate.conjunction_multiplier * open_gate.stability_dampener
    assert gated.score == pytest.approx(
        round((open_gate.weighted_sum / mult - lost) * mult / open_gate.total_weight * 100, 1)
    )
