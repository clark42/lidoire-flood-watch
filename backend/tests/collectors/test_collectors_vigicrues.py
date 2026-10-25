"""Vigicrues collector: payload parsing (units), redirects, no mock fallback."""

from datetime import datetime, timedelta

import httpx
import pytest
import respx

from app.services.collectors import vigicrues

OBS_PAYLOAD = {
    "Serie": {
        "CdStationHydro": "P577001001",
        "LbStationHydro": "Libourne",
        "GrdSerie": "H",
        "ObssHydro": [
            {"DtObsHydro": "2026-10-25T21:00:00+00:00", "ResObsHydro": 1.24},
            {"DtObsHydro": "2026-10-25T21:30:00+00:00", "ResObsHydro": 1.30},
            {"DtObsHydro": "2026-10-25T22:00:00+00:00", "ResObsHydro": 1.42},
        ],
    }
}

FORECAST_PAYLOAD = {
    "Simul": {
        "CdStationHydro": "P577001001",
        "Prevs": [
            {"DtPrev": 1792997400000, "ResMinPrev": 4.46, "ResMoyPrev": 4.64, "ResMaxPrev": 4.82},
        ],
    },
    "VersionFlux": "Beta 0.4f",
}


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    monkeypatch.setattr(vigicrues, "_river_cache", {})
    monkeypatch.setattr(vigicrues, "_cache_expiry", None)


def test_parse_observations_keeps_metres_and_utc():
    levels = vigicrues.parse_observations(OBS_PAYLOAD, "P577001001")
    assert [lv.height_m for lv in levels] == [1.24, 1.30, 1.42]  # no /1000
    assert levels[0].timestamp == datetime(2026, 10, 25, 21, 0)
    assert levels[0].timestamp.tzinfo is None
    assert levels[0].station_name == "Libourne"


def test_parse_observations_skips_invalid():
    payload = {"Serie": {"ObssHydro": [
        {"DtObsHydro": "2026-10-25T21:00:00+00:00", "ResObsHydro": None},
        {"DtObsHydro": "garbage", "ResObsHydro": 1.0},
        {"ResObsHydro": 1.0},
        {"DtObsHydro": "2026-10-25T22:00:00Z", "ResObsHydro": "1.5"},
    ]}}
    levels = vigicrues.parse_observations(payload, "P577001001")
    assert len(levels) == 1 and levels[0].height_m == 1.5


def test_parse_forecast_epoch_ms():
    prevs = vigicrues.parse_forecast(FORECAST_PAYLOAD)
    assert prevs == [{
        "timestamp": "2026-10-26T06:50:00Z",
        "min_m": 4.46, "mean_m": 4.64, "max_m": 4.82,
    }]
    assert vigicrues.parse_forecast({}) == []


@respx.mock
async def test_fetch_follows_redirect():
    respx.get(url__startswith="https://www.vigicrues.gouv.fr/services/observations.json/index.php").mock(
        return_value=httpx.Response(302, headers={"Location": "https://www.vigicrues.gouv.fr/moved"})
    )
    respx.get("https://www.vigicrues.gouv.fr/moved").mock(return_value=httpx.Response(200, json=OBS_PAYLOAD))
    levels = await vigicrues.fetch_station_data("P577001001")
    assert len(levels) == 3


@respx.mock
async def test_current_level_live(monkeypatch):
    monkeypatch.setattr(vigicrues, "_utcnow", lambda: datetime(2026, 10, 25, 22, 10))
    respx.get(url__startswith=vigicrues.VIGICRUES_OBSERVATIONS_URL).mock(
        return_value=httpx.Response(200, json=OBS_PAYLOAD))
    respx.get(url__startswith=vigicrues.VIGICRUES_FORECAST_URL).mock(
        return_value=httpx.Response(200, json=FORECAST_PAYLOAD))

    result = await vigicrues.get_current_river_level()
    assert result.available is True
    assert result.source == "vigicrues"
    assert result.current.height_m == 1.42
    assert result.tendency.value == "hausse"  # +18 cm in 1h
    assert len(result.forecast_scenarios) == 1


@respx.mock
async def test_current_level_unavailable_no_mock(monkeypatch):
    monkeypatch.setattr(vigicrues, "_utcnow", lambda: datetime(2026, 10, 25, 22, 10))
    respx.get(url__startswith=vigicrues.VIGICRUES_OBSERVATIONS_URL).mock(
        return_value=httpx.Response(503))

    result = await vigicrues.get_current_river_level()
    assert result.available is False
    assert result.source == "unavailable"
    assert result.current.height_m == 0.0
    assert result.forecast_scenarios == []


@respx.mock
async def test_current_level_stale_fallback(monkeypatch):
    t0 = datetime(2026, 10, 25, 22, 10)
    monkeypatch.setattr(vigicrues, "_utcnow", lambda: t0)
    obs_route = respx.get(url__startswith=vigicrues.VIGICRUES_OBSERVATIONS_URL)
    obs_route.mock(return_value=httpx.Response(200, json=OBS_PAYLOAD))
    respx.get(url__startswith=vigicrues.VIGICRUES_FORECAST_URL).mock(
        return_value=httpx.Response(200, json=FORECAST_PAYLOAD))
    await vigicrues.get_current_river_level()

    # 1h later: cache expired, Vigicrues down -> stale data served
    obs_route.mock(return_value=httpx.Response(500))
    monkeypatch.setattr(vigicrues, "_utcnow", lambda: t0 + timedelta(hours=1))
    result = await vigicrues.get_current_river_level()
    assert result.available is True and result.source == "vigicrues-stale"

    # 7h later: too old -> unavailable
    monkeypatch.setattr(vigicrues, "_utcnow", lambda: t0 + timedelta(hours=7))
    result = await vigicrues.get_current_river_level()
    assert result.available is False
