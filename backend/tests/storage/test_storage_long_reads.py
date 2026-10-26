"""Long reads: timeline downsampled server-side, station-extremes bounded and cached."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import episodes as episodes_api
from app.api import timeline
from app.models.schemas import RiverLevel
from app.services import episode_analyzer_v2, influx_client

T = datetime(2025, 1, 1)


@pytest.fixture
def app_client():
    app = FastAPI()
    app.include_router(timeline.router, prefix="/api/timeline")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.parametrize("days, requested, expected", [
    (2, "15m", "15m"),
    (31, "15m", "15m"),     # 2976 points
    (32, "15m", "1h"),
    (125, "1h", "1h"),      # 3000 points
    (126, "1h", "6h"),
    (400, "15m", "6h"),     # was ~35k points per station
    (400, "1d", "1d"),      # never finer than requested
])
def test_storage_effective_resolution(days, requested, expected):
    assert timeline.effective_resolution(T, T + timedelta(days=days), requested) == expected
    hours = days * 24 / timeline.RESOLUTION_HOURS[expected]
    assert hours <= timeline.MAX_POINTS_PER_SERIES


def test_storage_thin_running_total_keeps_window_end_value():
    pts = [{"timestamp": (T + timedelta(hours=h)).replace(tzinfo=timezone.utc), "value": float(h)} for h in range(1, 14)]
    thinned = timeline.thin_running_total(pts, "6h")
    assert [(p["timestamp"].hour, p["value"]) for p in thinned] == [(6, 6.0), (12, 12.0), (18, 13.0)]
    assert timeline.thin_running_total(pts, "1h") == pts


def test_storage_level_queries_use_max_when_asked():
    q = influx_client.build_river_levels_multi_station_query(T, T + timedelta(days=1), ["P555001001"], "6h", "max")
    assert "fn: max" in q
    assert "fn: mean" in influx_client.build_river_levels_multi_station_query(T, T + timedelta(days=1))
    assert "fn: max" in influx_client.build_tides_timeline_query(T, T + timedelta(days=1), "LIDOIRE", "6h", "max")
    with pytest.raises(influx_client.InvalidQueryParameter):
        influx_client.build_river_levels_multi_station_query(T, T + timedelta(days=1), None, "6h", "drop()")


async def test_storage_unified_long_range_is_downsampled(app_client, monkeypatch):
    seen = {}

    async def no_backfill(*a, **k):
        return None

    async def fake_river(start, end, station_ids=None, resolution="1h", agg_fn="mean"):
        seen["river"] = (resolution, agg_fn)
        return {"P555001001": [{"timestamp": T, "value": 4.2}]}

    async def fake_tides(start, end, port, resolution="1h", agg_fn="mean"):
        seen["tide"] = (resolution, agg_fn)
        return [{"timestamp": T, "value": 2.0}]

    async def fake_risk(start, end, resolution="1h", model_version=None, agg_fn="mean"):
        seen["risk"] = (resolution, agg_fn)
        return [{"timestamp": T, "value": 40.0}]

    async def fake_rain(start, end, sources=None, cities=None, resolution="1h", agg_fn="mean"):
        seen.setdefault("rain", []).append((resolution, agg_fn))
        return {}

    for name in ("_ensure_river_data", "_ensure_tide_data", "_ensure_risk_data", "_ensure_infoclimat_rainfall"):
        monkeypatch.setattr(timeline, name, no_backfill)
    monkeypatch.setattr(timeline, "query_river_levels_multi_station", fake_river)
    monkeypatch.setattr(timeline, "query_tides_timeline", fake_tides)
    monkeypatch.setattr(timeline, "query_risk_scores_timeline", fake_risk)
    monkeypatch.setattr(timeline, "query_rainfall_by_station", fake_rain)

    async with app_client as client:
        resp = await client.get("/api/timeline/unified", params={
            "start": "2024-09-01T00:00:00", "end": "2025-10-01T00:00:00",
            "river_stations": "P555001001", "include_tide": "true", "include_risk": "true",
            "resolution": "15m",
        })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["resolution"] == "6h" and body["requested_resolution"] == "15m"
    assert seen["river"] == seen["tide"] == seen["risk"] == ("6h", "max")
    assert ("6h", "sum") in seen["rain"]  # Infoclimat rain summed per window


async def test_storage_unified_short_range_keeps_requested_resolution(app_client, monkeypatch):
    async def no_backfill(*a, **k):
        return None

    seen = {}

    async def fake_river(start, end, station_ids=None, resolution="1h", agg_fn="mean"):
        seen["river"] = (resolution, agg_fn)
        return {}

    monkeypatch.setattr(timeline, "_ensure_river_data", no_backfill)
    monkeypatch.setattr(timeline, "query_river_levels_multi_station", fake_river)
    async with app_client as client:
        resp = await client.get("/api/timeline/unified", params={
            "start": "2025-01-01T00:00:00", "end": "2025-01-03T00:00:00",
            "river_stations": "P555001001", "include_infoclimat": "false", "resolution": "15m",
        })
    assert resp.json()["resolution"] == resp.json()["requested_resolution"] == "15m"
    assert seen["river"] == ("15m", "mean")


# --- station-extremes ---------------------------------------------------------


@pytest.fixture
def episodes_client():
    app = FastAPI()
    app.include_router(episodes_api.router, prefix="/api/episodes")
    return TestClient(app)


@pytest.mark.parametrize("start, end", [
    ("2024-01-01T00:00:00", "2024-01-01T00:00:00"),
    ("2024-02-01T00:00:00", "2024-01-01T00:00:00"),
    ("2000-01-01T00:00:00", "2024-12-31T00:00:00"),
])
def test_storage_station_extremes_bounded(episodes_client, start, end):
    resp = episodes_client.get("/api/episodes/station-extremes", params={"start_date": start, "end_date": end})
    assert resp.status_code == 422


async def test_storage_station_extremes_cached_and_concurrent(monkeypatch):
    calls = []

    async def fake_hist(station_id, start, end, granularity="daily_max"):
        calls.append(station_id)
        return [RiverLevel(timestamp=T, station_id=station_id, station_name="x", river="y", height_m=3.0)]

    monkeypatch.setattr(episode_analyzer_v2, "fetch_historical_data", fake_hist)
    episode_analyzer_v2._station_extremes_cache.clear()
    first = await episode_analyzer_v2.get_station_max_levels(T, T + timedelta(days=365))
    assert len(first) == len(calls) == 7
    again = await episode_analyzer_v2.get_station_max_levels(T + timedelta(hours=3), T + timedelta(days=365, hours=1))
    assert again == first and len(calls) == 7  # same day pair: served from cache
    episode_analyzer_v2._station_extremes_cache.clear()
