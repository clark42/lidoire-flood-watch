"""Unit tests of the pure scoring core (risk_core)."""

from datetime import datetime

import pytest

from app.models.schemas import RiskLevel, Tendency
from app.services import risk_core
from app.services.risk_core import (
    RiskFeatures,
    StationFeature,
    compute_risk,
    conjunction_multiplier,
    normalize_linear,
    normalize_score,
    stability_dampener,
)
from app.services.risk_v2 import _normalize_score

T = datetime(2024, 2, 10, 12, 0)
PESSAC_TH = (3.0, 3.8, 4.5)


def station(sid="P555001001", h=3.0, rate=0.0, w=0.3, th=PESSAC_TH):
    return StationFeature(sid, sid, "Dordogne", h, rate, w, th)


def features(stations, **kw):
    base = dict(
        timestamp=T, stations=tuple(stations), primary_station_id="P555001001",
        rain_24h_mm=0.0, rain_72h_mm=0.0, rain_7d_mm=0.0,
        tide_available=True, tide_coefficient=70, hours_to_high_tide=6.0,
        westerly_wind_kmh=0.0, soil_saturation_factor=1.0,
    )
    base.update(kw)
    return RiskFeatures(**base)


# --- _normalize_score ------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (-1.0, 0.0),
    (0.0, 0.0),
    (1.5, 0.165),          # half of yellow
    (3.0, 0.33),           # yellow
    (3.4, 0.495),          # middle yellow-orange
    (3.8, 0.66),           # orange
    (4.5, 1.0),            # red
    (9.0, 1.0),            # above red
])
def test_risk_normalize_linear(value, expected):
    assert _normalize_score(value, PESSAC_TH, exponential=False) == pytest.approx(expected)
    assert normalize_linear(value, PESSAC_TH) == pytest.approx(expected)


def test_risk_normalize_curve():
    assert _normalize_score(3.0, PESSAC_TH) == pytest.approx(0.33 ** 0.6)
    assert _normalize_score(3.0, PESSAC_TH) == pytest.approx(0.514, abs=1e-3)
    assert _normalize_score(0.0, PESSAC_TH) == 0.0
    assert _normalize_score(5.0, PESSAC_TH) == 1.0
    # Monotonic
    values = [normalize_score(v / 10, PESSAC_TH) for v in range(0, 60)]
    assert values == sorted(values)


# --- Orange threshold fix ----------------------------------------------------

def test_risk_above_orange_uses_height_not_post_curve_score():
    # 3.3 m at Pessac: post-curve score ~0.66 (> 0.6, the old test) but it is
    # BELOW the 3.8 m orange threshold.
    assert normalize_score(3.3, PESSAC_TH) > 0.6
    stations = [
        station("P555001001", 3.3, 0.0, 0.30, PESSAC_TH),
        station("P514001001", 3.0, 0.0, 0.15, (2.5, 3.5, 4.5)),   # < 3.5 orange
        station("P726151001", 2.8, 0.0, 0.25, (2.2, 3.0, 4.0)),   # < 3.0 orange
    ]
    for s in stations:
        assert normalize_score(s.height_m, s.thresholds) > 0.6
    bd = compute_risk(features(stations))
    assert bd.n_above_orange == 0
    assert all(not d.above_orange for d in bd.stations)
    # No dampener bypass / no multi-station-above-orange conjunction
    assert bd.stability_dampener == 0.65

    stations_high = [
        station("P555001001", 3.8, 0.0, 0.30, PESSAC_TH),
        station("P514001001", 3.5, 0.0, 0.15, (2.5, 3.5, 4.5)),
        station("P726151001", 3.0, 0.0, 0.25, (2.2, 3.0, 4.0)),
    ]
    bd_high = compute_risk(features(stations_high))
    assert bd_high.n_above_orange == 3
    assert bd_high.stability_dampener == 1.0
    assert bd_high.conjunction_multiplier >= 1.30


# --- Conjunction branches --------------------------------------------------

def conj(**kw):
    base = dict(
        river_score=0.0, tide_phase=0.0, tendency=Tendency.STABLE, n_rising=0,
        n_above_orange=0, rain_24h_score=0.0, rain_72h_score=0.0,
        soil_saturation_factor=1.0, station_detail=True,
    )
    base.update(kw)
    return conjunction_multiplier(**base)


@pytest.mark.parametrize("kw,expected", [
    ({}, 1.0),
    # Primary trigger
    ({"n_rising": 4, "river_score": 0.36, "rain_24h_score": 0.21}, 1.45),
    ({"n_rising": 4, "river_score": 0.36}, 1.12),
    ({"n_rising": 5, "river_score": 0.2, "rain_24h_score": 0.21}, 1.25),
    ({"n_rising": 3, "river_score": 0.56}, 1.25),
    # Multi-station above orange
    ({"n_above_orange": 3, "n_rising": 1}, 1.45),
    ({"n_above_orange": 3}, 1.30),
    ({"n_above_orange": 2, "river_score": 0.56, "n_rising": 1}, 1.20),
    # Drainage impairment
    ({"river_score": 0.41, "rain_72h_score": 0.51}, 1.20),
    ({"river_score": 0.51, "rain_72h_score": 0.41}, 1.30),
    ({"river_score": 0.61, "rain_72h_score": 0.21, "station_detail": True}, 1.15),
    # Tide
    ({"river_score": 0.51, "tide_phase": 0.8}, 1.15),
])
def test_risk_conjunction_branches(kw, expected):
    assert conj(**kw) == pytest.approx(expected)


def test_risk_conjunction_aggregate_fallback_and_cap():
    # Aggregate-only (simulation): high average stands for several stations
    assert conj(river_score=0.61, station_detail=False) == pytest.approx(1.25)
    assert conj(river_score=0.61, station_detail=True) == pytest.approx(1.0)
    # Everything on: capped at 2.5
    assert conj(
        river_score=0.9, tide_phase=1.0, tendency=Tendency.RISING, n_rising=6,
        n_above_orange=5, rain_24h_score=0.9, rain_72h_score=0.9,
        soil_saturation_factor=1.5,
    ) == 2.5


def test_risk_conjunction_soil_secondary_tertiary():
    base = conj(river_score=0.46, rain_72h_score=0.31)
    assert base == pytest.approx(1.0)
    assert conj(river_score=0.46, rain_72h_score=0.31, soil_saturation_factor=1.25) == pytest.approx(1.15)
    assert conj(river_score=0.46, tendency=Tendency.RISING, rain_24h_score=0.31) == pytest.approx(1.20)
    # tertiary + drainage (0.40 < river <= 0.50, rain72 > 0.5 -> +0.20)
    assert conj(river_score=0.46, rain_72h_score=0.51) == pytest.approx(1.0 + 0.20 + 0.15)


# --- Dampener branches -----------------------------------------------------

@pytest.mark.parametrize("kw,expected", [
    ({"river_score": 0.39}, 1.0),
    ({"river_score": 0.5, "rain_72h_score": 0.41}, 1.0),
    ({"river_score": 0.5, "n_above_orange": 3}, 1.0),
    ({"river_score": 0.5, "n_above_orange": 3, "station_detail": False}, 0.65),
    ({"river_score": 0.5}, 0.65),
    ({"river_score": 0.5, "tendency": Tendency.FALLING, "n_rising": 1}, 0.80),
    ({"river_score": 0.5, "tendency": Tendency.RISING, "n_rising": 1}, 0.85),
    ({"river_score": 0.5, "tendency": Tendency.RISING, "n_rising": 2}, 1.0),
    ({"river_score": 0.5, "n_rising": 2}, 1.0),
])
def test_risk_dampener_branches(kw, expected):
    base = dict(river_score=0.0, tendency=Tendency.STABLE, n_rising=0,
                rain_72h_score=0.0, n_above_orange=0, station_detail=True)
    base.update(kw)
    assert stability_dampener(**base) == pytest.approx(expected)


# --- Momentum gate ----------------------------------------------------------

@pytest.mark.parametrize("n,river,expected", [
    (0, 0.9, 0.0), (2, 0.9, 0.2), (3, 0.9, 0.5), (4, 0.9, 0.8), (6, 0.9, 1.0),
    (5, 0.45, 0.7), (5, 0.35, 0.4), (5, 0.1, 0.2),
])
def test_risk_momentum_gate(n, river, expected):
    assert risk_core.momentum_score(n, river) == pytest.approx(expected)


# --- compute_risk behaviour --------------------------------------------------

def test_risk_primary_fallback_never_libourne():
    libourne = station("P577001001", 5.0, 25.0, 0.0, (3.0, 4.0, 5.0))
    bergerac = station("P514001001", 2.0, 3.0, 0.15, (2.5, 3.5, 4.5))
    abzac = station("P726151001", 2.0, 0.5, 0.25, (2.2, 3.0, 4.0))
    bd = compute_risk(features([libourne, bergerac, abzac]))  # Pessac missing
    assert bd.primary_station_id == "P726151001"  # highest weight > 0
    assert bd.primary_rate_cm_h == 0.5
    assert bd.tendency_score == 0.3
    # Libourne does not count in the river score nor rising count
    assert bd.n_active == 2
    assert bd.n_rising == 1

    only_libourne = compute_risk(features([libourne]))
    assert only_libourne.primary_station_id is None
    assert only_libourne.tendency_score == 0.0
    assert only_libourne.river_score == 0.0


def test_risk_tide_unavailable_contributes_zero_and_is_reported():
    st = [station(h=4.0)]  # above the Pessac orange threshold: tide gate fully open
    ok = compute_risk(features(st, tide_coefficient=110, hours_to_high_tide=0.5))
    ko = compute_risk(features(st, tide_available=False, tide_coefficient=110, hours_to_high_tide=0.5))
    assert ok.tide_score == 1.0 and ok.tide_phase_score == 1.0
    assert ko.tide_score == 0.0 and ko.tide_phase_score == 0.0
    assert ko.tide_available is False
    assert any("tide" in n for n in ko.notes)
    assert ko.total_weight == ok.total_weight  # stays in the denominator
    assert ko.score < ok.score


def test_risk_same_denominator_rule():
    st = [station(h=3.5)]
    full = compute_risk(features(st))
    assert full.total_weight == pytest.approx(0.95)  # video missing only
    partial = compute_risk(features(st, rain_7d_mm=None, westerly_wind_kmh=None,
                                    soil_saturation_factor=None))
    assert partial.total_weight == pytest.approx(0.95 - 0.05 - 0.03 - 0.05)
    assert set(partial.unavailable) == {"rain_7d", "wind_west", "soil_sat", "video"}
    with_video = compute_risk(features(st, video_level=50.0))
    assert with_video.total_weight == pytest.approx(1.0)


def test_risk_soil_single_channel():
    st = [station(h=3.5)]
    dry = compute_risk(features(st, rain_24h_mm=20.0, soil_saturation_factor=1.0))
    wet = compute_risk(features(st, rain_24h_mm=20.0, soil_saturation_factor=1.5))
    assert wet.rain_24h_score == pytest.approx(min(1.0, dry.rain_24h_score * 1.5))
    assert wet.soil_sat_score == 1.0 and dry.soil_sat_score == 0.0
    assert wet.rain_24h_effective_mm == dry.rain_24h_effective_mm == 20.0


def test_risk_forecast_rain_blend():
    st = [station(h=3.0)]
    bd = compute_risk(features(st, rain_24h_mm=10.0, rain_forecast_24h_mm=10.0,
                               rain_72h_mm=20.0, rain_forecast_72h_mm=10.0))
    assert bd.rain_24h_effective_mm == pytest.approx(15.0)
    assert bd.rain_72h_effective_mm == pytest.approx(23.0)


def test_risk_levels_and_model_version():
    assert risk_core.score_to_level(14.9) == RiskLevel.GREEN
    assert risk_core.score_to_level(15) == RiskLevel.YELLOW
    assert risk_core.score_to_level(35) == RiskLevel.ORANGE
    assert risk_core.score_to_level(55) == RiskLevel.RED
    bd = compute_risk(features([station(h=4.6, rate=12.0)], rain_24h_mm=70, rain_72h_mm=130))
    assert bd.model_version == "3.4"
    assert bd.level == RiskLevel.RED
    assert 0 <= bd.score <= 100


def test_risk_tide_phase_table():
    assert risk_core.tide_phase_score(None) == 0.0
    assert risk_core.tide_phase_score(-0.5) == 1.0
    assert risk_core.tide_phase_score(1.5) == 0.8
    assert risk_core.tide_phase_score(-2.5) == 0.5
    assert risk_core.tide_phase_score(3.5) == 0.3
    assert risk_core.tide_phase_score(6.0) == 0.1
