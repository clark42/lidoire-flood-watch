"""/api/status real dependency checks and shared cache helpers."""

import asyncio
from datetime import datetime, timedelta

import httpx
import pytest
import respx

from app.api import status
from app.config import get_settings
from app.services import cache, influx_client

_REAL_QUERY_LAST_TIME = influx_client.query_last_time


def _influx_health_url() -> str:
    return get_settings().influxdb_url.rstrip("/") + "/health"


@pytest.fixture(autouse=True)
def fresh_sources(monkeypatch):
    """Every source has a point "now" (no InfluxDB needed); cache reset."""
    status._sources_cache.clear()

    async def last_time(measurement, tags=None, field=None, start=None, stop=None):
        return status._utcnow()

    monkeypatch.setattr(influx_client, "query_last_time", last_time)
    yield
    status._sources_cache.clear()


@pytest.fixture
def ok_db_and_redis(monkeypatch):
    async def ok():
        return None
    monkeypatch.setattr(status, "_check_postgres", ok)
    monkeypatch.setattr(status, "_check_redis", ok)


@respx.mock
async def test_status_healthy_when_all_dependencies_answer(ok_db_and_redis):
    respx.get(_influx_health_url()).mock(
        return_value=httpx.Response(200, json={"name": "influxdb", "status": "pass"})
    )
    body = await status.get_status()
    assert body["status"] == "healthy"
    assert body["services"]["influxdb"] == "connected"
    assert body["services"]["database"] == "connected"
    assert body["services"]["redis"] == "connected"
    assert "latency_ms" in body["services"]["checks"]["influxdb"]


@respx.mock
async def test_status_degraded_when_influx_fails(ok_db_and_redis):
    respx.get(_influx_health_url()).mock(return_value=httpx.Response(503, json={"status": "fail"}))
    body = await status.get_status()
    assert body["status"] == "degraded"
    assert body["services"]["influxdb"] == "error"
    assert body["services"]["checks"]["influxdb"]["error"] == "HTTPStatusError"


@respx.mock
async def test_status_influx_unreachable(ok_db_and_redis):
    respx.get(_influx_health_url()).mock(side_effect=httpx.ConnectError("refused"))
    body = await status.get_status()
    assert body["services"]["influxdb"] == "error"


@respx.mock
async def test_status_timeout_is_bounded(monkeypatch):
    monkeypatch.setattr(status, "HEALTH_CHECK_TIMEOUT_S", 0.05)
    respx.get(_influx_health_url()).mock(return_value=httpx.Response(200, json={"status": "pass"}))

    async def hang():
        await asyncio.sleep(10)

    async def boom():
        raise RuntimeError("postgresql://user:secret@db/x refused")

    monkeypatch.setattr(status, "_check_postgres", hang)
    monkeypatch.setattr(status, "_check_redis", boom)

    body = await asyncio.wait_for(status.get_status(), timeout=1)
    assert body["services"]["database"] == "timeout"
    assert body["services"]["redis"] == "error"
    # error message (may contain credentials) is not exposed
    assert "secret" not in str(body)
    assert body["status"] == "degraded"


# --- data freshness ----------------------------------------------------------------

NOW = datetime(2026, 10, 26, 12, 0)


@pytest.fixture
def frozen_ages(monkeypatch, ok_db_and_redis):
    """Ages (hours, None = no point) per measurement/tag; returns the call log."""
    monkeypatch.setattr(status, "_utcnow", lambda: NOW)
    ages: dict[str, object] = {}
    calls: list[tuple] = []

    def key_of(measurement, tags):
        tags = tags or {}
        if measurement == "rainfall":
            return tags["source"][0]
        return measurement

    async def last_time(measurement, tags=None, field=None, start=None, stop=None):
        calls.append((measurement, tags, field, stop))
        age = ages.get(key_of(measurement, tags), 0.1)
        if isinstance(age, Exception):
            raise age
        return None if age is None else NOW - timedelta(hours=age)

    monkeypatch.setattr(influx_client, "query_last_time", last_time)
    return ages, calls


def _influx_ok():
    respx.get(_influx_health_url()).mock(return_value=httpx.Response(200, json={"status": "pass"}))


@respx.mock
async def test_status_sources_fresh_keep_contract(frozen_ages):
    _influx_ok()
    body = await status.get_status()
    assert body["status"] == "healthy"
    core = {"river_primary", "rain_openmeteo", "infoclimat", "netatmo", "tide", "soil", "risk_score"}
    assert core <= set(body["sources"])
    # Only informative sources are added: vigilance, hydrological indicators (tests/indicators) and probabilistic series (tests/probabilistic)
    extra = set(body["sources"]) - core
    assert "vigilance" in extra
    assert all(k in {"vigilance", "radar_opera", "glofas", "ensemble_rain"} or k.startswith("indicator_") for k in extra)
    assert not any(body["sources"][k]["critical"] for k in extra)
    river = body["sources"]["river_primary"]
    assert river["status"] == "ok" and river["critical"] is True
    assert river["last_timestamp"] == (NOW - timedelta(hours=0.1)).isoformat()
    assert river["age_hours"] == 0.1 and river["max_age_hours"] == 3.0
    # Existing contract untouched
    assert body["services"]["influxdb"] == "connected" and "collectors" in body["services"]


@respx.mock
@pytest.mark.parametrize("key,age", [("risk_score", 2.5), ("river_level", 3.5), ("risk_score", None)])
async def test_status_degraded_when_critical_source_stale(frozen_ages, key, age):
    _influx_ok()
    ages, _ = frozen_ages
    ages[key] = age
    body = await status.get_status()
    name = "risk_score" if key == "risk_score" else "river_primary"
    assert body["sources"][name]["status"] == ("unavailable" if age is None else "stale")
    assert body["status"] == "degraded"
    assert body["services"]["influxdb"] == "connected"


@respx.mock
async def test_status_optional_sources_do_not_degrade(frozen_ages, monkeypatch):
    _influx_ok()
    ages, _ = frozen_ages
    ages["infoclimat"] = None        # Infoclimat down for 30 days
    ages["netatmo"] = 5.0            # Netatmo collector stuck
    monkeypatch.setattr(get_settings(), "netatmo_client_id", "")
    body = await status.get_status()
    assert body["sources"]["infoclimat"]["status"] == "unavailable"
    assert body["sources"]["netatmo"]["status"] == "stale"
    assert "non configure" in body["sources"]["netatmo"]["note"]
    assert body["status"] == "healthy"


@respx.mock
async def test_status_tide_must_reach_into_the_future(frozen_ages):
    _influx_ok()
    ages, calls = frozen_ages
    ages["tide"] = -30.0             # next stored high/low water in 30 h
    body = await status.get_status()
    tide = body["sources"]["tide"]
    assert tide["status"] == "ok" and tide["age_hours"] == -30.0
    stop = next(stop for m, _, _, stop in calls if m == "tide")
    assert stop == NOW + status.TIDE_HORIZON

    status._sources_cache.clear()
    ages["tide"] = 1.0               # newest tide already in the past
    body = await status.get_status()
    assert body["sources"]["tide"]["status"] == "stale"


@respx.mock
async def test_status_sources_cached_and_errors_not_cached(frozen_ages):
    _influx_ok()
    ages, calls = frozen_ages
    await status.get_status()
    n = len(calls)
    await status.get_status()
    assert len(calls) == n            # served from the 60 s cache

    status._sources_cache.clear()
    ages["soil_moisture"] = RuntimeError("http://user:secret@influx refused")
    body = await status.get_status()
    assert body["sources"]["soil"]["status"] == "unknown"
    assert body["sources"]["soil"]["error"] == "RuntimeError"
    assert "secret" not in str(body)
    assert body["status"] == "healthy"   # unknown != stale
    calls.clear()
    await status.get_status()
    assert calls                      # a failed check is retried next call


@respx.mock
async def test_status_sources_skipped_when_influx_down(frozen_ages):
    _, calls = frozen_ages
    respx.get(_influx_health_url()).mock(return_value=httpx.Response(503, json={"status": "fail"}))
    body = await status.get_status()
    assert calls == []
    assert {s["status"] for s in body["sources"].values()} == {"unknown"}
    assert body["status"] == "degraded"


async def test_status_last_time_flux(monkeypatch):
    queries = []

    async def run_query(query):
        queries.append(query)
        return []

    monkeypatch.setattr(influx_client, "_run_query", run_query)
    monkeypatch.setattr(influx_client, "_utcnow", lambda: NOW)
    assert await _REAL_QUERY_LAST_TIME(
        "rainfall", tags={"source": ["netatmo", "netatmo-lidoire"]}, field="precipitation_mm_h",
    ) is None
    q = queries[-1]
    assert "range(start: 2026-09-26T12:00:00Z, stop: 2026-10-26T12:00:00Z)" in q
    assert 'r.source == "netatmo" or r.source == "netatmo-lidoire"' in q
    assert 'r._field == "precipitation_mm_h"' in q
    assert "|> last()" in q and 'keep(columns: ["_time"])' in q
    with pytest.raises(influx_client.InvalidQueryParameter):
        await _REAL_QUERY_LAST_TIME("rainfall", tags={"source": ['x") or true or ("']})


# --- cache helpers -----------------------------------------------------------------

def test_get_redis_is_a_lazy_singleton(monkeypatch):
    monkeypatch.setattr(cache, "_redis_client", None)
    first = cache.get_redis()
    assert cache.get_redis() is first
    monkeypatch.setattr(cache, "_redis_client", None)


def test_ttl_cache_expiry(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(cache.time, "monotonic", lambda: clock["t"])
    c = cache.TTLCache(ttl_seconds=10)
    c.set("a", 1)
    c.set("b", 2, ttl_seconds=100)
    assert c.get("a") == 1 and "a" in c
    clock["t"] += 11
    assert c.get("a") is None and "a" not in c
    assert c.get("b") == 2


def test_ttl_cache_bounded_size():
    c = cache.TTLCache(ttl_seconds=60, max_entries=3)
    for i in range(10):
        c.set(i, i)
    assert len(c) <= 3
    assert c.get(9) == 9
