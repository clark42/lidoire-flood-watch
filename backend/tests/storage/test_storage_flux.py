"""Flux query builders: validation, escaping, aggregation order, tz handling."""

import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services import influx_client as ic
from app.services.influx_client import InvalidQueryParameter

START = datetime(2024, 2, 10)
END = datetime(2024, 2, 12)


# --- _flux_ts / to_naive_utc -------------------------------------------------

def test_flux_ts_converts_aware_datetime_to_utc():
    paris = datetime(2024, 2, 15, 14, 0, tzinfo=ZoneInfo("Europe/Paris"))  # UTC+1
    assert ic._flux_ts(paris) == "2024-02-15T13:00:00Z"


def test_flux_ts_summer_offset():
    paris = datetime(2024, 7, 1, 12, 30, tzinfo=ZoneInfo("Europe/Paris"))  # UTC+2
    assert ic._flux_ts(paris) == "2024-07-01T10:30:00Z"


def test_flux_ts_naive_is_assumed_utc_and_utc_aware_unchanged():
    assert ic._flux_ts(datetime(2024, 1, 1, 6)) == "2024-01-01T06:00:00Z"
    assert ic._flux_ts(datetime(2024, 1, 1, 6, tzinfo=timezone.utc)) == "2024-01-01T06:00:00Z"
    assert "+00:00" not in ic._flux_ts(datetime(2024, 1, 1, tzinfo=timezone.utc))


def test_to_naive_utc():
    aware = datetime(2024, 1, 1, 12, tzinfo=timezone(timedelta(hours=3)))
    assert ic.to_naive_utc(aware) == datetime(2024, 1, 1, 9)
    naive = datetime(2024, 1, 1, 12)
    assert ic.to_naive_utc(naive) is naive


# --- escaping / validation ----------------------------------------------------

def test_flux_string_escapes_quotes_backslashes_and_interpolation():
    assert ic.flux_string('a"b') == '"a\\"b"'
    assert ic.flux_string("a\\b") == '"a\\\\b"'
    assert ic.flux_string("${token}") == '"\\${token}"'


def test_flux_string_rejects_control_chars():
    with pytest.raises(InvalidQueryParameter):
        ic.flux_string("abc\n|> drop()")


def test_validate_station_ids_whitelist():
    assert ic.validate_station_ids(["P555001001", "P577001001"]) == ["P555001001", "P577001001"]
    with pytest.raises(InvalidQueryParameter):
        ic.validate_station_ids(['P555001001") |> to(bucket: "x'])
    with pytest.raises(InvalidQueryParameter):
        ic.validate_station_ids(["P000000000"])


@pytest.mark.parametrize("city", ["Saint-Rémy", "Les Salles-de-Castillon", "Saint-Antoine-sur-l'Isle", "Moulin-Lidoire"])
def test_validate_tag_value_accepts_place_names(city):
    assert ic.validate_tag_value(city) == city


@pytest.mark.parametrize("bad", ['x") or true or ("', "a\"b", "", "x;y", "a\\b", "${x}"])
def test_validate_tag_value_rejects_injection(bad):
    with pytest.raises(InvalidQueryParameter):
        ic.validate_tag_value(bad)


def test_validate_port_whitelist():
    assert ic.validate_port("LIBOURNE") == "LIBOURNE"
    with pytest.raises(InvalidQueryParameter):
        ic.validate_port('LIBOURNE" or r.port != "')


def test_resolution_and_agg_fn_whitelists():
    with pytest.raises(InvalidQueryParameter):
        ic.validate_resolution("1h, fn: (x) => x")
    with pytest.raises(InvalidQueryParameter):
        ic.validate_agg_fn("mean) |> to(bucket: \"evil\"")


def test_rainfall_query_rejects_injected_city_and_source():
    with pytest.raises(InvalidQueryParameter):
        ic.build_rainfall_by_station_query(START, END, cities=['Flaujagues") |> drop(columns: ["x'])
    with pytest.raises(InvalidQueryParameter):
        ic.build_rainfall_by_station_query(START, END, sources=['netatmo" or "1" == "1'])


def test_river_query_rejects_unknown_station():
    with pytest.raises(InvalidQueryParameter):
        ic.build_river_levels_multi_station_query(START, END, ["P555001001", 'X" or true or "'])


async def test_query_functions_raise_before_hitting_influx(capture_queries):
    queries, _ = capture_queries
    with pytest.raises(InvalidQueryParameter):
        await ic.query_river_levels('bad"id', START, END)
    with pytest.raises(InvalidQueryParameter):
        await ic.query_tides("NOWHERE", START, END)
    with pytest.raises(InvalidQueryParameter):
        await ic.query_latest_measurement("wind", {"location) or (r.x": "moulin"})
    with pytest.raises(InvalidQueryParameter):
        await ic.query_latest_point("wind; drop", "moulin")
    assert queries == []


def test_rainfall_query_contains_escaped_filters():
    q = ic.build_rainfall_by_station_query(
        START, END, sources=["netatmo-lidoire"], cities=["Saint-Rémy", "Flaujagues"],
    )
    assert 'r.source == "netatmo-lidoire"' in q
    assert 'r.city == "Saint-Rémy" or r.city == "Flaujagues"' in q
    assert "range(start: 2024-02-10T00:00:00Z, stop: 2024-02-12T00:00:00Z)" in q


# --- aggregation order --------------------------------------------------------

def test_rain_sum_coarse_is_hourly_mean_then_sum():
    q = ic.build_rainfall_by_station_query(START, END, resolution="6h", agg_fn="sum")
    hourly = q.index('aggregateWindow(every: 1h, fn: mean, createEmpty: false, timeSrc: "_start")')
    coarse = q.index("aggregateWindow(every: 6h, fn: sum, createEmpty: false)")
    assert hourly < coarse
    # never a raw sum of points
    assert "every: 1h, fn: sum" not in q


def test_rain_sum_hourly_is_hourly_mean():
    q = ic.build_rainfall_by_station_query(START, END, resolution="1h", agg_fn="sum")
    assert "aggregateWindow(every: 1h, fn: mean, createEmpty: false)" in q
    assert "fn: sum" not in q


def test_rain_sum_sub_hour_scales_rate():
    q = ic.build_rainfall_by_station_query(START, END, resolution="15m", agg_fn="sum")
    assert "aggregateWindow(every: 15m, fn: mean" in q
    assert "r._value * 0.25" in q
    assert "fn: sum" not in q


def test_rain_mean_is_single_stage():
    q = ic.build_rainfall_by_station_query(START, END, resolution="6h", agg_fn="mean")
    assert q.count("aggregateWindow") == 1
    assert "aggregateWindow(every: 6h, fn: mean, createEmpty: false)" in q


def test_basin_sparkline_groups_before_aggregating():
    q = ic.build_basin_sparkline_query(START, END)
    assert q.index("|> group()") < q.index("aggregateWindow(every: 1h, fn: mean")
    assert 'r.source == "netatmo-lidoire"' in q


async def test_basin_sparkline_returns_all_hours_single_series(capture_queries, fake_tables):
    queries, set_response = capture_queries
    t0 = datetime(2024, 2, 10, 1, tzinfo=timezone.utc)
    set_response(fake_tables([[
        {"_time": t0, "_value": 1.26},
        {"_time": t0 + timedelta(hours=1), "_value": None},
    ]]))
    points = await ic.query_basin_rainfall_sparkline(24)
    assert len(queries) == 1
    assert [p["value"] for p in points] == [1.3, 0.0]


# --- coverage query -------------------------------------------------------------

def test_coverage_query_counts_in_flux():
    q = ic.build_coverage_query(
        "river_level", START, END, field="height_m", filters={"station_id": ["P555001001"]},
    )
    assert 'data |> count() |> yield(name: "count")' in q
    assert 'data |> first() |> yield(name: "first")' in q
    assert 'r.station_id == "P555001001"' in q
    assert "aggregateWindow" not in q


def test_coverage_query_window_counts_windows_with_data():
    q = ic.build_coverage_query("rainfall", START, END, field="precipitation_mm_h",
                                filters={"source": ["open-meteo"]}, window="1h")
    assert "aggregateWindow(every: 1h, fn: count, createEmpty: false)" in q


def test_coverage_query_validates_filters():
    with pytest.raises(InvalidQueryParameter):
        ic.build_coverage_query("river_level", START, END, filters={"station_id": ["nope"]})
    with pytest.raises(InvalidQueryParameter):
        ic.build_coverage_query("river_level", START, END, filters={"bad col": ["x"]})
    with pytest.raises(InvalidQueryParameter):
        ic.build_coverage_query('river" or "', START, END)


async def test_query_coverage_parses_count_and_first(capture_queries, fake_tables):
    _, set_response = capture_queries
    first_a = datetime(2024, 2, 10, 3, tzinfo=timezone.utc)
    set_response(fake_tables([
        [{"result": "count", "station_id": "P555001001", "_value": 10}],
        [{"result": "count", "station_id": "P555001001", "_value": 5}],  # second series
        [{"result": "first", "station_id": "P555001001", "_time": first_a, "_value": 2.0}],
        [{"result": "first", "station_id": "P555001001", "_time": first_a + timedelta(hours=2), "_value": 2.0}],
    ]))
    cov = await ic.query_coverage("river_level", START, END, field="height_m",
                                  filters={"station_id": ["P555001001"]}, group_tag="station_id")
    assert cov == {"P555001001": {"count": 15, "first": datetime(2024, 2, 10, 3)}}


async def test_query_coverage_returns_none_on_influx_error(monkeypatch):
    async def boom(query):
        raise ConnectionError("influx down")
    monkeypatch.setattr(ic, "_run_query", boom)
    assert await ic.query_coverage("tide", START, END) is None


# --- non-blocking execution -----------------------------------------------------

async def test_run_query_does_not_block_event_loop(monkeypatch):
    class SlowQueryApi:
        def query(self, query, org=None):
            time.sleep(0.2)  # blocking HTTP call of the sync client
            return []

    monkeypatch.setattr(ic, "get_query_api", lambda: SlowQueryApi())
    import asyncio

    t0 = time.perf_counter()
    await asyncio.gather(*(ic._run_query("q") for _ in range(4)))
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.6, f"queries ran sequentially ({elapsed:.2f}s)"


async def test_run_write_uses_worker_thread(monkeypatch):
    import threading

    seen = {}

    class FakeWriteApi:
        def write(self, bucket, org, record):
            seen["thread"] = threading.current_thread()
            seen["record"] = record

    monkeypatch.setattr(ic, "get_write_api", lambda: FakeWriteApi())
    ok = await ic.write_tide("LIBOURNE", 2.5, datetime(2024, 1, 1))
    assert ok is True
    assert seen["thread"] is not threading.main_thread()
