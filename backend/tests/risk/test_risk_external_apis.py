"""Request paths never await an optional external API (Infoclimat, Netatmo,
Meteo Agricole): they read the caches the scheduler jobs fill."""

import asyncio
import time

import pytest

from risk_scenario import build_scenario

from app.services import risk_v2
from app.services.cache import TTLCache
from app.services.collectors import hubeau, infoclimat, meteo_agricole, netatmo, tide, vigicrues, weather


@pytest.fixture
def core_sources(monkeypatch):
    """Fast fakes for the cache-through sources (tide, Open-Meteo, Hub'Eau)."""
    sc = build_scenario()

    async def get_tide():
        return sc.tide

    async def get_rain():
        return sc.rainfall

    async def get_stations():
        return sc.stations_data

    async def get_soil_wind():
        return {}

    monkeypatch.setattr(tide, "get_current_tide_data", get_tide)
    monkeypatch.setattr(weather, "get_rainfall_summary", get_rain)
    monkeypatch.setattr(hubeau, "get_multi_station_data", get_stations)
    monkeypatch.setattr(weather, "get_soil_wind_cached", get_soil_wind)
    return sc


@pytest.fixture
def hung_optional_apis(monkeypatch):
    """Every Infoclimat / Netatmo / Meteo Agricole fetch hangs; caches empty."""
    calls = []

    async def hang(*args, **kwargs):
        calls.append(1)
        await asyncio.sleep(30)

    for module, name in (
        (infoclimat, "get_basin_rainfall_infoclimat"),
        (infoclimat, "fetch_stations_list"),
        (infoclimat, "fetch_station_observations"),
        (netatmo, "get_basin_rainfall"),
        (netatmo, "fetch_public_stations_data"),
        (meteo_agricole, "get_meteo_agricole_cached"),
        (meteo_agricole, "fetch_meteo_agricole"),
        (meteo_agricole, "get_nowcast_rain_cached"),
        (meteo_agricole, "fetch_nowcast_rain"),
    ):
        monkeypatch.setattr(module, name, hang)
    monkeypatch.setattr(infoclimat, "_latest", TTLCache(ttl_seconds=3600, max_entries=1))
    monkeypatch.setattr(netatmo, "_latest", TTLCache(ttl_seconds=3600, max_entries=1))
    for name in ("_cache", "_cache_expiry", "_nowcast_cache", "_nowcast_cache_expiry"):
        monkeypatch.setattr(meteo_agricole, name, None)
    return calls


async def test_gather_realtime_inputs_never_awaits_optional_apis(core_sources, hung_optional_apis):
    t = time.monotonic()
    inputs = await asyncio.wait_for(risk_v2.gather_realtime_inputs(core_sources.t0), timeout=5)
    assert time.monotonic() - t < 1.0
    assert hung_optional_apis == []

    # Unavailable optional sources: same outcome as a failed fetch
    assert inputs.infoclimat["available"] is False
    assert inputs.netatmo["available"] is False
    assert inputs.meteo_agricole is None
    assert inputs.rainfall.last_24h_mm == core_sources.rainfall.last_24h_mm


async def test_realtime_endpoint_returns_quickly_when_optional_apis_hang(
    core_sources, hung_optional_apis, monkeypatch,
):
    from app.api import realtime

    async def river():
        return vigicrues._unavailable_forecast("P577001001", core_sources.t0)

    monkeypatch.setattr(realtime, "get_current_river_level", river)

    t = time.monotonic()
    data = await asyncio.wait_for(realtime.get_realtime_data(), timeout=5)
    assert time.monotonic() - t < 1.0
    assert hung_optional_apis == []
    assert data.netatmo_available is False
    assert data.nowcast_rain is None


async def test_infoclimat_job_result_is_fused_by_request_path(core_sources, monkeypatch):
    """Scheduler job -> cache -> realtime inputs: Infoclimat stays in the max merge."""
    from app.config import get_settings
    from app.services import scheduler

    monkeypatch.setattr(get_settings(), "infoclimat_api_key", "k")
    monkeypatch.setattr(infoclimat, "_latest", TTLCache(ttl_seconds=3600, max_entries=1))
    monkeypatch.setattr(infoclimat, "_observations_cache", None)
    monkeypatch.setattr(infoclimat, "_cache_expiry", None)
    monkeypatch.setattr(netatmo, "_latest", TTLCache(ttl_seconds=3600, max_entries=1))
    gauge_24h = core_sources.rainfall.last_24h_mm + 20.0

    async def fake_find(lat, lon, radius_km):
        return [{"id": "ST1", "name": "Station 1", "distance_km": 5.0}]

    async def fake_obs(ids, start, end):
        return {"hourly": {"ST1": [{"dh_utc": "2026-10-25 12:00:00", "pluie_24h": str(gauge_24h)}]}}

    monkeypatch.setattr(infoclimat, "find_stations_near", fake_find)
    monkeypatch.setattr(infoclimat, "fetch_station_observations", fake_obs)
    await scheduler.collect_infoclimat_job()

    async def forbidden(*args, **kwargs):
        raise AssertionError("request path called Infoclimat")

    monkeypatch.setattr(infoclimat, "fetch_station_observations", forbidden)
    monkeypatch.setattr(infoclimat, "get_basin_rainfall_infoclimat", forbidden)

    inputs = await risk_v2.gather_realtime_inputs(core_sources.t0)
    assert inputs.infoclimat["available"] is True
    assert inputs.rainfall.last_24h_mm == pytest.approx(round(gauge_24h, 1))
    assert inputs.rainfall_raw.last_24h_mm == core_sources.rainfall.last_24h_mm
