"""Collectors: batched InfluxDB writes, daily ET0 backfill, concurrent episode context."""

import asyncio
from datetime import datetime, timedelta

import httpx
import pytest
import respx

from app.config import Settings
from app.models.schemas import RiverLevel
from app.services.collectors import hubeau, infoclimat, tide, weather

NOW = datetime(2026, 10, 25, 12, 0)


@pytest.fixture(autouse=True)
def _freeze(monkeypatch):
    monkeypatch.setattr(weather, "_utcnow", lambda: NOW)
    monkeypatch.setattr(tide, "_utcnow", lambda: NOW)


def _hours(start: datetime, n: int) -> list[str]:
    return [(start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(n)]


# --- weather ---------------------------------------------------------------------


@respx.mock
async def test_followups_historical_rain_single_batch(batch_writes, forbid_single_writes):
    times = _hours(datetime(2026, 10, 23), 48)
    respx.get(url__startswith=weather.OPEN_METEO_CURRENT).mock(return_value=httpx.Response(
        200, json={"hourly": {"time": times, "precipitation": [0.1] * 47 + [None]}}))
    stored = await weather.fetch_and_store_historical_rainfall(NOW - timedelta(days=2), NOW)
    assert stored == 47
    assert len(batch_writes) == 1
    p = batch_writes[0][0]
    assert p["measurement"] == "rainfall"
    assert p["tags"] == {"source": "open-meteo", "city": "Moulin-Lidoire"}
    assert p["fields"] == {"precipitation_mm_h": 0.1}


def test_followups_daily_et0_prefers_daily_sum_then_complete_hourly_days():
    times = _hours(datetime(2026, 10, 23), 60)  # 23rd, 24th complete, 25th partial
    hourly = [0.1] * 60
    daily = {"time": ["2026-10-23"], "et0_fao_evapotranspiration": [1.9]}
    out = weather.daily_et0_by_date(daily, times, hourly)
    assert out["2026-10-23"] == 1.9  # daily sum wins
    assert out["2026-10-24"] == pytest.approx(2.4)  # 24 x 0.1 mm/h
    assert "2026-10-25" not in out  # partial day: no fake low value
    assert weather.daily_et0_by_date({}, [], []) == {}


@respx.mock
async def test_followups_soil_backfill_stores_et0_in_mm_per_day(batch_writes, forbid_single_writes):
    times = _hours(datetime(2026, 10, 23), 48)
    hourly = {
        "time": times,
        "soil_moisture_0_to_7cm": [0.3] * 48,
        "soil_moisture_7_to_28cm": [0.32] * 48,
        "soil_moisture_28_to_100cm": [0.34] * 48,
        "soil_temperature_0_to_7cm": [9.0] * 48,
        "et0_fao_evapotranspiration": [0.05] * 24 + [0.1] * 24,
    }
    route = respx.get(url__startswith=weather.OPEN_METEO_CURRENT).mock(return_value=httpx.Response(
        200, json={"hourly": hourly,
                   "daily": {"time": ["2026-10-23", "2026-10-24"],
                             "et0_fao_evapotranspiration": [1.2, None]}}))
    stored = await weather.fetch_and_store_soil_historical(NOW - timedelta(days=2), NOW)
    assert stored == 48
    assert route.calls[0].request.url.params["daily"] == "et0_fao_evapotranspiration"
    assert len(batch_writes) == 1
    points = batch_writes[0]
    first, last = points[0]["fields"], points[-1]["fields"]
    assert first["et0_mm"] == 1.2 and first["et0_mm_h"] == 0.05  # daily value (API)
    assert last["et0_mm"] == pytest.approx(2.4) and last["et0_mm_h"] == 0.1  # sum of hours
    assert points[0]["measurement"] == "soil_timeline"


@respx.mock
async def test_followups_wind_backfill_single_batch(batch_writes, forbid_single_writes):
    times = _hours(datetime(2026, 10, 24), 24)
    respx.get(url__startswith=weather.OPEN_METEO_CURRENT).mock(return_value=httpx.Response(
        200, json={"hourly": {"time": times, "wind_speed_10m": [20.0] * 23 + [None],
                              "wind_direction_10m": [270.0] * 24}}))
    assert await weather.fetch_and_store_wind_historical(NOW - timedelta(days=1), NOW) == 23
    assert len(batch_writes) == 1
    assert batch_writes[0][0]["fields"]["westerly_kmh"] == 20.0


async def test_followups_soil_wind_cache_miss_single_batch(monkeypatch, batch_writes, forbid_single_writes):
    from app.services import influx_client

    async def no_cache(*args, **kwargs):
        return None

    async def fake_fetch(lat, lon):
        return {"soil_moisture": 0.3, "moisture_deep": 0.41, "soil_temperature_0cm": 8.0,
                "et0_mm": 1.1, "precip_probability": 40, "soil_saturation_factor": 1.2,
                "wind_speed_kmh": 10.0, "wind_direction_deg": 250.0, "westerly_wind_kmh": 9.0}

    monkeypatch.setattr(influx_client, "query_latest_point", no_cache)
    monkeypatch.setattr(weather, "fetch_soil_and_wind", fake_fetch)
    data = await weather.get_soil_wind_cached()
    assert data["et0_mm"] == 1.1
    assert len(batch_writes) == 1
    assert [p["measurement"] for p in batch_writes[0]] == ["soil_moisture", "wind", "soil_agricultural"]
    assert all(p["timestamp"] == NOW for p in batch_writes[0])


# --- tide -----------------------------------------------------------------------------


@respx.mock
async def test_followups_tide_range_batches_hourly_and_events(batch_writes, forbid_single_writes):
    times = _hours(datetime(2026, 10, 24), 16)
    levels = [0.83, 1.12, 1.07, 0.65, -0.06, -0.92, -1.69, -2.14,
              -2.11, -1.56, -0.63, 0.37, 1.12, 1.53, 1.55, 1.18]
    respx.get(url__startswith=tide.OPEN_METEO_MARINE_URL).mock(return_value=httpx.Response(
        200, json={"hourly": {"time": times, "sea_level_height_msl": levels}}))
    stored = await tide.fetch_and_store_tides_range(NOW - timedelta(days=1), NOW)
    assert stored == 16
    # one write for the hourly heights (event hours excepted: no duplicate
    # timestamp), one for the PM/BM events
    assert [len(b) for b in batch_writes] == [13, 3]
    events = batch_writes[1]
    assert {p["tags"]["tide_type"] for p in events} == {"PM", "BM"}
    assert any(p["fields"].get("coefficient") == 74 for p in events)


# --- infoclimat ----------------------------------------------------------------------


async def test_followups_infoclimat_backfill_batches_per_station(monkeypatch, batch_writes, forbid_single_writes):
    settings = Settings(_env_file=None, infoclimat_api_key="k")
    monkeypatch.setattr(infoclimat, "get_settings", lambda: settings)

    async def fake_near(lat, lon, radius_km=30):
        return [{"id": "000AB", "name": "Station A"}, {"id": "000CD", "name": "Station B"}]

    async def fake_obs(station_ids, start, end):
        return {"hourly": {
            "000AB": [{"dh_utc": "2026-10-24 10:00:00", "pluie_1h": "0.4"},
                      {"dh_utc": "2026-10-24 11:00:00", "pluie_1h": None},
                      {"dh_utc": "bad", "pluie_1h": "1"}],
            "000CD": [{"dh_utc": "2026-10-24 10:00:00", "pluie_1h": "0"},
                      {"dh_utc": "2026-10-24 11:00:00", "pluie_1h": "1.2"}],
        }}

    monkeypatch.setattr(infoclimat, "find_stations_near", fake_near)
    monkeypatch.setattr(infoclimat, "fetch_station_observations", fake_obs)
    total = await infoclimat.fetch_and_store_infoclimat_historical_range(NOW - timedelta(days=2), NOW)
    assert total == 3
    assert [len(b) for b in batch_writes] == [1, 2]
    assert batch_writes[1][1]["tags"] == {"source": "infoclimat", "city": "Station B"}
    assert batch_writes[1][1]["fields"]["precipitation_mm_h"] == 1.2


# --- hubeau episode context -----------------------------------------------------------


async def test_followups_episode_context_is_concurrent_and_bounded(monkeypatch):
    from app.config import get_settings

    station_ids = [s["id"] for s in get_settings().hubeau_stations]
    state = {"active": 0, "max": 0}

    async def fake_hydro(station_id, start, end):
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        await asyncio.sleep(0.01)
        state["active"] -= 1
        if station_id == station_ids[0]:
            return []  # forces the obs_elab fallback
        return [RiverLevel(timestamp=start, station_id=station_id, station_name="x",
                           river="y", height_m=3.0)]

    async def fake_elab(station_id, start, end, granularity="daily_max"):
        assert granularity == "daily_max"
        return [RiverLevel(timestamp=end, station_id=station_id, station_name="x",
                           river="y", height_m=4.0)]

    monkeypatch.setattr(hubeau, "fetch_hydroportail_data", fake_hydro)
    monkeypatch.setattr(hubeau, "fetch_historical_data", fake_elab)

    result = await hubeau.fetch_episode_context(datetime(2024, 2, 10, 12, 0))
    assert list(result) == station_ids  # configured order kept
    assert result[station_ids[0]][0].height_m == 4.0
    assert 1 < state["max"] <= hubeau.MAX_CONCURRENT_REQUESTS


async def test_followups_episode_context_recent_uses_realtime_window(monkeypatch):
    now = datetime(2026, 10, 25, 12, 0)
    monkeypatch.setattr(hubeau, "_utcnow", lambda: now)
    calls = []

    async def fake_tr(station_id, hours_back=48, client=None):
        calls.append(hours_back)
        return [RiverLevel(timestamp=now - timedelta(hours=h), station_id=station_id,
                           station_name="x", river="y", height_m=3.0) for h in (100, 50, 1)]

    async def forbidden(*args, **kwargs):
        raise AssertionError("historical source used for a recent episode")

    monkeypatch.setattr(hubeau, "fetch_realtime_observations", fake_tr)
    monkeypatch.setattr(hubeau, "fetch_hydroportail_data", forbidden)

    episode = now - timedelta(hours=24)
    result = await hubeau.fetch_episode_context(episode, hours_before=72, hours_after=24)
    assert set(calls) == {96}
    # points outside [episode - 72h, episode + 24h] are dropped
    assert all(len(levels) == 2 for levels in result.values())
