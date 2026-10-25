"""realtime == backfill == TimeMachine == forecast H+0 on frozen inputs."""

from datetime import timedelta

import pytest

from app.config import get_settings
from app.services import risk_core
from app.services.forecast import ForecastInputs, build_forecast, build_forecast_features
from app.services.risk_core import StationConfig, compute_risk
from app.services.risk_v2 import (
    RealtimeInputs,
    build_realtime_features,
    rain_hourly_from_records,
    risk_from_inputs,
    station_features_from_multi,
    tide_high_list,
    tide_inputs_from_records,
    tide_forecast_is_reliable,
)

from risk_scenario import build_scenario


def _backfill_features(sc, rain_7d_available=True):
    settings = get_settings()
    configs = [StationConfig.from_settings_dict(c) for c in settings.hubeau_stations if c["weight"] > 0]
    rain_rows = [
        {"timestamp": k, "precipitation_mm_h": v, "source": "open-meteo", "city": "Moulin"}
        for k, v in sc.rain_hourly.items()
    ]
    highs, coefs = tide_inputs_from_records(sc.tide_rows)
    tide = risk_core.tide_at(sc.t0, highs, coefficient_records=coefs)
    return risk_core.build_historical_features(
        sc.t0, configs, sc.station_points, settings.hubeau_primary_station,
        rain_hourly_from_records(rain_rows), tide, rain_7d_available=rain_7d_available,
    )


def _forecast_inputs(sc, wind=None, soil=None):
    settings = get_settings()
    return ForecastInputs(
        t0=sc.t0,
        stations=station_features_from_multi(sc.stations_data, sc.t0),
        primary_station_id=sc.stations_data.primary_station_id,
        station_delays={s["id"]: float(s["delay_hours"]) for s in settings.hubeau_stations},
        rain_hourly=dict(sc.rain_hourly),
        observed_rain=sc.rainfall,
        tide_highs=tide_high_list(sc.tide),
        tide_fallback_coefficient=sc.tide.current_coefficient,
        tide_reliable=tide_forecast_is_reliable(sc.tide),
        soil_saturation_factor=soil,
        westerly_wind_kmh=wind,
    )


def _core_fields(f):
    return (
        sorted((s.station_id, round(s.height_m, 6), round(s.rate_cm_h, 6), s.weight) for s in f.stations),
        round(f.rain_24h_mm, 6), round(f.rain_72h_mm, 6),
        None if f.rain_7d_mm is None else round(f.rain_7d_mm, 6),
        f.tide_available, f.tide_coefficient, round(f.hours_to_high_tide, 6),
    )


def test_risk_realtime_equals_backfill_on_frozen_features(scenario):
    sc = scenario
    realtime = build_realtime_features(
        sc.stations_data, sc.tide, sc.rainfall, sc.t0,
        westerly_wind_kmh=None, soil_saturation_factor=None,
    )
    backfill = _backfill_features(sc)

    # Libourne is dropped by the backfill (weight 0) but kept (inactive) in realtime
    rt_active = [s for s in realtime.stations if s.weight > 0]
    assert _core_fields(backfill) == _core_fields(
        risk_core.features_with(realtime, stations=tuple(rt_active))
    )

    rt = compute_risk(realtime)
    bf = compute_risk(backfill)
    assert rt.score == bf.score
    assert rt.river_score == pytest.approx(bf.river_score)
    assert rt.n_rising == bf.n_rising == 3
    assert rt.n_above_orange == bf.n_above_orange
    assert 20 < rt.score < 90  # not saturated: equality is meaningful
    assert rt.conjunction_multiplier == bf.conjunction_multiplier
    assert rt.stability_dampener == bf.stability_dampener
    assert rt.total_weight == bf.total_weight
    assert rt.primary_station_id == bf.primary_station_id == "P555001001"


def test_risk_forecast_h0_equals_realtime_on_frozen_features(scenario):
    sc = build_scenario(forecast_24h=12.0, forecast_72h=30.0)
    inputs = RealtimeInputs(
        timestamp=sc.t0, tide=sc.tide, rainfall=sc.rainfall, rainfall_raw=sc.rainfall,
        stations=sc.stations_data, soil_saturation_factor=1.3, westerly_wind_kmh=45.0,
    )
    realtime = risk_from_inputs(inputs)
    rt_bd = compute_risk(inputs.features())

    fi = _forecast_inputs(sc, wind=45.0, soil=1.3)
    # Future rain in the series must not change H+0
    for k in range(1, 72):
        fi.rain_hourly[sc.t0 + timedelta(hours=k)] = 2.0
    fc_features, _ = build_forecast_features(fi, 0)
    fc_bd = compute_risk(fc_features)
    h0 = build_forecast(fi, 3)[0]

    assert 20 < rt_bd.score < 95
    assert fc_bd.score == rt_bd.score == realtime.score == h0["score"]
    assert fc_bd.n_rising == rt_bd.n_rising
    assert fc_bd.tendency_score == rt_bd.tendency_score
    assert fc_bd.conjunction_multiplier == rt_bd.conjunction_multiplier
    assert fc_bd.stability_dampener == rt_bd.stability_dampener
    assert fc_bd.total_weight == rt_bd.total_weight
    assert fc_bd.tide_score == rt_bd.tide_score
    assert fc_bd.tide_phase_score == rt_bd.tide_phase_score
    assert fc_bd.rain_24h_effective_mm == rt_bd.rain_24h_effective_mm


def test_risk_forecast_h0_from_series_equals_realtime_without_summary(scenario):
    """Even when H+0 rain comes from the hourly series, windows match realtime."""
    sc = scenario
    fi = _forecast_inputs(sc)
    fi.observed_rain = None
    fc_features, _ = build_forecast_features(fi, 0)
    realtime = build_realtime_features(
        sc.stations_data, sc.tide, sc.rainfall, sc.t0,
        westerly_wind_kmh=None, soil_saturation_factor=None,
    )
    assert fc_features.rain_24h_mm == pytest.approx(realtime.rain_24h_mm)
    assert fc_features.rain_72h_mm == pytest.approx(realtime.rain_72h_mm)
    assert fc_features.rain_7d_mm == pytest.approx(realtime.rain_7d_mm)
    assert compute_risk(fc_features).score == compute_risk(realtime).score
