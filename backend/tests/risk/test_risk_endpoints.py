"""TimeMachine and simulation endpoints: no writes, honest inputs."""

from datetime import timedelta, timezone

import pytest
from fastapi import HTTPException

from app.api import realtime as realtime_api
from app.api import risk as risk_api
from app.services import influx_client, risk_core, risk_v2

from risk_scenario import build_scenario


def _snapshot(sc):
    river = {
        sid: [
            {"timestamp": ts.replace(tzinfo=timezone.utc).isoformat(), "height_m": h,
             "station_name": sid, "river": "x"}
            for ts, h in pts if abs((ts - sc.t0).total_seconds()) <= 6 * 3600
        ]
        for sid, pts in sc.station_points.items()
    }
    tides = []
    for r in sc.tide_rows:
        ts = r["timestamp"].replace(tzinfo=timezone.utc).isoformat()
        if r["height_m"] is not None:
            tides.append({"timestamp": ts, "value": r["height_m"], "field": "height_m",
                          "port": "BORDEAUX", "tide_type": r["tide_type"]})
        if r["coefficient"] is not None:
            tides.append({"timestamp": ts, "value": r["coefficient"], "field": "coefficient",
                          "port": "BORDEAUX", "tide_type": r["tide_type"]})
    return {"river_levels": river, "rainfall": [], "tides": tides, "risk_scores": []}


@pytest.fixture
def timemachine(monkeypatch):
    sc = build_scenario()
    writes = []

    async def fake_snapshot(target_time, window_hours=1):
        return _snapshot(sc)

    async def fake_rainfall(start, end, source=None):
        return [
            {"timestamp": k.replace(tzinfo=timezone.utc), "precipitation_mm_h": v,
             "source": "open-meteo", "city": "Moulin"}
            for k, v in sc.rain_hourly.items() if start <= k < end
        ]

    async def forbidden(*args, **kwargs):
        writes.append((args, kwargs))
        return True

    monkeypatch.setattr(realtime_api, "query_historical_snapshot", fake_snapshot)
    monkeypatch.setattr(realtime_api, "query_rainfall", fake_rainfall)
    monkeypatch.setattr(realtime_api, "write_river_levels_batch", forbidden)
    monkeypatch.setattr(realtime_api, "write_rainfall", forbidden)
    monkeypatch.setattr(influx_client, "write_risk_score", forbidden)
    monkeypatch.setattr(risk_v2, "store_risk_score", forbidden)
    return sc, writes


async def test_risk_timemachine_does_not_write(timemachine):
    sc, writes = timemachine
    data = await realtime_api.get_realtime_data_at(sc.t0.isoformat())
    assert writes == []
    assert data.risk.model_version == "3.3"
    assert data.timestamp == sc.t0
    assert data.risk.timestamp == sc.t0


async def test_risk_timemachine_matches_backfill_features(timemachine):
    sc, _ = timemachine
    data = await realtime_api.get_realtime_data_at(sc.t0.isoformat() + "Z")

    from app.config import get_settings
    settings = get_settings()
    configs = [risk_core.StationConfig.from_settings_dict(c) for c in settings.hubeau_stations]
    highs, coefs = risk_v2.tide_inputs_from_records(sc.tide_rows)
    feats = risk_core.build_historical_features(
        sc.t0, configs, sc.station_points, settings.hubeau_primary_station, sc.rain_hourly,
        risk_core.tide_at(sc.t0, highs, coefficient_records=coefs),
    )
    assert data.risk.score == risk_core.compute_risk(feats).score
    assert data.rainfall.last_24h_mm == pytest.approx(sc.rainfall.last_24h_mm, abs=0.05)
    assert data.rainfall.last_7d_mm == pytest.approx(sc.rainfall.last_7d_mm, abs=0.05)
    # Libourne shown but excluded from the river score
    libourne = next(s for s in data.stations.stations if s.station_id == "P577001001")
    assert libourne.weight == 0
    assert all(d.station_id != "P577001001" or d.weight == 0 for d in data.risk.factors.stations_detail)


async def test_risk_timemachine_invalid_timestamp_is_400(timemachine):
    with pytest.raises(HTTPException) as exc:
        await realtime_api.get_realtime_data_at("not-a-date")
    assert exc.value.status_code == 400


async def test_risk_simulate_uses_every_input_and_never_stores(monkeypatch):
    writes = []

    async def forbidden(*a, **k):
        writes.append(1)
        return True

    monkeypatch.setattr(influx_client, "write_risk_score", forbidden)
    monkeypatch.setattr(risk_v2, "store_risk_score", forbidden)

    base = dict(rain_24h_mm=20, rain_72h_mm=40, rain_7d_mm=0, dordogne_level_m=3.5,
                tide_coefficient=80, hours_to_high_tide=3, westerly_wind_kmh=0,
                soil_saturation=1.0, rate_cm_per_hour=2, n_rising_stations=2)
    ref = (await risk_api.simulate_risk(**base))["risk"]
    for key, value in (("rain_7d_mm", 140), ("westerly_wind_kmh", 90), ("soil_saturation", 1.5)):
        other = (await risk_api.simulate_risk(**{**base, key: value}))["risk"]
        assert other.score > ref.score, key
    out = await risk_api.simulate_risk(**base)
    assert out["model_version"] == "v3.3"
    assert out["risk"].factors.unavailable_factors == ["video"]
    assert writes == []


async def test_risk_calculate_v2_does_not_store_by_default(monkeypatch):
    sc = build_scenario()
    writes = []

    async def spy(risk):
        writes.append(risk)
        return True

    monkeypatch.setattr(risk_v2, "store_risk_score", spy)
    risk = await risk_v2.calculate_risk_score_v2(sc.stations_data, sc.tide, sc.rainfall, timestamp=sc.t0)
    assert writes == []
    await risk_v2.calculate_risk_score_v2(sc.stations_data, sc.tide, sc.rainfall, timestamp=sc.t0, store=True)
    assert len(writes) == 1 and writes[0].score == risk.score


def test_risk_realtime_drops_stale_station_levels():
    sc = build_scenario()
    stale = sc.stations_data.model_copy(deep=True)
    pessac = next(s for s in stale.stations if s.station_id == "P555001001")
    pessac.current_level.timestamp = sc.t0 - timedelta(hours=5)
    f = risk_v2.build_realtime_features(stale, sc.tide, sc.rainfall, sc.t0)
    assert "P555001001" not in {s.station_id for s in f.stations}
    bd = risk_core.compute_risk(f)
    assert bd.primary_station_id == "P726151001"  # Abzac: next highest weight, never Libourne


def test_risk_fuse_rainfall_keeps_windows_monotonic():
    from app.models.schemas import RainfallSummary

    rf = RainfallSummary(last_hour_mm=0.5, last_24h_mm=5.0, last_72h_mm=8.0, last_7d_mm=9.0)
    fused = risk_v2.fuse_rainfall(rf, {"available": True, "rain_24h_avg_mm": 20.0, "rain_1h_avg_mm": 2.0}, {})
    assert fused.last_24h_mm == 20.0
    assert fused.last_72h_mm == 20.0
    assert fused.last_7d_mm == 20.0
    assert fused.last_hour_mm == 2.0


def test_risk_tide_mock_flag_makes_tide_unavailable():
    sc = build_scenario()
    assert risk_v2.tide_forecast_is_reliable(sc.tide)

    class Flagged(type(sc.tide)):
        is_estimated: bool = True

    flagged = Flagged(**sc.tide.model_dump())
    assert not risk_v2.tide_forecast_is_reliable(flagged)
    f = risk_v2.build_realtime_features(sc.stations_data, flagged, sc.rainfall, sc.t0)
    bd = risk_core.compute_risk(f)
    assert bd.tide_score == 0 and bd.tide_phase_score == 0 and not bd.tide_available
    risk = risk_v2.breakdown_to_risk_score(bd)
    assert risk.factors.tide_available is False
    assert "marée" in risk.description
