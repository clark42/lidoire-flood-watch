"""/api/status real dependency checks and shared cache helpers."""

import asyncio

import httpx
import pytest
import respx

from app.api import status
from app.config import get_settings
from app.services import cache


def _influx_health_url() -> str:
    return get_settings().influxdb_url.rstrip("/") + "/health"


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
