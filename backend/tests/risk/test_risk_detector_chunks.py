"""Episode detection in bounded-memory chunks: same result as a single pass."""

import random
import re
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from app.services import episode_detector, influx_client
from app.services.episode_detector import (
    DETECTION_CHUNK_DAYS,
    _median_from_counts,
    detection_chunks,
    run_episode_detection,
)

START = datetime(2024, 1, 1, 0, 0)
END = datetime(2024, 2, 1, 0, 0)
PESSAC = "P555001001"   # red 4.5 m
BERGERAC = "P514001001"
LIBOURNE = "P577001001"  # tidal, weight 0


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc)


class _Record:
    def __init__(self, values: dict):
        self.values = values

    def get_time(self):
        return self.values["_time"]

    def get_value(self):
        return self.values["_value"]

    def get_field(self):
        return self.values.get("_field")


class _Table:
    def __init__(self, records):
        self.records = records


def _risk_points() -> list[dict]:
    """6-hourly backfill for 5 days, then hourly; two episodes on chunk bounds."""
    scores: dict[datetime, float] = {}
    t = START
    while t < START + timedelta(days=5):
        scores[t] = 10.0
        t += timedelta(hours=6)
    while t < END:
        scores[t] = 10.0
        t += timedelta(hours=1)
    # Episode A across 2024-01-08 00:00 (7-day chunk bound): 8 orange hours, 3 red
    for h in range(-4, 4):
        scores[datetime(2024, 1, 8) + timedelta(hours=h)] = 40.0
    for h in (-1, 0, 1):
        scores[datetime(2024, 1, 8) + timedelta(hours=h)] = 62.0
    # Episode B across 2024-01-15 00:00: two orange bursts 20 h apart (merged,
    # gap < 24 h) with sub-threshold points inside the span
    for h in range(-8, -2):
        scores[datetime(2024, 1, 15) + timedelta(hours=h)] = 45.0
    for h in range(18, 21):
        scores[datetime(2024, 1, 15) + timedelta(hours=h)] = 50.0

    points = []
    for ts, score in scores.items():
        points.append({"_time": _utc(ts), "_value": score, "model_version": "3.4", "source": "backfill"})
    # Realtime points win over backfill within an hour (also on a chunk bound)
    for ts in (datetime(2024, 1, 7, 23, 0), datetime(2024, 1, 8, 0, 0)):
        points.append({"_time": _utc(ts + timedelta(minutes=5)), "_value": 70.0,
                       "model_version": "3.4", "source": "realtime"})
    # Shadow series: must never be read by the detector
    for h in range(48):
        ts = datetime(2024, 1, 20) + timedelta(hours=h)
        points.append({"_time": _utc(ts), "_value": 95.0, "model_version": "3.4-gate", "source": "realtime"})
    return points


def _river_points() -> list[dict]:
    """15-min levels; Pessac above red across 2024-01-22 00:00 with a short dip."""
    points = []
    t = START
    while t < END:
        pessac = 3.0
        if datetime(2024, 1, 21, 18) <= t < datetime(2024, 1, 21, 23):
            pessac = 4.8
        elif datetime(2024, 1, 22, 6) <= t < datetime(2024, 1, 22, 10):
            pessac = 5.1   # back above red < 12 h later: same episode
        points.append({"_time": _utc(t), "_value": pessac, "station_id": PESSAC})
        points.append({"_time": _utc(t), "_value": 2.0, "station_id": BERGERAC})
        points.append({"_time": _utc(t), "_value": 9.0, "station_id": LIBOURNE})
        t += timedelta(minutes=15)
    return points


_RANGE_RE = re.compile(r"range\(start: ([0-9T:.\-]+)Z, stop: ([0-9T:.\-]+)Z\)")


@pytest.fixture
def fake_influx(monkeypatch):
    """Serve synthetic data through influx_client._run_query, honouring the
    query range, the station filter and the model_version filter."""
    risk = _risk_points()
    river = _river_points()
    calls: list[tuple[str, datetime, datetime]] = []

    async def run_query(query: str):
        m = _RANGE_RE.search(query)
        if not m:
            return []
        start = _utc(datetime.fromisoformat(m.group(1)))
        stop = _utc(datetime.fromisoformat(m.group(2)))

        def in_range(p):
            return start <= p["_time"] < stop

        if '"risk_score"' in query:
            calls.append(("risk", start, stop))
            recs = [
                p for p in risk if in_range(p)
                and (p["model_version"] is None or f'r.model_version == "{p["model_version"]}"' in query)
            ]
        elif '"river_level"' in query:
            compact = 'keep(columns: ["_time", "_value", "station_id"])' in query
            calls.append(("river" if compact else "river_full", start, stop))
            recs = [
                p for p in river if in_range(p)
                and (not compact or f'r.station_id == "{p["station_id"]}"' in query)
            ]
        else:
            return []  # rainfall / tides: no context data
        return [_Table([_Record(dict(p)) for p in recs])]

    monkeypatch.setattr(influx_client, "_run_query", run_query)
    return calls


async def test_detector_chunked_equals_single_pass(fake_influx):
    single = await run_episode_detection(START, END, chunk_days=365)
    assert [c for c in fake_influx if c[0] in ("risk", "river")] == [
        ("risk", _utc(START), _utc(END)), ("river", _utc(START), _utc(END)),
    ]

    for chunk_days in (7, 1, 0.25):
        fake_influx.clear()
        chunked = await run_episode_detection(START, END, chunk_days=chunk_days)
        assert chunked == single, chunk_days
        assert len([c for c in fake_influx if c[0] == "risk"]) > 4

    eps = sorted(single["episodes"], key=lambda e: e["start_time"])
    assert [(e["start_time"], e["end_time"], e["detection_method"]) for e in eps] == [
        # A: realtime 70 wins over backfill in the two hours around the bound
        ("2024-01-07T20:00:00", "2024-01-08T03:00:00", "risk_score"),
        # B: two bursts merged across the 01-15 bound
        ("2024-01-14T16:00:00", "2024-01-15T20:00:00", "risk_score"),
        # Pessac above red across the 01-22 bound, dip < 12 h
        ("2024-01-21T18:00:00", "2024-01-22T09:45:00", "river_threshold"),
    ]
    assert eps[0]["peak_score"] == 70.0
    assert eps[0]["peak_time"] == "2024-01-07T23:05:00"
    # Global median step (hourly) even though the first chunk is 6-hourly
    # Global median step (1 h) although the first chunks are 6-hourly: 7 h + 1 h
    assert [e["duration_hours"] for e in eps] == [8.0, 29.0, 15.75]
    # The 3.4-gate shadow series (95 for two days) never shows up
    assert all(e["peak_score"] < 95 for e in eps)
    # Libourne (tidal) is not even queried
    assert all(LIBOURNE not in e["max_river_levels"] for e in eps)


def test_detector_chunks_contiguous_and_hour_aligned():
    start = datetime(2022, 11, 1, 7, 30)
    end = datetime(2026, 10, 26, 12, 0)
    chunks = detection_chunks(start, end)
    assert chunks[0][0] == start and chunks[-1][1] == end
    for (a, b), (c, _) in zip(chunks, chunks[1:]):
        assert b == c                               # no gap, no overlap
        assert b.minute == 0 and b.second == 0      # hour buckets never split
    assert all(b - a <= timedelta(days=DETECTION_CHUNK_DAYS) for a, b in chunks)
    assert detection_chunks(end, start) == []
    # Aware bounds are converted to UTC
    paris = timezone(timedelta(hours=2))
    assert detection_chunks(datetime(2024, 1, 1, 2, tzinfo=paris), datetime(2024, 1, 2, tzinfo=paris))[0][0] \
        == datetime(2024, 1, 1, 0, 0)
    with pytest.raises(ValueError):
        detection_chunks(start, end, chunk_days=0)


async def test_detector_long_range_triggers_chunked_queries(monkeypatch):
    calls = {"risk": [], "river": []}

    async def q_scores(start, end, model_version=None):
        calls["risk"].append((start, end))
        return []

    async def q_heights(start, end, station_ids):
        assert LIBOURNE not in station_ids
        calls["river"].append((start, end))
        return {}

    async def forbidden(*args, **kwargs):
        raise AssertionError("full-range full-resolution query")

    monkeypatch.setattr(influx_client, "query_risk_score_values", q_scores)
    monkeypatch.setattr(influx_client, "query_station_heights", q_heights)
    monkeypatch.setattr(influx_client, "query_risk_scores", forbidden)
    monkeypatch.setattr(influx_client, "query_all_stations_levels", forbidden)

    start, end = datetime(2022, 11, 1), datetime(2026, 10, 26)
    result = await run_episode_detection(start, end)
    assert result["episodes_count"] == 0

    expected = -(-(end - start).days // DETECTION_CHUNK_DAYS)
    for kind in ("risk", "river"):
        windows = calls[kind]
        assert len(windows) == expected > 40
        assert windows[0][0] == start and windows[-1][1] == end
        assert all(b - a <= timedelta(days=DETECTION_CHUNK_DAYS) for a, b in windows)
        assert all(b == c for (_, b), (c, _) in zip(windows, windows[1:]))


def test_detector_median_from_counts_matches_statistics():
    rng = random.Random(3)
    for n in (1, 2, 3, 10, 101):
        values = [rng.choice([1.0, 3.0, 6.0, 0.25, 2.5]) for _ in range(n)]
        assert _median_from_counts(Counter(values)) == statistics.median(values)
    assert _median_from_counts(Counter()) == 1.0


async def test_detector_lean_queries_flux(monkeypatch):
    queries = []

    async def run_query(query):
        queries.append(query)
        return []

    monkeypatch.setattr(influx_client, "_run_query", run_query)
    await influx_client.query_risk_score_values(START, END)
    q = queries[-1]
    assert 'r._field == "score"' in q and "pivot(" not in q
    assert '(not exists r.model_version) or r.model_version == "3.4"' in q
    assert "-gate" not in q and '"3.3"' not in q

    await influx_client.query_station_heights(START, END, [PESSAC])
    q = queries[-1]
    assert 'keep(columns: ["_time", "_value", "station_id"])' in q
    assert f'r.station_id == "{PESSAC}"' in q and LIBOURNE not in q
    with pytest.raises(influx_client.InvalidQueryParameter):
        await influx_client.query_station_heights(START, END, ['x") or true or ("'])


async def test_detector_chunk_query_error_propagates(monkeypatch):
    async def boom(query):
        raise RuntimeError("influx down")

    monkeypatch.setattr(influx_client, "_run_query", boom)
    with pytest.raises(RuntimeError):
        await episode_detector.detect_episodes_from_river_levels(START, END)
