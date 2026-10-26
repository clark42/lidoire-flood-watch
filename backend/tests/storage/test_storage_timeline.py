"""Timeline API: range bounds, id normalisation, cumulative policy, negative cache."""

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api import timeline

NOW = datetime(2024, 2, 15, 12, 0)


# --- range bounds -----------------------------------------------------------------

def test_resolve_time_range_defaults_to_48h():
    start, end = timeline.resolve_time_range(None, None, now=NOW)
    assert end == NOW
    assert start == NOW - timedelta(hours=48)


def test_resolve_time_range_converts_aware_to_naive_utc():
    start, end = timeline.resolve_time_range(
        datetime(2024, 2, 15, 10, tzinfo=ZoneInfo("Europe/Paris")),
        datetime(2024, 2, 15, 12, tzinfo=timezone.utc),
        now=NOW,
    )
    assert start == datetime(2024, 2, 15, 9)
    assert end == datetime(2024, 2, 15, 12)
    assert start.tzinfo is None and end.tzinfo is None


def test_resolve_time_range_rejects_inverted_range():
    with pytest.raises(HTTPException) as exc:
        timeline.resolve_time_range(NOW, NOW - timedelta(hours=1), now=NOW)
    assert exc.value.status_code == 422


def test_resolve_time_range_rejects_too_long_range():
    with pytest.raises(HTTPException) as exc:
        timeline.resolve_time_range(NOW - timedelta(days=401), NOW, now=NOW)
    assert exc.value.status_code == 422
    # 400 days exactly is fine
    timeline.resolve_time_range(NOW - timedelta(days=400), NOW, now=NOW)


# --- id normalisation ------------------------------------------------------------------

def test_rainfall_catalogue_comes_from_config():
    assert "netatmo-lidoire:Saint-Rémy" in timeline.RAINFALL_STATIONS
    assert "netatmo-lidoire:Saint-Antoine-de-Breuilh" in timeline.RAINFALL_STATIONS
    assert "netatmo-lidoire:Saint-Remy" not in timeline.RAINFALL_STATIONS
    assert timeline.OPEN_METEO_KEY in timeline.RAINFALL_STATIONS


def test_normalize_rainfall_sources_maps_legacy_ids():
    out = timeline.normalize_rainfall_sources([
        "netatmo-lidoire:Saint-Remy",
        "netatmo-lidoire:Saint-Antoine-sur-l'Isle",
        "netatmo-lidoire:flaujagues",
        "netatmo-lidoire:Saint-Rémy",  # duplicate after normalisation
    ])
    assert out == [
        "netatmo-lidoire:Saint-Rémy",
        "netatmo-lidoire:Saint-Antoine-de-Breuilh",
        "netatmo-lidoire:Flaujagues",
    ]


def test_normalize_rainfall_sources_rejects_unknown():
    with pytest.raises(HTTPException) as exc:
        timeline.normalize_rainfall_sources(['netatmo-lidoire:X") |> drop()'])
    assert exc.value.status_code == 422


def test_validate_river_stations():
    assert timeline.validate_river_stations(["P555001001", "P555001001"]) == ["P555001001"]
    with pytest.raises(HTTPException) as exc:
        timeline.validate_river_stations(["P555001001", "bogus"])
    assert exc.value.status_code == 422


def test_split_rainfall_request_reads_accent_variants():
    sources, cities = timeline._split_rainfall_request(
        ["netatmo-lidoire:Saint-Rémy", timeline.OPEN_METEO_KEY]
    )
    assert sources == ["netatmo-lidoire", "open-meteo"]
    assert cities == ["Saint-Rémy", "Saint-Remy", "Moulin-Lidoire"]


def test_canonicalize_rainfall_merges_variants():
    t = datetime(2024, 2, 15, 10)
    raw = {
        "netatmo-lidoire:Saint-Remy": [{"timestamp": t, "value": 2.0}],
        "netatmo-lidoire:Saint-Rémy": [{"timestamp": t, "value": 4.0},
                                       {"timestamp": t + timedelta(hours=1), "value": 1.0}],
        "netatmo-lidoire:Other": [{"timestamp": t, "value": 9.0}],
    }
    out = timeline._canonicalize_rainfall(raw, ["netatmo-lidoire:Saint-Rémy"])
    assert out == {"netatmo-lidoire:Saint-Rémy": [
        {"timestamp": t, "value": 3.0},
        {"timestamp": t + timedelta(hours=1), "value": 1.0},
    ]}


# --- cumulative policy ----------------------------------------------------------------

def test_cumulative_uses_gauge_mean_and_model_fallback():
    h = [datetime(2024, 2, 15, i) for i in range(4)]
    hourly = {
        "netatmo-lidoire:A": [{"timestamp": h[0], "value": 2.0}, {"timestamp": h[1], "value": 4.0}],
        "netatmo-lidoire:B": [{"timestamp": h[0], "value": 4.0}],
        "open-meteo:Moulin-Lidoire": [
            {"timestamp": h[0], "value": 100.0},  # ignored: gauges reported
            {"timestamp": h[2], "value": 1.5},    # used: no gauge this hour
            {"timestamp": h[3], "value": None},
        ],
    }
    cumul = timeline.compute_cumulative_rainfall(hourly)
    assert [(p["timestamp"], p["value"]) for p in cumul] == [
        (h[0], 3.0),   # mean(2, 4)
        (h[1], 7.0),   # + 4
        (h[2], 8.5),   # + model 1.5
    ]


def test_cumulative_empty():
    assert timeline.compute_cumulative_rainfall({}) == []


# --- negative cache of _ensure_* ---------------------------------------------------------

class _Counter:
    def __init__(self, coverage):
        self.calls = 0
        self.coverage = coverage

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        return self.coverage


@pytest.fixture
def tide_backfill(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch(start, end):
        calls["n"] += 1
        return 0  # archive gap: API returns nothing

    import app.services.collectors.tide as tide_mod
    monkeypatch.setattr(tide_mod, "fetch_and_store_tides_range", fake_fetch)
    return calls


async def test_ensure_tide_backfills_once_then_negative_cached(monkeypatch, tide_backfill):
    coverage = _Counter({})
    monkeypatch.setattr(timeline, "query_coverage", coverage)
    start, end = datetime(2023, 1, 1), datetime(2023, 1, 3)

    task = await timeline._ensure_tide_data(start, end)
    assert isinstance(task, asyncio.Task)
    await task
    assert tide_backfill["n"] == 1

    # Same period again: no count query, no backfill
    for _ in range(3):
        assert await timeline._ensure_tide_data(start, end) is None
    assert coverage.calls == 1
    assert tide_backfill["n"] == 1


async def test_ensure_returns_inflight_task_without_duplicating(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}

    async def slow_fetch(start, end):
        calls["n"] += 1
        started.set()
        await release.wait()
        return 1

    import app.services.collectors.tide as tide_mod
    monkeypatch.setattr(tide_mod, "fetch_and_store_tides_range", slow_fetch)
    monkeypatch.setattr(timeline, "query_coverage", _Counter({}))

    start, end = datetime(2023, 1, 1), datetime(2023, 1, 3)
    t1 = await timeline._ensure_tide_data(start, end)
    await started.wait()
    t2 = await timeline._ensure_tide_data(start, end)
    assert t1 is t2
    release.set()
    await t1
    assert calls["n"] == 1


async def test_ensure_ok_is_cached(monkeypatch, tide_backfill):
    coverage = _Counter({"LIDOIRE": {"count": 1000, "first": None}})
    monkeypatch.setattr(timeline, "query_coverage", coverage)
    start, end = datetime(2023, 1, 1), datetime(2023, 1, 3)
    assert await timeline._ensure_tide_data(start, end) is None
    assert await timeline._ensure_tide_data(start, end) is None
    assert coverage.calls == 1
    assert tide_backfill["n"] == 0


async def test_ensure_influx_error_neither_backfills_nor_caches(monkeypatch, tide_backfill):
    coverage = _Counter(None)
    monkeypatch.setattr(timeline, "query_coverage", coverage)
    start, end = datetime(2023, 1, 1), datetime(2023, 1, 3)
    assert await timeline._ensure_tide_data(start, end) is None
    assert await timeline._ensure_tide_data(start, end) is None
    assert coverage.calls == 2
    assert tide_backfill["n"] == 0


async def test_ensure_river_uses_counts_and_gap_detection(monkeypatch):
    calls = []

    async def fake_elab(s, e, ids):
        calls.append(("elab", ids))
        return 0

    async def fake_tr(s, e, ids=None):
        calls.append(("tr", ids))
        return 0

    import app.services.data_store as ds
    monkeypatch.setattr(ds, "_fetch_and_store_river_levels_elab", fake_elab)
    monkeypatch.setattr(ds, "_fetch_and_store_river_levels", fake_tr)

    start, end = datetime(2023, 1, 1), datetime(2023, 1, 11)  # old period (> 30 days)
    # plenty of points but data starts 5 days late -> gap -> backfill
    monkeypatch.setattr(timeline, "query_coverage", _Counter({
        "P555001001": {"count": 10_000, "first": start + timedelta(days=5)},
    }))
    task = await timeline._ensure_river_data(start, end, ["P555001001"])
    assert task is not None
    await task
    assert calls == [("elab", ["P555001001"])]


async def test_ensure_netatmo_counts_hours_across_accent_variants(monkeypatch):
    calls = []

    async def fake_hist(s, e, cities):
        calls.append(cities)
        return 0

    import app.services.collectors.netatmo as netatmo_mod
    monkeypatch.setattr(netatmo_mod, "fetch_and_store_netatmo_historical_range", fake_hist)
    # 48h period -> expected 16 hours, threshold 8
    monkeypatch.setattr(timeline, "query_coverage", _Counter({
        "Saint-Remy": {"count": 20, "first": None},   # historical tag (accent-stripped)
        "Flaujagues": {"count": 2, "first": None},
    }))
    start, end = datetime(2023, 1, 1), datetime(2023, 1, 3)
    task = await timeline._ensure_netatmo_rainfall(start, end, ["Saint-Rémy", "Flaujagues"])
    await task
    assert calls == [["Flaujagues"]]


# --- endpoint --------------------------------------------------------------------------------

@pytest.fixture
def app_client():
    app = FastAPI()
    app.include_router(timeline.router, prefix="/api/timeline")
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_unified_rejects_too_long_range(app_client):
    async with app_client as client:
        resp = await client.get("/api/timeline/unified", params={
            "start": "2020-01-01T00:00:00", "end": "2024-01-01T00:00:00",
        })
    assert resp.status_code == 422


async def test_unified_rejects_unknown_station(app_client):
    async with app_client as client:
        resp = await client.get("/api/timeline/unified", params={
            "river_stations": 'P555001001,X" or true',
            "include_infoclimat": "false",
        })
    assert resp.status_code == 422


async def test_unified_does_not_block_on_slow_backfill(app_client, monkeypatch):
    monkeypatch.setattr(timeline, "BACKFILL_INLINE_WAIT_S", 0.05)
    release = asyncio.Event()

    async def slow_fetch(start, end):
        await release.wait()
        return 0

    import app.services.collectors.tide as tide_mod
    monkeypatch.setattr(tide_mod, "fetch_and_store_tides_range", slow_fetch)
    monkeypatch.setattr(timeline, "query_coverage", _Counter({}))

    async def fake_tides(start, end, port, resolution):
        return [{"timestamp": datetime(2024, 2, 15, 10), "value": 3.2},
                {"timestamp": datetime(2024, 2, 15, 11), "value": None}]

    monkeypatch.setattr(timeline, "query_tides_timeline", fake_tides)

    async with app_client as client:
        resp = await asyncio.wait_for(client.get("/api/timeline/unified", params={
            "start": "2024-02-14T00:00:00", "end": "2024-02-15T12:00:00",
            "include_tide": "true", "include_infoclimat": "false",
        }), timeout=2)
    assert resp.status_code == 200
    assert resp.headers.get("X-Backfill-Pending") == "tide"
    body = resp.json()
    assert [p["value"] for p in body["series"]["tide:LIDOIRE"]["data"]] == [3.2]
    release.set()
    await asyncio.sleep(0)


async def test_unified_rainfall_coarse_resolution_and_cumul(app_client, monkeypatch):
    monkeypatch.setattr(timeline, "query_coverage", _Counter({
        "_all": {"count": 10_000, "first": None},
        "Flaujagues": {"count": 10_000, "first": None},
    }))
    seen = []
    t = datetime(2024, 2, 15, 6)

    async def fake_rain(start, end, sources=None, cities=None, resolution="1h", agg_fn="mean"):
        seen.append((tuple(sources or ()), resolution, agg_fn))
        if resolution == "6h":
            return {"netatmo-lidoire:Flaujagues": [{"timestamp": t, "value": 6.0}] * 8}
        return {"netatmo-lidoire:Flaujagues": [
            {"timestamp": t + timedelta(hours=i), "value": 1.0} for i in range(3)
        ]}

    monkeypatch.setattr(timeline, "query_rainfall_by_station", fake_rain)
    async with app_client as client:
        resp = await client.get("/api/timeline/unified", params={
            "start": "2024-02-14T00:00:00", "end": "2024-02-15T12:00:00",
            "rainfall_sources": "netatmo-lidoire:Flaujagues",
            "resolution": "6h", "include_infoclimat": "false",
        })
    assert resp.status_code == 200, resp.text
    series = resp.json()["series"]
    assert series["rainfall:netatmo-lidoire:Flaujagues"]["unit"] == "mm"
    assert (("netatmo-lidoire",), "6h", "sum") in seen
    assert (("netatmo-lidoire",), "1h", "sum") in seen  # cumul from hourly totals
    assert [p["value"] for p in series["rainfall_cumul"]["data"]] == [1.0, 2.0, 3.0]
