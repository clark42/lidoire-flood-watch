"""Archive downloads: parsing, cache, offline mode, HIXnJ fallback (respx, no network)."""

from datetime import date, datetime

import httpx
import pytest
import respx

from scripts.eval import sources
from scripts.eval.sources import (
    PoliteClient,
    RawCache,
    cached_get,
    download_rain,
    download_station_levels,
    parse_hubeau_elab,
    parse_hydroportail,
    parse_open_meteo_hourly,
    quarter_chunks,
    year_chunks,
)

HYDRO_PAYLOAD = {
    "timezone": "UTC",
    "series": {
        "unit": "mm",
        "data": [
            {"v": 1711, "t": "2024-02-20T00:10:00Z"},
            {"v": None, "t": "2024-02-20T01:00:00Z"},
            {"v": 1739, "t": "2024-02-20T04:05:00Z"},
        ],
    },
}


@pytest.fixture
def client():
    c = PoliteClient(min_interval_s={}, sleep=lambda s: None, retries=2)
    yield c
    c.close()


def test_evaluation_parse_hydroportail_mm_to_m_utc():
    pts = parse_hydroportail(HYDRO_PAYLOAD)
    assert pts == [(datetime(2024, 2, 20, 0, 10), 1.711), (datetime(2024, 2, 20, 4, 5), 1.739)]
    assert parse_hydroportail(None) == [] and parse_hydroportail({"series": {}}) == []


def test_evaluation_parse_hubeau_elab_and_open_meteo():
    daily = parse_hubeau_elab({"data": [
        {"date_obs_elab": "2024-02-27", "resultat_obs_elab": 5155},
        {"date_obs_elab": "2024-02-26", "resultat_obs_elab": None},
    ]})
    assert daily == [(date(2024, 2, 27), 5.155)]
    om = parse_open_meteo_hourly(
        {"hourly": {"time": ["2024-01-01T00:00", "2024-01-01T01:00"], "precipitation": [0.2, None]}},
        "precipitation",
    )
    assert om == [(datetime(2024, 1, 1, 0), 0.2), (datetime(2024, 1, 1, 1), None)]


def test_evaluation_chunks_aligned_whole_periods_capped():
    # whole quarters whatever the requested bounds (stable cache keys)
    assert quarter_chunks(date(2022, 11, 1), date(2023, 4, 10)) == [
        (date(2022, 10, 1), date(2022, 12, 31)),
        (date(2023, 1, 1), date(2023, 3, 31)),
        (date(2023, 4, 1), date(2023, 6, 30)),
    ]
    assert quarter_chunks(date(2022, 12, 15), date(2023, 1, 2)) == [
        (date(2022, 10, 1), date(2022, 12, 31)), (date(2023, 1, 1), date(2023, 3, 31)),
    ]
    # only the last day with data shortens the current chunk
    assert quarter_chunks(date(2026, 9, 1), date(2026, 10, 25), cap=date(2026, 10, 25)) == [
        (date(2026, 7, 1), date(2026, 9, 30)), (date(2026, 10, 1), date(2026, 10, 25)),
    ]
    assert year_chunks(date(2022, 10, 24), date(2023, 2, 1), cap=date(2023, 1, 31)) == [
        (date(2022, 1, 1), date(2022, 12, 31)), (date(2023, 1, 1), date(2023, 1, 31)),
    ]


@respx.mock
def test_evaluation_cached_get_downloads_once_then_offline(tmp_path, client):
    route = respx.get("https://example.test/api").mock(return_value=httpx.Response(200, json={"a": 1}))
    cache = RawCache(tmp_path)
    assert cached_get(cache, client, "k", "key1", "https://example.test/api", {"x": 1}) == {"a": 1}
    assert cached_get(cache, client, "k", "key1", "https://example.test/api", {"x": 1}) == {"a": 1}
    assert route.call_count == 1
    assert route.calls[0].request.method == "GET"
    # offline: cache only
    assert cached_get(cache, None, "k", "key1", "https://example.test/api", {}) == {"a": 1}
    assert cached_get(cache, None, "k", "other", "https://example.test/api", {}) is None
    # refresh forces a new download
    cached_get(cache, client, "k", "key1", "https://example.test/api", {}, refresh=True)
    assert route.call_count == 2


@respx.mock
def test_evaluation_cached_get_errors_are_not_cached(tmp_path, client):
    route = respx.get("https://example.test/err").mock(return_value=httpx.Response(503))
    cache = RawCache(tmp_path)
    assert cached_get(cache, client, "k", "e", "https://example.test/err", {}) is None
    assert route.call_count == 2  # retried
    assert not cache.path("k", "e").exists()
    route404 = respx.get("https://example.test/404").mock(return_value=httpx.Response(404))
    assert cached_get(cache, client, "k", "n", "https://example.test/404", {}) is None
    assert route404.call_count == 1  # client errors are not retried


@respx.mock
def test_evaluation_station_levels_fall_back_to_daily_max(tmp_path, client):
    sid = "P555001001"
    hydro = respx.get(sources.HYDROPORTAIL_URL.format(station_id=sid)).mock(side_effect=[
        httpx.Response(200, json=HYDRO_PAYLOAD),
        httpx.Response(200, json={"series": {"data": []}}),
    ])
    hubeau = respx.get(sources.HUBEAU_ELAB_URL).mock(return_value=httpx.Response(200, json={
        "data": [{"date_obs_elab": "2024-04-02", "resultat_obs_elab": 3100}],
    }))
    cache = RawCache(tmp_path)
    raw = download_station_levels(sid, date(2024, 2, 1), date(2024, 4, 30), cache, client)
    assert hydro.call_count == 2 and hubeau.call_count == 1
    assert hubeau.calls[0].request.url.params["grandeur_hydro_elab"] == "HIXnJ"
    assert hydro.calls[0].request.headers["X-Requested-With"] == "XMLHttpRequest"
    assert raw.chunks_ok == 1
    assert raw.chunks_daily_fallback == [f"{sid}_2024-04-01_2024-06-30"]
    assert [h for _, h in raw.points] == [1.711, 1.739]
    assert raw.daily_max == [(date(2024, 4, 2), 3.1)]

    # second run is fully offline from the cache
    raw2 = download_station_levels(sid, date(2024, 2, 1), date(2024, 4, 30), cache, None)
    assert raw2.points == raw.points and raw2.daily_max == raw.daily_max


@respx.mock
def test_evaluation_rain_era5_fills_missing_hours(tmp_path, client):
    hist = respx.get(sources.OPEN_METEO_HIST_FORECAST_URL).mock(return_value=httpx.Response(200, json={
        "hourly": {"time": ["2024-01-01T00:00", "2024-01-01T01:00"], "precipitation": [0.5, None]},
    }))
    era5 = respx.get(sources.OPEN_METEO_ARCHIVE_URL).mock(return_value=httpx.Response(200, json={
        "hourly": {"time": ["2024-01-01T00:00", "2024-01-01T01:00"], "precipitation": [9.0, 1.5]},
    }))
    rain = download_rain([{"id": "aval", "lat": 44.876, "lon": 0.001}],
                         date(2024, 1, 1), date(2024, 1, 1), RawCache(tmp_path), client)
    assert rain.by_point["aval"] == {datetime(2024, 1, 1, 0): 0.5, datetime(2024, 1, 1, 1): 1.5}
    assert rain.era5_filled["aval"] == 1
    assert hist.calls[0].request.url.params["timezone"] == "UTC"
    assert era5.call_count == 1


def test_evaluation_polite_client_spaces_requests():
    waits = []
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    c = PoliteClient(min_interval_s={"a.test": 5.0}, sleep=waits.append, transport=transport)
    try:
        c.get_json("https://a.test/x", {})
        c.get_json("https://a.test/x", {})
    finally:
        c.close()
    assert len(waits) == 1 and 4.0 < waits[0] <= 5.0
    assert c.n_requests == 2
