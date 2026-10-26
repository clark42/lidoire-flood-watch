"""Vigilance collector: fetching (respx, recorded payloads), cache, storage, route."""

from datetime import datetime, timedelta

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from vigilance_fixtures import load_fixture

from app.api import vigilance as vigilance_api
from app.services import influx_client
from app.services.collectors import vigilance as v

NOW = datetime(2026, 10, 26, 21, 0)
DAY_URL = f"{v.MF_MIRROR_BASE}/2026/10/26/"
PREV_DAY_URL = f"{v.MF_MIRROR_BASE}/2026/10/25/"
RUN_URL = f"{v.MF_MIRROR_BASE}/2026/10/26/150006/{v.MF_MAP_FILE}"
OLDER_RUN_URL = f"{v.MF_MIRROR_BASE}/2026/10/26/050006/{v.MF_MAP_FILE}"


@pytest.fixture
def frozen(monkeypatch):
    clock = {"now": NOW}
    monkeypatch.setattr(v, "_utcnow", lambda: clock["now"])
    return clock


@pytest.fixture
def written(monkeypatch):
    points: list[dict] = []

    async def fake_write(batch, chunk_size=5000):
        points.extend(influx_client.point_as_dict(p) for p in batch)
        return len(batch)

    monkeypatch.setattr(influx_client, "write_points_batch", fake_write)
    return points


def _mock_upstreams(geo=None, mf=None):
    respx.get(v.VIGICRUES_INFO_URL).mock(
        return_value=httpx.Response(200, json=geo or load_fixture("vigicrues_infovigicru_20261026.geojson"))
    )
    respx.get(DAY_URL).mock(return_value=httpx.Response(200, text=load_fixture("meteofrance_listing_20261026.html")))
    respx.get(PREV_DAY_URL).mock(return_value=httpx.Response(404))
    return respx.get(RUN_URL).mock(
        return_value=httpx.Response(200, json=mf or load_fixture("meteofrance_20261026_150006.json"))
    )


@respx.mock
async def test_vigilance_collect_fills_cache_and_writes_once(frozen, written):
    map_route = _mock_upstreams()
    result = await v.collect_and_store_vigilance()
    assert result == {"vigicrues": True, "meteofrance": True, "points_written": 10}
    assert {p["measurement"] for p in written} == {"vigilance"}
    tags = {(p["tags"]["source"], p["tags"]["zone"], p["tags"]["phenomenon"]) for p in written}
    assert ("vigicrues", "DO5", "crues") in tags and ("meteofrance", "24", "pluie-inondation") in tags
    assert all(p["fields"] == {"level": 1.0} for p in written)

    cached = v.peek_vigilance()
    assert cached["vigicrues"]["troncons"][0]["level"] == 1
    assert cached["meteofrance"]["departments"][0]["code"] == "33"

    # Same colours 30 min later: nothing written, the same run is not downloaded again
    frozen["now"] = NOW + timedelta(minutes=30)
    result = await v.collect_and_store_vigilance()
    assert result["points_written"] == 0 and len(written) == 10
    assert map_route.call_count == 1
    assert v.peek_vigilance()["meteofrance"]["fetched_at"] == NOW + timedelta(minutes=30)


@respx.mock
async def test_vigilance_collect_writes_only_the_change(frozen, written):
    _mock_upstreams()
    await v.collect_and_store_vigilance()
    geo = load_fixture("vigicrues_infovigicru_20261026.geojson")
    for f in geo["features"]:
        if f["properties"]["CdEntCru"] == "DO5":
            f["properties"]["NivInfViCr"] = 2
    respx.get(v.VIGICRUES_INFO_URL).mock(return_value=httpx.Response(200, json=geo))
    frozen["now"] = NOW + timedelta(minutes=30)
    result = await v.collect_and_store_vigilance()
    assert result["points_written"] == 1
    assert written[-1]["tags"] == {"source": "vigicrues", "zone": "DO5", "phenomenon": "crues"}
    assert written[-1]["fields"] == {"level": 2.0}


@respx.mock
async def test_vigilance_failed_write_is_retried(frozen, monkeypatch):
    _mock_upstreams()

    async def failing_write(batch, chunk_size=5000):
        return 0

    monkeypatch.setattr(influx_client, "write_points_batch", failing_write)
    await v.collect_and_store_vigilance()
    assert v._stored == {}


@respx.mock
async def test_vigilance_text_only_run_falls_back_to_older_map(frozen, written):
    respx.get(v.VIGICRUES_INFO_URL).mock(return_value=httpx.Response(503))
    respx.get(DAY_URL).mock(return_value=httpx.Response(200, text=load_fixture("meteofrance_listing_20261026.html")))
    respx.get(PREV_DAY_URL).mock(return_value=httpx.Response(404))
    respx.get(RUN_URL).mock(return_value=httpx.Response(404, text="<Error><Code>NoSuchKey</Code></Error>"))
    respx.get(OLDER_RUN_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("meteofrance_20260212_070229.json"))
    )
    result = await v.collect_and_store_vigilance()
    assert result["vigicrues"] is False and result["meteofrance"] is True
    assert v._mf_last_run == "2026/10/26/050006"
    assert v.peek_vigilance()["vigicrues"] is None
    # Vigicrues 5xx opened its breaker: the next run does not call it
    assert not v._vigicrues_breaker.allow()
    assert {p["tags"]["source"] for p in written} == {"meteofrance"}


@respx.mock
async def test_vigilance_timeout_is_handled(frozen, written):
    respx.get(v.VIGICRUES_INFO_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    respx.get(DAY_URL).mock(side_effect=httpx.ConnectError("down"))
    result = await v.collect_and_store_vigilance()
    assert result == {"vigicrues": False, "meteofrance": False, "points_written": 0}
    assert not v._meteofrance_breaker.allow()


def test_vigilance_cache_expires(monkeypatch):
    import time

    v._latest.set(v.SOURCE_VIGICRUES, {"available": True, "troncons": []})
    assert v.peek_vigilance()["vigicrues"] is not None
    real = time.monotonic
    monkeypatch.setattr("app.services.cache.time.monotonic", lambda: real() + v.LATEST_MAX_AGE_S + 1)
    assert v.peek_vigilance()["vigicrues"] is None


@respx.mock(assert_all_called=False)
def test_vigilance_route_reads_cache_only(frozen):
    # No route is mocked: any upstream call would raise inside respx
    app = FastAPI()
    app.include_router(vigilance_api.router, prefix="/api/vigilance")
    client = TestClient(app)

    body = client.get("/api/vigilance").json()
    assert body["vigicrues"]["available"] is False
    assert [t["code"] for t in body["vigicrues"]["troncons"]] == ["DO5", "DO22"]
    assert body["meteofrance"]["departments"][1]["url"] == "https://vigilance.meteofrance.fr/fr/dordogne"

    meteofrance = v.parse_meteofrance(
        load_fixture("meteofrance_20260212_070229.json"),
        [{"code": "33", "name": "Gironde", "slug": "gironde"}], ["4"], NOW,
    )
    v._latest.set(v.SOURCE_METEOFRANCE, meteofrance)
    body = client.get("/api/vigilance").json()
    crues = body["meteofrance"]["departments"][0]["phenomena"][0]
    assert crues["level"] == 4 and crues["color"] == "rouge"
    assert crues["windows"][0]["begin"] == "2026-02-12T07:00:00Z"
    assert body["meteofrance"]["issued_at"] == "2026-02-12T07:02:32Z"
    assert "score" in body["note"]


def test_vigilance_job_registered_every_30_min():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    scheduler = AsyncIOScheduler()
    v.schedule_vigilance(scheduler, first_run=datetime(2026, 10, 26, 21, 0))
    job = scheduler.get_job(v.JOB_ID)
    assert job is not None and job.trigger.interval == timedelta(minutes=30)


def test_vigilance_is_not_a_model_input():
    import inspect

    from app.services import forecast, risk_core, risk_v2

    for module in (risk_core, risk_v2, forecast):
        assert "vigilance" not in inspect.getsource(module)
