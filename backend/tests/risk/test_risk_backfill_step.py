"""Backfill step and windows: hourly by default, scores independent of both."""

import math
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import episodes as episodes_api
from app.config import get_settings
from app.services import influx_client, risk_core, risk_v2
from app.services.risk_core import StationConfig, build_historical_features, compute_risk

START = datetime(2024, 2, 1, 0, 0)
DAYS = 5
HALF_TIDE = timedelta(hours=6, minutes=12, seconds=30)


def _levels(base: float, amp: float, phase_h: float, trend_cm_h: float) -> list[tuple[datetime, float]]:
    """15-min series over the whole period plus 1 day of lead-in (tidal-like wave + trend)."""
    pts = []
    t = START - timedelta(days=1)
    end = START + timedelta(days=DAYS, hours=2)
    while t <= end:
        h = (t - START).total_seconds() / 3600
        pts.append((t, base + amp * math.sin(2 * math.pi * (h + phase_h) / 12.42) + trend_cm_h / 100 * h))
        t += timedelta(minutes=15)
    return pts


def _tide_rows() -> list[dict]:
    """PM/BM events every ~6h12 (height + coefficient rows, as query_tides returns them)."""
    rows = []
    t = START - timedelta(days=4)
    k = 0
    while t <= START + timedelta(days=DAYS + 1):
        high = k % 2 == 0
        rows.append({"timestamp": t.replace(tzinfo=timezone.utc), "height_m": 2.0 if high else -2.0,
                     "coefficient": None, "tide_type": "PM" if high else "BM", "port": "LIDOIRE"})
        if high:
            rows.append({"timestamp": t.replace(tzinfo=timezone.utc), "height_m": None,
                         "coefficient": 60 + (k * 7) % 55, "tide_type": "PM", "port": "LIDOIRE"})
        t += HALF_TIDE
        k += 1
    return rows


class _WindowedInflux:
    """influx_client stand-in that honours the requested time windows."""

    def __init__(self):
        self.points = {
            "P555001001": _levels(2.9, 0.5, 0.0, 0.6),
            "P514001001": _levels(2.2, 0.1, 3.0, 0.3),
            "P726151001": _levels(1.8, 0.05, 1.0, 0.4),
            "P704151001": _levels(1.6, 0.0, 0.0, -0.2),
            "P846251001": _levels(1.7, 0.05, 2.0, 0.4),
            "P816251001": _levels(0.9, 0.0, 0.0, 0.1),
        }
        self.rain_rows = [
            {"timestamp": START + timedelta(hours=k), "precipitation_mm_h": 0.1 + (k % 7) * 0.1,
             "source": "open-meteo", "city": "Moulin"}
            for k in range(-200, 24 * DAYS + 2)
        ]
        self.tide_rows = _tide_rows()
        self.writes: list[dict] = []
        self.batches = 0
        self.queries: list[tuple[datetime, datetime]] = []

    async def query_river_levels(self, station_id, start, end):
        self.queries.append((start, end))
        return [
            {"timestamp": ts.replace(tzinfo=timezone.utc), "height_m": h, "station_id": station_id}
            for ts, h in self.points.get(station_id, []) if start <= ts < end
        ]

    async def query_rainfall(self, start, end, source=None):
        return [r for r in self.rain_rows if start <= r["timestamp"] < end]

    async def query_tides(self, port, start, end):
        return [
            r for r in self.tide_rows
            if port == "LIDOIRE" and start <= r["timestamp"].replace(tzinfo=None) < end
        ]

    async def write_points_batch(self, points, chunk_size=5000):
        for p in points:
            d = influx_client.point_as_dict(p)
            self.writes.append({
                "measurement": d["measurement"],
                "timestamp": d["timestamp"],
                "score": d["fields"]["score"],
                "fields": d["fields"],
            })
        self.batches += 1
        return len(points)


@pytest.fixture
def influx(monkeypatch):
    stub = _WindowedInflux()
    for name in ("query_river_levels", "query_rainfall", "query_tides", "write_points_batch"):
        monkeypatch.setattr(influx_client, name, getattr(stub, name))
    return stub


def _key(w: dict) -> tuple:
    return w["measurement"], w["timestamp"]


def _scores(stub, measurement="risk_score") -> dict[datetime, float]:
    return {w["timestamp"]: w["score"] for w in stub.writes if w["measurement"] == measurement}


async def test_risk_backfill_is_hourly_on_long_ranges(influx):
    # Used to be 6-hourly beyond 30 days and 3-hourly beyond 7 days
    end = START + timedelta(days=DAYS)
    stored = await risk_v2.backfill_risk_scores(START, end)
    ts = sorted(_scores(influx))
    assert stored == len(ts) == DAYS * 24 + 1
    assert all(b - a == timedelta(hours=1) for a, b in zip(ts, ts[1:]))


async def test_risk_backfill_scores_do_not_depend_on_windows(influx):
    end = START + timedelta(days=DAYS)
    await risk_v2.backfill_risk_scores(START, end, shadow=True)
    one_window = sorted(influx.writes, key=_key)
    assert influx.batches == 2  # one window: one risk batch + one shadow batch

    influx.writes.clear()
    influx.batches = 0
    await risk_v2.backfill_risk_scores(START, end, shadow=True, chunk_days=0.75)
    assert sorted(influx.writes, key=_key) == one_window  # same points, same fields
    assert influx.batches == 2 * math.ceil((DAYS * 24 + 1) / 18)


async def test_risk_backfill_step_only_selects_hours(influx):
    end = START + timedelta(days=DAYS)
    await risk_v2.backfill_risk_scores(START, end, step_hours=1, chunk_days=1)
    hourly = _scores(influx)
    influx.writes.clear()
    await risk_v2.backfill_risk_scores(START + timedelta(minutes=20), end, step_hours=6, chunk_days=2)
    six_hourly = _scores(influx)
    assert sorted(six_hourly) == [START + timedelta(hours=1 + 6 * k) for k in range(20)]
    assert all(six_hourly[t] == hourly[t] for t in six_hourly)


async def test_risk_backfill_scores_equal_direct_compute_risk(influx):
    """Every stored v3.4 score is compute_risk() of the features built from all the data."""
    end = START + timedelta(days=DAYS)
    await risk_v2.backfill_risk_scores(START, end, chunk_days=1)
    settings = get_settings()
    configs = [StationConfig.from_settings_dict(c) for c in settings.hubeau_stations if c["weight"] > 0]
    rain = risk_v2.rain_hourly_from_records(influx.rain_rows)
    highs, coefs = risk_v2.tide_inputs_from_records(influx.tide_rows)
    scores = _scores(influx)
    for t in list(scores)[::7]:
        feats = build_historical_features(
            t, configs, influx.points, settings.hubeau_primary_station, rain,
            risk_core.tide_at(t, highs, coefficient_records=coefs),
            max_age_hours=settings.risk_level_max_age_hours,
            rate_window_h=settings.risk_rate_window_hours,
        )
        assert scores[t] == compute_risk(feats).score


async def test_risk_backfill_loads_inputs_per_window(influx):
    end = START + timedelta(days=DAYS)
    await risk_v2.backfill_risk_scores(START, end, chunk_days=1)
    spans = {(s, e) for s, e in influx.queries}
    assert len(spans) == DAYS + 1
    # Each window only loads its own span plus the lookback
    assert max(e - s for s, e in spans) < timedelta(days=1, hours=16)


@pytest.mark.parametrize("step", [0, 25])
async def test_risk_backfill_rejects_invalid_step(influx, step):
    with pytest.raises(ValueError):
        await risk_v2.backfill_risk_scores(START, START + timedelta(hours=3), step_hours=step)


# --- POST /api/episodes/backfill exposes the step ---------------------------


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(episodes_api.router, prefix="/api/episodes")
    return TestClient(app)


@pytest.mark.parametrize("step", [0, 25])
def test_risk_backfill_route_validates_step(client, step):
    resp = client.post("/api/episodes/backfill", params={"risk_step_hours": step})
    assert resp.status_code == 422


async def test_risk_backfill_task_forwards_step(monkeypatch):
    seen = {}

    async def fake_full_backfill(**kwargs):
        seen.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(episodes_api, "run_full_backfill", fake_full_backfill)
    await episodes_api._run_backfill_task(None, None, True, True, True, True, False, risk_step_hours=3)
    assert seen["risk_step_hours"] == 3
    assert episodes_api._backfill_status["result"] == {"ok": True}
