"""External APIs: circuit breaker (negative cache), short timeouts, single log line."""

import logging
import time
from datetime import datetime, timedelta

import httpx
import pytest
import respx

from app.models.schemas import NowcastRainData
from app.services import cache
from app.services.collectors import hubeau, infoclimat, meteo_agricole, netatmo, vigicrues

START, END = datetime(2026, 10, 25), datetime(2026, 10, 26)


def _warnings(caplog, needle: str) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING and needle in r.getMessage()]


# --- CircuitBreaker ---------------------------------------------------------

def test_breaker_opens_logs_once_with_repr_and_closes(caplog):
    breaker = cache.CircuitBreaker("Test API", cooldown_s=60)
    assert breaker.allow()
    with caplog.at_level(logging.DEBUG, logger="app.services.cache"):
        breaker.record_failure(httpx.ReadTimeout(""))
        breaker.record_failure(httpx.ReadTimeout(""))
    assert not breaker.allow()
    warnings = _warnings(caplog, "Test API")
    assert len(warnings) == 1
    assert "ReadTimeout" in warnings[0]  # str(ReadTimeout("")) is empty
    breaker.record_success()
    assert breaker.allow()


def test_breaker_half_open_after_cooldown():
    breaker = cache.CircuitBreaker("Test API", cooldown_s=0.05)
    breaker.record_failure("boom")
    assert not breaker.allow()
    time.sleep(0.06)
    assert breaker.allow()


def test_upstream_failure_classification():
    request = httpx.Request("GET", "https://example.test/?token=secret")
    assert cache.is_upstream_failure(httpx.ReadTimeout("", request=request))
    assert cache.is_upstream_failure(httpx.ConnectError("refused", request=request))
    err_504 = httpx.HTTPStatusError("x", request=request, response=httpx.Response(504, request=request))
    err_403 = httpx.HTTPStatusError("x", request=request, response=httpx.Response(403, request=request))
    assert cache.is_upstream_failure(err_504)
    assert not cache.is_upstream_failure(err_403)
    assert not cache.is_upstream_failure(ValueError("bad json"))
    # HTTP errors are described without the URL (and its query-string token)
    assert "secret" not in cache.describe_error(err_504)


# --- Infoclimat (the 504-after-30s outage) ----------------------------------

@pytest.fixture
def infoclimat_http(monkeypatch):
    """Route infoclimat's httpx clients to a handler; returns the request log."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "infoclimat_api_key", "k")
    monkeypatch.setattr(infoclimat, "_stations_cache", None)
    state = {"handler": None, "requests": []}

    def handler(request):
        state["requests"].append(request)
        return state["handler"](request)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        infoclimat.httpx, "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    return state


async def test_infoclimat_timeout_opens_breaker_and_suppresses_retries(infoclimat_http, caplog):
    def timeout(request):
        raise httpx.ReadTimeout("", request=request)

    infoclimat_http["handler"] = timeout
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert await infoclimat.fetch_station_observations(["ST1"], START, END) == {}
        # same host: the stations list is skipped too while the breaker is open
        assert await infoclimat.fetch_stations_list() == []

    requests = infoclimat_http["requests"]
    assert len(requests) == 1
    assert requests[0].extensions["timeout"]["read"] == cache.EXTERNAL_TIMEOUT_S <= 10
    warnings = _warnings(caplog, "Infoclimat")
    assert len(warnings) == 1 and "ReadTimeout" in warnings[0]


async def test_infoclimat_gateway_timeout_opens_breaker(infoclimat_http):
    infoclimat_http["handler"] = lambda request: httpx.Response(504, text="Gateway Time-out")

    assert await infoclimat.fetch_station_observations(["ST1"], START, END) == {}
    assert await infoclimat.fetch_station_observations(["ST1"], START, END) == {}
    assert len(infoclimat_http["requests"]) == 1


async def test_infoclimat_client_error_does_not_open_breaker(infoclimat_http):
    # A rejected request (bad token / IP) is a config issue, not an outage
    infoclimat_http["handler"] = lambda request: httpx.Response(400, json={"errors": ["Wrong IP address"]})

    await infoclimat.fetch_station_observations(["ST1"], START, END)
    await infoclimat.fetch_station_observations(["ST1"], START, END)
    assert len(infoclimat_http["requests"]) == 2


async def test_infoclimat_peek_serves_last_available_result(monkeypatch):
    monkeypatch.setattr(infoclimat, "_latest", cache.TTLCache(ttl_seconds=60, max_entries=1))
    monkeypatch.setattr(infoclimat, "_observations_cache", None)
    monkeypatch.setattr(infoclimat, "_cache_expiry", None)
    assert infoclimat.peek_basin_rainfall_infoclimat()["available"] is False

    async def fake_find(lat, lon, radius_km):
        return [{"id": "ST1", "name": "Station 1", "distance_km": 5.0}]

    async def fake_obs(ids, start, end):
        return {"hourly": {"ST1": [{"dh_utc": "2026-10-25 12:00:00", "pluie_24h": "8.4"}]}}

    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "infoclimat_api_key", "k")
    monkeypatch.setattr(infoclimat, "find_stations_near", fake_find)
    monkeypatch.setattr(infoclimat, "fetch_station_observations", fake_obs)
    await infoclimat.get_basin_rainfall_infoclimat()  # what the scheduler job runs

    peeked = infoclimat.peek_basin_rainfall_infoclimat()
    assert peeked["available"] is True and peeked["rain_24h_avg_mm"] == 8.4


# --- Netatmo / Meteo Agricole / Vigicrues / Hub'Eau --------------------------

@respx.mock
async def test_netatmo_timeout_opens_breaker(monkeypatch):
    async def token():
        return "tok"

    monkeypatch.setattr(netatmo, "_get_access_token", token)
    monkeypatch.setattr(netatmo, "_stations_cache", {})
    route = respx.get(f"{netatmo.NETATMO_API_BASE}/getpublicdata").mock(
        side_effect=httpx.ConnectTimeout("")
    )
    assert await netatmo.fetch_public_stations_data(44.9, 0.0) == []
    assert await netatmo.fetch_public_stations_data(44.95, 0.18, 25.0) == []
    assert route.call_count == 1


@respx.mock
async def test_meteo_agricole_breaker_shared_by_both_pages(monkeypatch):
    from app.config import get_settings

    route = respx.get(url__startswith="https://www.lameteoagricole.net").mock(
        side_effect=httpx.ReadTimeout("")
    )
    monkeypatch.setattr(get_settings(), "meteo_agricole_url",
                        "https://www.lameteoagricole.net/previsions-meteo-agricole/x.html")
    assert await meteo_agricole.fetch_meteo_agricole() is None
    assert await meteo_agricole.fetch_nowcast_rain() is None
    assert route.call_count == 1


async def test_meteo_agricole_peek_never_scrapes(monkeypatch):
    now = datetime(2026, 10, 25, 12, 0)
    monkeypatch.setattr(meteo_agricole, "_utcnow", lambda: now)
    monkeypatch.setattr(meteo_agricole, "_nowcast_cache", None)
    monkeypatch.setattr(meteo_agricole, "_nowcast_cache_expiry", None)

    async def forbidden(*args, **kwargs):
        raise AssertionError("request path scraped Meteo Agricole")

    monkeypatch.setattr(meteo_agricole, "fetch_nowcast_rain", forbidden)
    assert meteo_agricole.peek_nowcast_rain() is None

    data = NowcastRainData(timestamp=now, commune="x", total_60min_mm=1.0,
                           max_intensity_mm=0.1, rain_minutes=10, minute_data=[])
    monkeypatch.setattr(meteo_agricole, "_nowcast_cache", data)
    monkeypatch.setattr(meteo_agricole, "_nowcast_cache_expiry", now - timedelta(minutes=5))
    assert meteo_agricole.peek_nowcast_rain() is data  # stale but within the grace
    monkeypatch.setattr(meteo_agricole, "_nowcast_cache_expiry", now - timedelta(minutes=11))
    assert meteo_agricole.peek_nowcast_rain() is None


@respx.mock
async def test_vigicrues_outage_skips_further_calls(monkeypatch):
    monkeypatch.setattr(vigicrues, "_river_cache", {})
    monkeypatch.setattr(vigicrues, "_cache_expiry", None)
    route = respx.get(url__startswith=vigicrues.VIGICRUES_OBSERVATIONS_URL).mock(
        side_effect=httpx.ReadTimeout("")
    )
    for _ in range(3):
        result = await vigicrues.get_current_river_level()
        assert result.available is False
    assert route.call_count == 1


@respx.mock
async def test_hubeau_total_failure_skips_further_rounds(monkeypatch):
    monkeypatch.setattr(hubeau, "_stations_cache_by_hours", {})
    from app.config import get_settings

    timeouts = []

    def handler(request):
        timeouts.append(request.extensions["timeout"]["read"])
        raise httpx.ReadTimeout("", request=request)

    respx.get(url__startswith=f"{hubeau.HUBEAU_API_BASE}/observations_tr").mock(side_effect=handler)
    assert await hubeau.fetch_all_stations_realtime(hours_back=24) == {}
    n_stations = len(get_settings().hubeau_stations)
    assert len(timeouts) == n_stations
    assert set(timeouts) == {cache.EXTERNAL_TIMEOUT_S}

    assert await hubeau.fetch_all_stations_realtime(hours_back=24) == {}
    assert len(timeouts) == n_stations  # breaker open: no new round
