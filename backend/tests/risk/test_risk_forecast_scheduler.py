"""Forecast fixes (calibration, UTC, no rain re-injection) and scheduler."""

from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.models.schemas import RainfallSummary, WeatherForecast
from app.services import forecast as forecast_mod
from app.services import risk_v2
from app.services.forecast import (
    ForecastInputs,
    build_forecast,
    build_forecast_features,
    calibrate_past_rain,
    forecast_ts_to_utc,
)
from app.services.risk_core import StationFeature

from risk_scenario import build_scenario

T0 = datetime(2024, 2, 10, 12, 0)


def test_risk_forecast_naive_utc_and_aware_to_utc():
    # fetch_open_meteo_forecast() returns naive UTC timestamps (timezone=UTC)
    assert forecast_ts_to_utc(datetime(2024, 2, 10, 12, 0)) == datetime(2024, 2, 10, 12, 0)
    paris = timezone(timedelta(hours=2))
    assert forecast_ts_to_utc(datetime(2024, 7, 10, 14, 0, tzinfo=paris)) == datetime(2024, 7, 10, 12, 0)


def test_risk_forecast_calibration_capped_and_window_only():
    rain = {T0 - timedelta(hours=k): 0.5 for k in range(168)}       # model: 12 mm / 24h
    observed = RainfallSummary(last_24h_mm=240.0, last_72h_mm=264.0, last_7d_mm=312.0)
    out, info = calibrate_past_rain(rain, T0, observed)
    assert info["24h"]["factor"] == 3.0                              # capped (would be x20)
    assert sum(out[T0 - timedelta(hours=k)] for k in range(24)) == pytest.approx(36.0)
    # Older hours keep their own (here neutral) factor
    assert out[T0 - timedelta(hours=30)] == pytest.approx(0.5)
    assert out[T0 - timedelta(hours=100)] == pytest.approx(0.5)


def test_risk_forecast_calibration_injects_when_model_is_dry():
    rain = {T0 - timedelta(hours=k): 0.0 for k in range(168)}
    observed = RainfallSummary(last_24h_mm=12.0, last_72h_mm=12.0, last_7d_mm=12.0)
    out, info = calibrate_past_rain(rain, T0, observed)
    assert info["24h"] == {"injected_mm": 12.0}
    assert sum(out[T0 - timedelta(hours=k)] for k in range(24)) == pytest.approx(12.0)


def _single_station_inputs(rain_hourly, rate=0.0):
    pessac = StationFeature("P555001001", "Pessac", "Dordogne", 3.0, rate, 0.30, (3.0, 3.8, 4.5))
    return ForecastInputs(
        t0=T0, stations=(pessac,), primary_station_id="P555001001",
        station_delays={"P555001001": 4.0}, rain_hourly=rain_hourly,
        soil_saturation_factor=1.0, westerly_wind_kmh=0.0,
    )


def test_risk_forecast_does_not_reinject_past_rain():
    # 100 mm fell during the 2 days BEFORE t0, nothing after
    past_storm = {T0 - timedelta(hours=k): 100.0 / 48 for k in range(48)}
    fi = _single_station_inputs(past_storm)
    for h in (0, 12, 24, 36, 48):
        f, level = build_forecast_features(fi, h)
        assert level == pytest.approx(3.0), h      # no fake rise from past rain
        assert f.n_rising_override == 0, h


def test_risk_forecast_rain_after_t0_drives_rise_and_rising_count():
    future_storm = {T0 + timedelta(hours=k): 5.0 for k in range(1, 13)}  # 60 mm after t0
    fi = _single_station_inputs(future_storm)
    f0, lvl0 = build_forecast_features(fi, 0)
    assert lvl0 == 3.0 and f0.n_rising_override == 0      # H+0: nothing yet
    f30, lvl30 = build_forecast_features(fi, 30)
    assert lvl30 > 3.0
    assert 1 <= f30.n_rising_override <= 1                # capped to the station count
    assert f30.stations[0].rate_cm_h >= 0


def test_risk_forecast_trend_continues_from_h0():
    fi = _single_station_inputs({}, rate=2.0)
    _, lvl1 = build_forecast_features(fi, 1)
    f1, _ = build_forecast_features(fi, 1)
    assert lvl1 == pytest.approx(3.02)
    assert f1.stations[0].rate_cm_h == pytest.approx(2.0)   # Pessac rate drives tendency
    _, lvl20 = build_forecast_features(fi, 20)
    assert lvl20 == pytest.approx(3.24)                     # capped at 12h of trend


def test_risk_forecast_output_contract():
    fi = _single_station_inputs({T0 + timedelta(hours=1): 1.5})
    out = build_forecast(fi, 6)
    assert len(out) == 7
    first = out[0]
    assert first["timestamp"].endswith("+00:00")
    assert first["rain_1h_mm"] == 1.5
    for key in ("score", "level", "rain_24h_mm", "rain_72h_mm", "tide_coefficient",
                "hours_to_high_tide", "projected_level_m"):
        assert first[key] is not None


async def test_risk_get_forecast_h0_equals_realtime(monkeypatch):
    sc = build_scenario(forecast_24h=6.0, forecast_72h=10.0)
    inputs = risk_v2.RealtimeInputs(
        timestamp=sc.t0, tide=sc.tide, rainfall=sc.rainfall, rainfall_raw=sc.rainfall,
        stations=sc.stations_data, soil_wind={"x": 1}, soil_saturation_factor=1.2,
        westerly_wind_kmh=40.0,
    )

    async def no_cache(hours):
        return None

    async def no_set(hours, data):
        return None

    async def fake_gather(now=None):
        return inputs

    async def fake_fc(lat, lon, days):
        # Naive Europe/Paris timestamps, as returned by the collector
        return [
            WeatherForecast(timestamp=sc.t0 + timedelta(hours=k + 1), point_id="m", point_name="m",
                            precipitation_mm=1.0, wind_speed_kmh=20.0, wind_direction_deg=270.0)
            for k in range(1, 80)
        ]

    async def fake_past(lat, lon, past_days=7):
        return dict(sc.rain_hourly)

    async def fake_tides(port, start, end):
        return []

    monkeypatch.setattr(forecast_mod, "_get_cached_forecast", no_cache)
    monkeypatch.setattr(forecast_mod, "_set_cached_forecast", no_set)
    monkeypatch.setattr(risk_v2, "gather_realtime_inputs", fake_gather)
    monkeypatch.setattr("app.services.collectors.weather.fetch_open_meteo_forecast", fake_fc)
    monkeypatch.setattr(forecast_mod, "_fetch_past_hourly_rain_utc", fake_past)
    monkeypatch.setattr(forecast_mod, "_fetch_tide_highs_from_influx", fake_tides)

    monkeypatch.setattr(forecast_mod, "_utcnow", lambda: sc.t0)

    result = await forecast_mod.get_risk_forecast_v2(hours=24)
    realtime = risk_v2.risk_from_inputs(inputs)
    assert result["forecasts"][0]["score"] == realtime.score
    assert result["model_version"] == risk_v2.MODEL_VERSION
    assert len(result["forecasts"]) == 25
    # Future rain keys are strictly after t0 (no collision with past hours)
    assert result["forecasts"][0]["rain_24h_mm"] == pytest.approx(sc.rainfall.last_24h_mm, abs=0.05)


async def test_risk_scheduler_starts_without_blocking(monkeypatch):
    from app.services import scheduler

    calls = []

    async def noop():
        calls.append(1)

    for name in ("collect_tide_job", "collect_river_job", "collect_weather_job", "collect_netatmo_job",
                 "collect_meteo_agricole_job", "collect_nowcast_rain_job", "compute_risk_job",
                 "collect_camera_job"):
        monkeypatch.setattr(scheduler, name, noop)

    await scheduler.start_scheduler()
    try:
        assert calls == []                      # nothing awaited inside start_scheduler
        jobs = {j.id: j for j in scheduler._scheduler.get_jobs()}
        assert "risk_scorer" in jobs
        assert jobs["risk_scorer"].trigger.interval == timedelta(hours=1)
        for job_id in ("tide_collector", "river_collector", "weather_collector"):
            assert jobs[job_id].next_run_time <= jobs["risk_scorer"].next_run_time
    finally:
        await scheduler.stop_scheduler()


async def test_risk_compute_risk_job_stores(monkeypatch):
    from app.services import scheduler

    sc = build_scenario()
    stored = []

    async def fake_compute(store=False):
        risk = risk_v2.risk_from_inputs(risk_v2.RealtimeInputs(
            timestamp=sc.t0, tide=sc.tide, rainfall=sc.rainfall, rainfall_raw=sc.rainfall,
            stations=sc.stations_data,
        ))
        if store:
            stored.append(risk)
        return risk, None

    monkeypatch.setattr(risk_v2, "compute_current_risk", fake_compute)
    await scheduler.compute_risk_job()
    assert len(stored) == 1
