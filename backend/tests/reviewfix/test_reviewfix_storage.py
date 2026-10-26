"""Storage fixes: risk source tag + realtime preference, hour-end rain, tides, batching."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import data_store, influx_client, risk_core, risk_v2
from app.services.collectors import netatmo
from app.services.collectors import tide as tide_collector
from app.models.schemas import RainfallObservation, TideData, TideType

from risk_scenario import build_scenario

T = datetime(2024, 2, 10, 12, 0)
UTC = timezone.utc


class _WriteApi:
    def __init__(self):
        self.records = []

    def write(self, bucket, org, record):
        self.records.append(record)


def _capture_batches(monkeypatch):
    batches = []

    async def fake_batch(points, chunk_size=5000):
        batches.append([influx_client.point_as_dict(p) for p in points])
        return len(points)

    monkeypatch.setattr(influx_client, "write_points_batch", fake_batch)
    return batches


# --- risk_score source tag ---------------------------------------------------


async def test_reviewfix_realtime_score_is_tagged_source_realtime(monkeypatch):
    api = _WriteApi()
    monkeypatch.setattr(influx_client, "get_write_api", lambda: api)
    sc = build_scenario()
    risk = risk_v2.breakdown_to_risk_score(risk_core.compute_risk(
        risk_v2.build_realtime_features(sc.stations_data, sc.tide, sc.rainfall, sc.t0)
    ))
    assert await risk_v2.store_risk_score(risk)
    tags = api.records[0].to_line_protocol().split(" ")[0]
    assert tags == "risk_score,model_version=3.3,source=realtime"


def test_reviewfix_prefer_realtime_rows_per_hour_and_version():
    rows = [
        {"timestamp": T, "score": 30, "model_version": "3.3", "source": "backfill"},
        {"timestamp": T + timedelta(minutes=4), "score": 50, "model_version": "3.3", "source": "realtime"},
        {"timestamp": T + timedelta(hours=1), "score": 31, "model_version": "3.3", "source": "backfill"},
        # untagged (pre-source) point protects its hour like a realtime one
        {"timestamp": T + timedelta(hours=2), "score": 32, "model_version": "3.3", "source": "backfill"},
        {"timestamp": T + timedelta(hours=2, minutes=7), "score": 52, "model_version": "3.3", "source": None},
        # a realtime point of ANOTHER version does not hide a backfill of this one
        {"timestamp": T + timedelta(hours=3), "score": 33, "model_version": "3.3", "source": "backfill"},
        {"timestamp": T + timedelta(hours=3, minutes=2), "score": 53, "model_version": "3.2", "source": "realtime"},
    ]
    kept = influx_client.prefer_realtime_risk_rows(rows)
    assert [r["score"] for r in kept] == [50, 31, 52, 33, 53]
    # Backward compatible: rows without any source are returned unchanged
    legacy = [{"timestamp": T, "score": 1, "model_version": None},
              {"timestamp": T + timedelta(minutes=5), "score": 2, "model_version": None}]
    assert influx_client.prefer_realtime_risk_rows(legacy) == legacy


def test_reviewfix_query_risk_scores_prefers_realtime(monkeypatch):
    def rec(ts, values):
        return SimpleNamespace(values=values, get_time=lambda: ts)

    tables = [
        SimpleNamespace(records=[
            rec(T.replace(tzinfo=UTC), {"score": 20.0, "level": "green", "model_version": "3.3", "source": "backfill"}),
            rec((T + timedelta(hours=1)).replace(tzinfo=UTC),
                {"score": 21.0, "level": "green", "model_version": "3.3", "source": "backfill"}),
        ]),
        SimpleNamespace(records=[
            rec((T + timedelta(minutes=3)).replace(tzinfo=UTC),
                {"score": 60.0, "level": "orange", "model_version": "3.3", "source": "realtime"}),
        ]),
    ]

    class Q:
        def query(self, query, org=None):
            return tables

    monkeypatch.setattr(influx_client, "get_query_api", lambda: Q())
    import asyncio

    rows = asyncio.run(influx_client.query_risk_scores(T, T + timedelta(hours=2)))
    assert [(r["score"], r["source"]) for r in rows] == [(60.0, "realtime"), (21.0, "backfill")]


def test_reviewfix_risk_timeline_groups_by_source_and_prefers_realtime(monkeypatch):
    captured = {}

    def rec(ts, value, version, source):
        return SimpleNamespace(values={"model_version": version, "source": source},
                               get_time=lambda: ts, get_value=lambda: value)

    w1 = (T + timedelta(hours=1)).replace(tzinfo=UTC)
    w2 = (T + timedelta(hours=2)).replace(tzinfo=UTC)
    tables = [
        SimpleNamespace(records=[rec(w1, 20.0, "3.3", "backfill"), rec(w2, 22.0, "3.3", "backfill")]),
        SimpleNamespace(records=[rec(w1, 60.0, "3.3", "realtime")]),
        SimpleNamespace(records=[rec(w2, 99.0, "legacy", "unknown")]),
    ]

    class Q:
        def query(self, query, org=None):
            captured["q"] = query
            return tables

    monkeypatch.setattr(influx_client, "get_query_api", lambda: Q())
    import asyncio

    out = asyncio.run(influx_client.query_risk_scores_timeline(T, T + timedelta(hours=3)))
    assert 'group(columns: ["model_version", "source"])' in captured["q"]
    assert 'else "unknown"' in captured["q"]
    # w1: realtime wins over backfill (same version); w2: current version wins over legacy
    assert [(r["timestamp"], r["value"]) for r in out] == [(w1, 60.0), (w2, 22.0)]


# --- hour-end rain labels ----------------------------------------------------


@pytest.mark.parametrize("resolution,agg", [("1h", "mean"), ("1h", "sum"), ("6h", "sum"), ("15m", "sum")])
def test_reviewfix_rain_queries_shift_hour_end_labels(resolution, agg):
    q = influx_client.build_rainfall_by_station_query(T, T + timedelta(days=1), resolution=resolution, agg_fn=agg)
    assert "range(start: 2024-02-10T12:00:01Z, stop: 2024-02-11T12:00:01Z)" in q
    assert q.index("timeShift(duration: -1s)") < q.index("aggregateWindow")


def test_reviewfix_basin_sparkline_shifts_hour_end_labels():
    q = influx_client.build_basin_sparkline_query(T, T + timedelta(hours=24))
    assert q.index("timeShift(duration: -1s)") < q.index("aggregateWindow(every: 1h")


def test_reviewfix_shift_matches_risk_core_hour_end_convention():
    """Emulate aggregateWindow([start, stop) stamped at stop) after the -1s shift."""
    points = [(T + timedelta(hours=1), 4.0), (T + timedelta(minutes=47), 2.0), (T + timedelta(hours=2), 1.0)]

    def window_stop(ts):
        shifted = ts - timedelta(seconds=1)
        return shifted.replace(minute=0, second=0) + timedelta(hours=1)

    flux_like: dict = {}
    for ts, v in points:
        flux_like.setdefault(window_stop(ts), []).append(v)
    flux_like = {k: sum(v) / len(v) for k, v in flux_like.items()}
    core = risk_core.aggregate_hourly_rain((ts, v, "s") for ts, v in points)
    assert flux_like == core


# --- tides from InfluxDB -----------------------------------------------------


def _tide_rows():
    pm = T - timedelta(hours=2)
    bm = T + timedelta(hours=4, minutes=12)
    rows = [
        {"timestamp": pm.replace(tzinfo=UTC), "height_m": 5.6, "coefficient": None, "tide_type": "PM", "port": "BORDEAUX"},
        {"timestamp": pm.replace(tzinfo=UTC), "height_m": None, "coefficient": 87, "tide_type": "PM", "port": "BORDEAUX"},
        {"timestamp": bm.replace(tzinfo=UTC), "height_m": 0.7, "coefficient": None, "tide_type": "BM", "port": "BORDEAUX"},
    ]
    # untagged hourly heights are ignored
    rows += [{"timestamp": (T + timedelta(hours=k)).replace(tzinfo=UTC), "height_m": 1.0 + k / 10,
              "coefficient": None, "tide_type": None, "port": "BORDEAUX"} for k in range(3)]
    return rows


async def test_reviewfix_historical_tides_read_bordeaux_events(monkeypatch):
    ports = []

    async def fake_query_tides(port, start, end):
        ports.append(port)
        return _tide_rows() if port == "BORDEAUX" else []

    async def no_fetch(*a, **k):
        raise AssertionError("no API fetch when tides are stored")

    monkeypatch.setattr(influx_client, "query_tides", fake_query_tides)
    monkeypatch.setattr(tide_collector, "fetch_and_store_tides_range", no_fetch)

    tides = await data_store.get_historical_tides(T - timedelta(hours=6), T + timedelta(hours=6))
    assert ports[0] == "BORDEAUX"
    assert [(t.tide_type, t.coefficient, t.height_m) for t in tides] == [
        (TideType.HIGH, 87, 5.6), (TideType.LOW, None, 0.7),
    ]
    assert all(t.timestamp.tzinfo is None for t in tides)


async def test_reviewfix_historical_tides_fetch_range_when_nothing_stored(monkeypatch):
    stored = {"rows": []}
    fetched = []

    async def fake_query_tides(port, start, end):
        return stored["rows"] if port == "BORDEAUX" else []

    async def fake_fetch(start, end):
        fetched.append((start, end))
        stored["rows"] = _tide_rows()
        return 24

    monkeypatch.setattr(influx_client, "query_tides", fake_query_tides)
    monkeypatch.setattr(tide_collector, "fetch_and_store_tides_range", fake_fetch)

    start, end = T - timedelta(hours=6), T + timedelta(hours=6)
    tides = await data_store.get_historical_tides(start, end)
    assert fetched == [(start - timedelta(days=1), end + timedelta(days=1))]
    assert [t.tide_type for t in tides] == [TideType.HIGH, TideType.LOW]


async def test_reviewfix_historical_tides_detects_highs_from_heights(monkeypatch):
    heights = [1.0, 2.0, 3.0, 2.5, 1.0, 0.5, 1.5]
    rows = [{"timestamp": (T + timedelta(hours=k)).replace(tzinfo=UTC), "height_m": h,
             "coefficient": None, "tide_type": None, "port": "BORDEAUX"} for k, h in enumerate(heights)]

    async def fake_query_tides(port, start, end):
        return rows if port == "BORDEAUX" else []

    monkeypatch.setattr(influx_client, "query_tides", fake_query_tides)
    tides = await data_store.get_historical_tides(T, T + timedelta(hours=8))
    assert [(t.timestamp, t.tide_type, t.height_m) for t in tides] == [(T + timedelta(hours=2), TideType.HIGH, 3.0)]


# --- batched writes ----------------------------------------------------------


async def test_reviewfix_store_rainfall_is_one_batch(monkeypatch):
    batches = _capture_batches(monkeypatch)
    obs = [RainfallObservation(timestamp=T + timedelta(hours=k), source="netatmo", precipitation_mm_h=0.5 * k,
                               precipitation_mm_24h=3.0) for k in range(4)]
    assert await data_store.store_rainfall(obs) == 4
    assert len(batches) == 1 and len(batches[0]) == 4
    assert batches[0][2]["fields"]["precipitation_mm_h"] == 1.0
    assert await data_store.store_rainfall([]) == 0
    assert len(batches) == 1


async def test_reviewfix_store_tide_data_is_one_batch_and_skips_estimated(monkeypatch):
    batches = _capture_batches(monkeypatch)
    tides = [
        TideData(timestamp=T, port="BORDEAUX", tide_type=TideType.HIGH, height_m=5.0, coefficient=80, source="shom"),
        TideData(timestamp=T + timedelta(hours=6), port="BORDEAUX", tide_type=TideType.LOW, height_m=1.0, source="shom"),
        TideData(timestamp=T + timedelta(hours=12), port="BORDEAUX", tide_type=TideType.HIGH, height_m=5.0,
                 source="estimated"),
    ]
    assert await data_store.store_tide_data(tides) == 2
    assert len(batches) == 1 and len(batches[0]) == 2


async def test_reviewfix_risk_backfill_never_writes_point_by_point(monkeypatch):
    sc = build_scenario()

    async def forbidden(*a, **k):
        raise AssertionError("point-by-point write")

    async def q_levels(station_id, start, end):
        return [{"timestamp": ts, "height_m": h} for ts, h in sc.station_points.get(station_id, [])
                if start <= ts < end]

    async def q_rain(start, end, source=None):
        return [{"timestamp": k, "precipitation_mm_h": v, "source": "open-meteo", "city": "Moulin"}
                for k, v in sc.rain_hourly.items()]

    async def q_tides(port, start, end):
        return sc.tide_rows if port == "BORDEAUX" else []

    batches = _capture_batches(monkeypatch)
    monkeypatch.setattr(influx_client, "write_risk_score", forbidden)
    monkeypatch.setattr(influx_client, "query_river_levels", q_levels)
    monkeypatch.setattr(influx_client, "query_rainfall", q_rain)
    monkeypatch.setattr(influx_client, "query_tides", q_tides)

    assert await risk_v2.backfill_risk_scores(sc.t0 - timedelta(hours=5), sc.t0) == 6
    assert len(batches) == 1
    assert {p["tags"]["source"] for p in batches[0]} == {"backfill"}
    assert {p["tags"]["model_version"] for p in batches[0]} == {"3.3"}


# --- Netatmo coverage query is non-blocking ---------------------------------


async def test_reviewfix_netatmo_coverage_uses_non_blocking_query(monkeypatch):
    queries = []

    async def fake_run_query(query):
        queries.append(query)
        return []

    monkeypatch.setattr(influx_client, "_run_query", fake_run_query)
    # get_query_api is the autouse "boom": a direct synchronous call would fail
    coverage = await netatmo._query_daily_coverage(["Flaujagues"])
    assert coverage == {"Flaujagues": 0}
    assert f"from(bucket: {influx_client._bucket()})" in queries[0]
    assert 'r.source == "netatmo-lidoire"' in queries[0]
