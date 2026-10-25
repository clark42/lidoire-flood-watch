"""Episode detector (hours, consecutive, Libourne) and risk-score persistence."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import episode_detector, influx_client
from app.services.episode_detector import hourly_rain_total, longest_run_hours

T = datetime(2024, 2, 10, 0, 0)


def series(values, step_h=1.0):
    return [(T + timedelta(hours=i * step_h), v) for i, v in enumerate(values)]


def test_risk_detector_counts_hours_not_samples():
    # 6-hourly backfill: ONE red sample is 6h of red, not "1 hour"
    assert longest_run_hours(series([10, 60, 10], step_h=6), 55) == pytest.approx(6.0)
    # Hourly: two consecutive red samples = 2h
    assert longest_run_hours(series([10, 60, 60, 10]), 55) == pytest.approx(2.0)


def test_risk_detector_requires_consecutive_points():
    # Three red samples but never consecutive -> longest run 1h
    assert longest_run_hours(series([60, 10, 60, 10, 60]), 55) == pytest.approx(1.0)
    # A gap in the series breaks the run
    gap = [(T, 60), (T + timedelta(hours=1), 60), (T + timedelta(hours=5), 60), (T + timedelta(hours=6), 60)]
    assert longest_run_hours(gap, 55, step_hours=1.0) == pytest.approx(2.0)
    assert longest_run_hours([], 55) == 0.0


def test_risk_detector_rain_mean_per_hour_then_sum():
    rows = []
    for h in range(3):
        hour_end = T + timedelta(hours=h + 1)
        # Netatmo: 4 sub-hourly samples of a 2 mm/h rate; Open-Meteo: 2 mm
        for q in range(4):
            rows.append({"timestamp": hour_end - timedelta(minutes=15 * q), "precipitation_mm_h": 2.0,
                         "source": "netatmo-lidoire", "city": "A"})
        rows.append({"timestamp": hour_end, "precipitation_mm_h": 2.0, "source": "open-meteo", "city": "Moulin"})
    total = hourly_rain_total(rows, T, T + timedelta(hours=3))
    assert total == pytest.approx(6.0)  # not 30 (sum of all samples)


async def test_risk_detector_episode_and_libourne_excluded(monkeypatch):
    scores = [10] * 3 + [40] * 3 + [60] * 3 + [20] * 30
    rows = [
        {"timestamp": (T + timedelta(hours=i)).replace(tzinfo=timezone.utc), "score": s,
         "level": None, "model_version": "3.3"}
        for i, s in enumerate(scores)
    ]

    async def q_scores(start, end, model_version=None):
        return rows

    async def q_levels(start, end):
        base = [{"timestamp": (T + timedelta(hours=i)).replace(tzinfo=timezone.utc), "height_m": 2.0}
                for i in range(10)]
        return {
            "P577001001": [dict(r, height_m=9.0) for r in base],  # Libourne way above red
            "P555001001": base,
        }

    async def q_rain(start, end, source=None):
        return []

    async def q_tides(port, start, end):
        return []

    monkeypatch.setattr(influx_client, "query_risk_scores", q_scores)
    monkeypatch.setattr(influx_client, "query_all_stations_levels", q_levels)
    monkeypatch.setattr(influx_client, "query_rainfall", q_rain)
    monkeypatch.setattr(influx_client, "query_tides", q_tides)

    eps = await episode_detector.detect_episodes_from_risk_scores(T, T + timedelta(days=2))
    assert len(eps) == 1
    ep = eps[0]
    assert ep.peak_score == 60 and ep.peak_level == "red"
    assert "P577001001" not in ep.max_river_levels

    river_eps = await episode_detector.detect_episodes_from_river_levels(T, T + timedelta(days=2))
    assert river_eps == []  # Libourne (tidal) never triggers an episode

    # 2 red hours needed: with only 1 consecutive red hour no episode
    rows[7]["score"] = 20
    rows[8]["score"] = 20
    eps = await episode_detector.detect_episodes_from_risk_scores(T, T + timedelta(days=2), min_orange_hours=6)
    assert eps == []


# --- persistence ---------------------------------------------------------------

class _WriteApi:
    def __init__(self):
        self.records = []

    def write(self, bucket, org, record):
        self.records.append(record)


def test_risk_write_risk_score_level_field_and_version_tag(monkeypatch):
    api = _WriteApi()
    monkeypatch.setattr(influx_client, "get_write_api", lambda: api)
    import asyncio

    ok = asyncio.run(influx_client.write_risk_score(
        score=42.0, level="orange", timestamp=T, factors={"momentum": 0.5, "tide_available": True},
        model_version="3.3",
    ))
    assert ok
    line = api.records[0].to_line_protocol()
    measurement_and_tags = line.split(" ")[0]
    assert measurement_and_tags == "risk_score,model_version=3.3"   # level is NOT a tag
    assert 'level="orange"' in line
    assert "score=42" in line
    assert "factor_momentum=0.5" in line
    assert "factor_tide_available=1" in line


def test_risk_select_rows_prefers_current_version():
    ts1 = T
    ts2 = T + timedelta(hours=1)
    rows = [
        {"timestamp": ts1, "score": 10, "model_version": None},      # legacy (v2.5/v3.2)
        {"timestamp": ts1, "score": 20, "model_version": "3.3"},
        {"timestamp": ts1, "score": 30, "model_version": "3.4"},
        {"timestamp": ts2, "score": 15, "model_version": None},
        {"timestamp": ts2, "score": 16, "model_version": "legacy"},
    ]
    out = influx_client.select_risk_rows_by_version(rows, "3.3")
    assert [(r["timestamp"], r["score"]) for r in out] == [(ts1, 20), (ts2, 15)]
    newest = influx_client.select_risk_rows_by_version(rows[:3], "9.9")
    assert newest[0]["score"] == 30


def test_risk_query_risk_scores_reads_old_and_new_points(monkeypatch):
    """Pivoted rows: legacy (level tag, no version) and v3.3 (level field + version)."""
    def rec(ts, values):
        return SimpleNamespace(values=values, get_time=lambda: ts)

    ts_old = T.replace(tzinfo=timezone.utc)
    ts_both = (T + timedelta(hours=1)).replace(tzinfo=timezone.utc)
    tables = [
        SimpleNamespace(records=[
            rec(ts_old, {"score": 33.0, "level": "yellow", "factor_rain_24h": 0.1}),
            rec(ts_both, {"score": 50.0, "level": "orange"}),
        ]),
        SimpleNamespace(records=[
            rec(ts_both, {"score": 44.0, "level": "orange", "model_version": "3.3", "factor_momentum": 0.2}),
        ]),
    ]
    captured = {}

    class Q:
        def query(self, query, org=None):
            captured["q"] = query
            return tables

    monkeypatch.setattr(influx_client, "get_query_api", lambda: Q())
    import asyncio

    rows = asyncio.run(influx_client.query_risk_scores(T, T + timedelta(hours=2)))
    assert "pivot(" in captured["q"]
    assert [(r["timestamp"], r["score"], r["model_version"]) for r in rows] == [
        (ts_old, 33.0, None), (ts_both, 44.0, "3.3"),
    ]
    assert rows[0]["level"] == "yellow" and rows[0]["factor_rain_24h"] == 0.1
