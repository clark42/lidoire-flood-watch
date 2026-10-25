"""Netatmo 24h-counter cumulative rainfall: sampling logic, single query, cache."""

from datetime import datetime, timedelta, timezone

import pytest

from app.services import influx_client as ic
from app.services.influx_client import InvalidQueryParameter, sum_daily_counters

NOW = datetime(2024, 2, 15, 12, 0)


def test_sum_daily_counters_samples_non_overlapping_boundaries():
    samples = []
    # hourly counter readings over 4 days; value encodes the day offset
    for h in range(0, 24 * 4):
        ts = NOW - timedelta(hours=h)
        samples.append((ts, float(h // 24 + 1)))  # day0 -> 1, day1 -> 2, ...
    samples.sort()
    assert sum_daily_counters(samples, NOW, 1) == 1.0
    assert sum_daily_counters(samples, NOW, 3) == 1.0 + 2.0 + 3.0


def test_sum_daily_counters_window_tolerance_and_missing_days():
    samples = [
        (NOW - timedelta(hours=2, minutes=30), 4.0),       # day 0, within 3h before
        (NOW - timedelta(hours=24 + 4), 9.0),             # day 1, too old (> 3h before boundary)
        (NOW - timedelta(hours=48) + timedelta(minutes=30), 2.0),  # day 2, within 1h after
    ]
    assert sum_daily_counters(samples, NOW, 3) == 6.0
    assert sum_daily_counters([], NOW, 7) is None


def test_sum_daily_counters_keeps_last_reading_in_window():
    samples = [
        (NOW - timedelta(hours=2), 3.0),
        (NOW - timedelta(minutes=10), 3.4),
    ]
    assert sum_daily_counters(samples, NOW, 1) == 3.4


def test_counter_query_uses_window_last_and_single_range():
    q = ic.build_rainfall_counter_query(
        ["Saint-Rémy", "Flaujagues"], "netatmo-lidoire",
        NOW - timedelta(days=7), NOW,
    )
    assert q.count("from(bucket:") == 1
    assert 'r._field == "precipitation_mm_24h"' in q
    assert q.index("window(every: 1h)") < q.index("last()") < q.index('group(columns: ["city"])')
    assert 'r.city == "Saint-Rémy" or r.city == "Flaujagues"' in q


def test_counter_query_rejects_bad_city():
    with pytest.raises(InvalidQueryParameter):
        ic.build_rainfall_counter_query(['x") |> yield(name: "y'], "netatmo-lidoire", NOW, NOW)


async def test_all_stations_cumulative_single_query_and_cache(capture_queries, fake_tables, monkeypatch):
    queries, set_response = capture_queries

    monkeypatch.setattr(ic, "_utcnow", lambda: NOW)

    def rows(city, values_by_day):
        return [
            {"city": city, "_time": (NOW - timedelta(hours=24 * d, minutes=5)).replace(tzinfo=timezone.utc),
             "_value": v}
            for d, v in values_by_day.items()
        ]

    set_response(fake_tables([
        rows("Flaujagues", {0: 5.0, 1: 3.0, 2: 1.0, 5: 10.0}),
        rows("Castillon-la-Bataille", {0: 2.0}),
    ]))

    cities = ["Flaujagues", "Castillon-la-Bataille", "Lamothe-Montravel"]
    result = await ic.query_all_stations_rainfall_cumulative(cities)

    assert len(queries) == 1, "expected one Flux query for all cities and windows"
    assert result["Flaujagues"] == {"rain_24h_mm": 5.0, "rain_72h_mm": 9.0, "rain_7d_mm": 19.0}
    assert result["Castillon-la-Bataille"] == {"rain_24h_mm": 2.0, "rain_72h_mm": 2.0, "rain_7d_mm": 2.0}
    assert result["Lamothe-Montravel"] == {"rain_24h_mm": None, "rain_72h_mm": None, "rain_7d_mm": None}

    # Cached: no new query, and callers can't mutate the cached value
    result["Flaujagues"]["rain_24h_mm"] = 999
    again = await ic.query_all_stations_rainfall_cumulative(list(reversed(cities)))
    assert len(queries) == 1
    assert again["Flaujagues"]["rain_24h_mm"] == 5.0


async def test_all_stations_cumulative_failure_not_cached(monkeypatch):
    calls = {"n": 0}

    async def boom(query):
        calls["n"] += 1
        raise ConnectionError("down")

    monkeypatch.setattr(ic, "_run_query", boom)
    res = await ic.query_all_stations_rainfall_cumulative(["Flaujagues"])
    assert res == {"Flaujagues": {"rain_24h_mm": None, "rain_72h_mm": None, "rain_7d_mm": None}}
    await ic.query_all_stations_rainfall_cumulative(["Flaujagues"])
    assert calls["n"] == 2


async def test_all_stations_cumulative_skips_unsafe_city_names(capture_queries):
    queries, set_response = capture_queries
    set_response([])
    res = await ic.query_all_stations_rainfall_cumulative(["Flaujagues", 'Evil") |> drop()'])
    assert len(queries) == 1
    assert "Evil" not in queries[0]
    assert res['Evil") |> drop()'] == {"rain_24h_mm": None, "rain_72h_mm": None, "rain_7d_mm": None}
    assert res["Flaujagues"]["rain_24h_mm"] is None


async def test_station_cumulative_single_query(capture_queries, fake_tables):
    queries, set_response = capture_queries
    set_response([])
    assert await ic.query_station_rainfall_cumulative("Flaujagues", 72) is None
    assert len(queries) == 1
