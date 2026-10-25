"""Infoclimat collector: latest pluie_24h regardless of API ordering, no fake hourly zeros."""

from datetime import datetime

import pytest

from app.services.collectors import infoclimat


@pytest.fixture
def setup(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "infoclimat_api_key", "k")
    monkeypatch.setattr(infoclimat, "_observations_cache", None)
    monkeypatch.setattr(infoclimat, "_cache_expiry", None)

    async def fake_find(lat, lon, radius_km):
        return [{"id": "ST1", "name": "Station 1", "distance_km": 5.0}]

    monkeypatch.setattr(infoclimat, "find_stations_near", fake_find)


async def test_latest_pluie_24h_with_unsorted_observations(setup, monkeypatch):
    async def fake_obs(ids, start, end):
        # Unordered: the most recent observation (12:00) comes first
        return {"hourly": {"ST1": [
            {"dh_utc": "2026-10-25 12:00:00", "pluie_24h": "8.4"},
            {"dh_utc": "2026-10-25 10:00:00", "pluie_24h": "3.0"},
            {"dh_utc": "2026-10-25 11:00:00", "pluie_24h": "5.0"},
        ]}}

    monkeypatch.setattr(infoclimat, "fetch_station_observations", fake_obs)
    result = await infoclimat.get_basin_rainfall_infoclimat()
    assert result["available"] is True
    assert result["stations"][0]["rain_24h_mm"] == 8.4


async def test_store_does_not_write_fake_hourly(monkeypatch):
    from app.services import influx_client

    points = []

    async def fake_write_point(measurement, tags, fields, timestamp):
        points.append(fields)
        return True

    async def forbidden(**kwargs):
        raise AssertionError("write_rainfall would add precipitation_mm_h=0")

    monkeypatch.setattr(influx_client, "write_point", fake_write_point)
    monkeypatch.setattr(influx_client, "write_rainfall", forbidden)

    n = await infoclimat.store_rainfall_to_influx({
        "rain_24h_avg_mm": 4.0,
        "stations": [{"station_name": "Station 1", "rain_24h_mm": 4.0}],
    })
    assert n == 2
    assert all(set(f) == {"precipitation_mm_24h"} for f in points)
