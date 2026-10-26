"""/api/indicators, timeline and status contracts of the indicator series (no InfluxDB)."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from fastapi import FastAPI

from app.api import indicators as indicators_api
from app.api import status, timeline
from app.services import indicators, influx_client

NOW = datetime(2026, 2, 12, 12, 0)


@pytest.fixture(autouse=True)
def _clear_caches():
    indicators._history_cache.clear()
    indicators._payload_cache.clear()
    status._sources_cache.clear()
    yield
    indicators._history_cache.clear()
    indicators._payload_cache.clear()
    status._sources_cache.clear()


def _recent(values, step_min=60, end=NOW):
    n = len(values)
    return [{"timestamp": (end - timedelta(minutes=step_min * (n - 1 - i))).replace(tzinfo=timezone.utc),
             "value": v} for i, v in enumerate(values)]


@pytest.fixture
def fake_store(monkeypatch):
    """Stored series per indicator key: recent raw points and daily history."""
    history = {
        "EYRAUD": [0.1] * 300 + [2.0] * 50 + [15.3, 18.8, 21.3],
        "MOULIETS": [5.8] * 300 + [7.0, 7.2],
        "BORDEAUX": [5.0] * 200 + [5.9],
    }
    recent = {
        "EYRAUD": _recent([14.0, 18.0, 22.0, 26.0], step_min=60),   # sharp rise
        "MOULIETS": _recent([7.30, 7.47], step_min=24 * 60),
        "BORDEAUX": _recent([4.0, 5.95, 3.0], step_min=180),
    }
    calls = []

    async def fake_query(spec, start, end, every=None, fn="mean"):
        calls.append((spec["key"], every, fn))
        if every == "1d":
            return [{"timestamp": NOW, "value": v} for v in history.get(spec["key"], [])]
        return recent.get(spec["key"], [])

    monkeypatch.setattr(indicators, "query_series", fake_query)
    monkeypatch.setattr(indicators, "_utcnow", lambda: NOW)
    return calls


async def test_indicators_payload_by_mechanism(fake_store):
    body = await indicators.get_indicators(now=NOW)
    assert body["model_input"] is False
    assert body["timestamp"] == "2026-02-12T12:00:00Z"
    assert set(body) >= {"runoff", "dordogne", "groundwater", "tide_observed"}
    assert [e["key"] for e in body["runoff"]] == ["EYRAUD", "CAUDEAU"]
    assert [e["key"] for e in body["dordogne"]] == ["PESSAC", "LAMONZIE", "ARGENTAT"]

    eyraud = body["runoff"][0]
    assert eyraud["available"] is True and eyraud["stale"] is False
    assert eyraud["last_timestamp"] == "2026-02-12T12:00:00Z"
    assert eyraud["q_m3s"] == 26.0
    assert eyraud["q_mean_24h_m3s"] == 20.0
    assert eyraud["trend"] == "rising" and eyraud["trend_m3s_per_h"] == pytest.approx(4.0)
    assert eyraud["percentile_label"] == "p99"
    assert eyraud["threshold"]["exceeded"] == ["watch", "alert"]
    assert eyraud["history_days"] == 353

    caudeau = body["runoff"][1]
    assert caudeau["available"] is False and caudeau["q_m3s"] is None and caudeau["percentile"] is None

    gw = body["groundwater"][0]
    assert gw["level_m_ngf"] == 7.47 and gw["change_24h_m"] == pytest.approx(0.17)
    assert gw["percentile"] == 100.0 and gw["threshold"]["exceeded"] == ["watch"]

    tide = next(t for t in body["tide_observed"] if t["key"] == "BORDEAUX")
    assert tide["max_24h_m"] == 5.95 and tide["height_m"] == 3.0
    assert tide["percentile"] == 100.0


async def test_indicators_history_uses_daily_aggregates(fake_store):
    await indicators.get_indicators(now=NOW)
    daily = {(k, fn) for k, every, fn in fake_store if every == "1d"}
    assert ("EYRAUD", "mean") in daily and ("MOULIETS", "mean") in daily and ("BORDEAUX", "max") in daily
    n = len(fake_store)
    await indicators.get_indicators(now=NOW)
    # non-empty history cached (6 h); an empty one is retried (series not backfilled yet)
    again = {k for k, every, _ in fake_store[n:] if every == "1d"}
    assert again.isdisjoint({"EYRAUD", "MOULIETS", "BORDEAUX"}) and "CAUDEAU" in again


@respx.mock
async def test_indicators_route_reads_storage_only(fake_store):
    # respx: any request to a real host would fail (ASGI transport is not mocked)
    app = FastAPI()
    app.include_router(indicators_api.router, prefix="/api/indicators")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/indicators")
    assert resp.status_code == 200
    assert resp.json()["runoff"][0]["key"] == "EYRAUD"
    assert respx.calls.call_count == 0


def test_series_query_is_exact_and_validated():
    spec = indicators.series_catalogue()["flow:EYRAUD"]
    q = indicators.build_series_query(spec, NOW - timedelta(days=1), NOW, every="1h")
    assert 'r._measurement == "river_flow"' in q
    assert 'r._field == "q_m3s"' in q
    assert 'r.key == "EYRAUD"' in q
    assert "aggregateWindow(every: 1h, fn: mean" in q
    bad = dict(spec, key='X" or true')
    with pytest.raises(influx_client.InvalidQueryParameter):
        indicators.build_series_query(bad, NOW - timedelta(days=1), NOW)
    with pytest.raises(influx_client.InvalidQueryParameter):
        indicators.build_series_query(spec, NOW - timedelta(days=1), NOW, every="7w")


def test_catalogue_ids():
    ids = set(indicators.series_catalogue())
    assert {"flow:EYRAUD", "flow:CAUDEAU", "flow:PESSAC", "flow:ARGENTAT", "flow:LAMONZIE",
            "gw:MOULIETS", "tide_obs:BORDEAUX", "tide_obs:BEC_AMBES"} == ids


# --- timeline -------------------------------------------------------------------------------

@pytest.fixture
def timeline_client():
    app = FastAPI()
    app.include_router(timeline.router, prefix="/api/timeline")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_timeline_sources_list_indicators(timeline_client):
    async with timeline_client as client:
        body = (await client.get("/api/timeline/sources")).json()
    by_id = {s["id"]: s for s in body["indicator_series"]}
    assert by_id["flow:EYRAUD"]["type"] == "flow_tributary"
    assert by_id["flow:PESSAC"]["type"] == "flow_dordogne"
    assert by_id["gw:MOULIETS"]["unit"] == "m NGF"
    assert by_id["tide_obs:BORDEAUX"]["kind"] == "tide_observed"
    assert by_id["flow:EYRAUD"]["distance_km"] > 0
    # river stations untouched: the indicators are not Hub'Eau level stations
    assert "P540401002" not in {s["id"] for s in body["river_stations"]}


async def test_timeline_unified_indicator_series(timeline_client, monkeypatch):
    seen = []

    async def fake_query(spec, start, end, every=None, fn="mean"):
        seen.append((spec["key"], every, fn))
        return [{"timestamp": datetime(2024, 2, 26, 6), "value": 23.0},
                {"timestamp": datetime(2024, 2, 26, 7), "value": None}]

    monkeypatch.setattr(indicators, "query_series", fake_query)
    async with timeline_client as client:
        resp = await client.get("/api/timeline/unified", params={
            "start": "2024-02-24T00:00:00", "end": "2024-02-27T00:00:00",
            "indicators": "flow:EYRAUD,gw:MOULIETS,tide_obs:BORDEAUX,flow:EYRAUD",
            "include_infoclimat": "false", "resolution": "6h",
        })
    assert resp.status_code == 200
    series = resp.json()["series"]
    assert set(series) == {"flow:EYRAUD", "gw:MOULIETS", "tide_obs:BORDEAUX"}
    assert series["flow:EYRAUD"]["type"] == "flow_tributary"
    assert series["flow:EYRAUD"]["unit"] == "m³/s"
    assert [p["value"] for p in series["flow:EYRAUD"]["data"]] == [23.0]
    # Coarse resolution: observed tide keeps its high waters (max), others mean
    assert ("BORDEAUX", "6h", "max") in seen and ("EYRAUD", "6h", "mean") in seen


async def test_timeline_unified_rejects_unknown_indicator(timeline_client):
    async with timeline_client as client:
        resp = await client.get("/api/timeline/unified", params={
            "indicators": 'flow:EYRAUD,flow:X" or true', "include_infoclimat": "false",
        })
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["unknown"] == ['flow:X" or true'] and "flow:EYRAUD" in detail["valid"]


async def test_timeline_without_indicators_does_not_query_them(timeline_client, monkeypatch):
    async def boom(*a, **kw):
        raise AssertionError("indicators queried without being requested")

    monkeypatch.setattr(indicators, "query_series", boom)
    async with timeline_client as client:
        resp = await client.get("/api/timeline/unified", params={"include_infoclimat": "false"})
    assert resp.status_code == 200


# --- status -------------------------------------------------------------------------------

async def test_status_lists_indicators_as_informative(monkeypatch):
    now = datetime(2026, 10, 26, 12, 0)
    monkeypatch.setattr(status, "_utcnow", lambda: now)

    async def last_time(measurement, tags=None, field=None, start=None, stop=None):
        if measurement in ("river_flow", "groundwater", "tide_observed"):
            return None  # never collected yet
        return now

    monkeypatch.setattr(influx_client, "query_last_time", last_time)
    sources = await status.check_sources()
    eyraud = sources["indicator_flow_eyraud"]
    assert eyraud["status"] == "unavailable" and eyraud["critical"] is False
    assert eyraud["max_age_hours"] == 12.0
    assert sources["indicator_gw_mouliets"]["max_age_hours"] == 36.0
    assert "indicator_tide_obs_bordeaux" in sources
    spec = status.source_specs()["indicator_flow_eyraud"]
    assert spec["tags"] == {"key": ["EYRAUD"]} and spec["measurement"] == "river_flow"
