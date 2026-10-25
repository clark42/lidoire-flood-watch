"""Netatmo collector: hourly-aligned rain writes, coverage, UTC, token refresh lock."""

import asyncio
import os
import stat
from datetime import datetime, timedelta

import httpx
import pytest
import respx

from app.services.collectors import netatmo


# --- hourly alignment -------------------------------------------------------

def test_hourly_slot_first_collection_after_hour():
    assert netatmo.hourly_slot(datetime(2026, 10, 25, 14, 7), None) == datetime(2026, 10, 25, 14, 0)


def test_hourly_slot_skips_when_already_written_or_too_late():
    slot = datetime(2026, 10, 25, 14, 0)
    assert netatmo.hourly_slot(datetime(2026, 10, 25, 14, 12), slot) is None
    assert netatmo.hourly_slot(datetime(2026, 10, 25, 14, 40), None) is None
    assert netatmo.hourly_slot(datetime(2026, 10, 25, 15, 2), slot) == datetime(2026, 10, 25, 15, 0)


@pytest.fixture
def influx_writes(monkeypatch):
    from app.services import influx_client

    points, hourly = [], []

    async def fake_write_points_batch(batch, chunk_size=5000):
        # One batched write per collection; split snapshots / hourly points
        for point in batch:
            d = influx_client.point_as_dict(point)
            if "precipitation_mm_h" in d["fields"]:
                hourly.append({
                    "station_city": d["tags"].get("city"),
                    "timestamp": d["timestamp"],
                    "precipitation_mm_h": d["fields"]["precipitation_mm_h"],
                })
            else:
                points.append(d)
        return len(batch)

    monkeypatch.setattr(influx_client, "write_points_batch", fake_write_points_batch)
    monkeypatch.setattr(netatmo, "_last_hourly_write", {})
    return points, hourly


async def test_store_writes_one_hourly_value_per_clock_hour(monkeypatch, influx_writes):
    points, hourly = influx_writes
    basin = {
        "rain_1h_avg_mm": 2.0,
        "rain_24h_avg_mm": 10.0,
        "lidoire_stations": [{"city": "Flaujagues", "rain_1h_mm": 3.0, "rain_24h_mm": 12.0,
                              "lat": 44.83, "lon": 0.03}],
        "stations": [],
    }
    # Four 15-min collections across 14h, then one at 15h
    for minute_offset in (5, 20, 35, 50, 65):
        now = datetime(2026, 10, 25, 14, 0) + timedelta(minutes=minute_offset)
        monkeypatch.setattr(netatmo, "_utcnow", lambda now=now: now)
        await netatmo.store_rainfall_to_influx(basin)

    # Snapshots every time (2 series x 5), never carrying precipitation_mm_h
    assert len(points) == 10
    assert all("precipitation_mm_h" not in p["fields"] for p in points)
    assert all("precipitation_mm_24h" in p["fields"] for p in points)

    # Hourly field: one per series per clock hour, on the hour
    flaujagues = [h for h in hourly if h["station_city"] == "Flaujagues"]
    assert [h["timestamp"] for h in flaujagues] == [datetime(2026, 10, 25, 14, 0), datetime(2026, 10, 25, 15, 0)]
    assert flaujagues[0]["precipitation_mm_h"] == 3.0
    assert len(hourly) == 4  # basin average + Flaujagues, 2 hours


# --- coverage (fallback detection by point count) ---------------------------

def test_coverage_counts_consecutive_chunks():
    now = datetime(2026, 10, 25, 12, 0)
    ts = [now - timedelta(hours=24 * k, minutes=30) for k in range(3)]
    assert netatmo.consecutive_daily_coverage(ts, now) == 3
    # gap at k=1 -> only first chunk counts
    ts = [now - timedelta(minutes=10), now - timedelta(hours=48)]
    assert netatmo.consecutive_daily_coverage(ts, now) == 1
    assert netatmo.consecutive_daily_coverage([], now) == 0


def test_dry_weather_complete_history_needs_no_fallback():
    # Seven daily samples, all 0 mm: previously 0 == 0 triggered the fallback
    now = datetime(2026, 10, 25, 12, 0)
    ts = [now - timedelta(hours=24 * k) for k in range(7)]
    assert netatmo.consecutive_daily_coverage(ts, now) == 7


# --- timezones ---------------------------------------------------------------

def test_utc_from_epoch_is_utc():
    assert netatmo._utc_from_epoch(1761400800) == datetime(2025, 10, 25, 14, 0)


def test_parse_station_rain_timestamp_utc():
    station = {
        "_id": "70:ee:50:00:00:01",
        "place": {"location": [0.03, 44.83], "city": "Flaujagues"},
        "measures": {"05:00:00:00:00:01": {"rain_60min": 1.2, "rain_24h": 5.0, "rain_live": 0,
                                           "rain_timeutc": 1761400800}},
    }
    parsed = netatmo._parse_station_data(station, 44.87, 0.0)
    assert parsed["rain"]["timestamp"] == datetime(2025, 10, 25, 14, 0)


# --- token refresh -----------------------------------------------------------

@pytest.fixture
def token_store(monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "netatmo_client_id", "cid")
    monkeypatch.setattr(settings, "netatmo_client_secret", "secret")

    store = {"tokens": {
        "access_token": "old-access",
        "refresh_token": "rt-1",
        "expires_at": (netatmo._utcnow() - timedelta(minutes=1)).isoformat(),
    }}

    def fake_get():
        return dict(store["tokens"])

    def fake_update(access_token, refresh_token, expires_in):
        store["tokens"] = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": (netatmo._utcnow() + timedelta(seconds=expires_in)).isoformat(),
        }

    monkeypatch.setattr(netatmo, "_get_stored_tokens", fake_get)
    monkeypatch.setattr(netatmo, "_update_stored_tokens", fake_update)
    monkeypatch.setattr(netatmo, "_token_lock", asyncio.Lock())
    return store


@respx.mock
async def test_concurrent_refresh_hits_netatmo_once(token_store):
    seen_refresh_tokens = []

    def handler(request):
        body = dict(x.split("=") for x in request.content.decode().split("&"))
        seen_refresh_tokens.append(body["refresh_token"])
        n = len(seen_refresh_tokens)
        return httpx.Response(200, json={"access_token": f"acc-{n}", "refresh_token": f"rt-{n + 1}",
                                         "expires_in": 10800})

    route = respx.post(netatmo.NETATMO_AUTH_URL).mock(side_effect=handler)

    tokens = await asyncio.gather(*[netatmo._get_access_token() for _ in range(8)])
    assert route.call_count == 1
    assert seen_refresh_tokens == ["rt-1"]
    assert set(tokens) == {"acc-1"}
    assert token_store["tokens"]["refresh_token"] == "rt-2"


@respx.mock
async def test_valid_token_no_refresh(token_store):
    token_store["tokens"]["expires_at"] = (netatmo._utcnow() + timedelta(hours=1)).isoformat()
    route = respx.post(netatmo.NETATMO_AUTH_URL).mock(return_value=httpx.Response(500))
    assert await netatmo._get_access_token() == "old-access"
    assert route.call_count == 0


# --- auth.py token file -----------------------------------------------------

def test_token_file_atomic_and_private(tmp_path, monkeypatch):
    from app.api import auth

    path = tmp_path / "data" / "netatmo_tokens.json"
    monkeypatch.setattr(auth, "TOKEN_STORAGE_PATH", path)
    auth.update_netatmo_tokens("a1", "r1", 3600)
    auth.update_netatmo_tokens("a2", "r2", 3600)

    assert auth.get_netatmo_tokens()["refresh_token"] == "r2"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    # no temp files left behind
    assert [p.name for p in path.parent.iterdir()] == ["netatmo_tokens.json"]
