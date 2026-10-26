"""Shadow model 4.0a: own measurement, never read as the displayed score.

A tiny Flux emulator serves stored points filtered by the exact measurement
named in each query (as InfluxDB does), ignoring every other filter: the
client-side defenses are exercised too, with a 4.0a point even (wrongly)
stored in ``risk_score``.
"""

import pathlib
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI

from app.api import timeline
from app.services import episode_detector, influx_client, risk_shadow_v4
from app.services.risk_shadow import SHADOW_MODEL_VERSION
from app.services.risk_v4 import V4Features

T = datetime(2024, 2, 15, 10)
V4 = risk_shadow_v4.SHADOW_V4_MODEL_VERSION


def _v34(ts, score, source="realtime"):
    return influx_client.point_as_dict(influx_client.make_risk_point(
        score, "green" if score < 15 else "red", ts, {"rain_24h": 0.1}, "3.4", source,
    ))


def _v4(ts, score_inputs=(6.8, 40.0), source="realtime", measurement=influx_client.RISK_SHADOW_MEASUREMENT):
    """A RED 4.0a point (Pessac 6.8 m, Eyraud 40 m3/s)."""
    h, q = score_inputs
    p = risk_shadow_v4.shadow_v4_point(ts, V4Features(backwater_level_m=h, runoff_q_m3s=q), source)
    d = influx_client.point_as_dict(p)
    d["measurement"] = measurement
    return d


def _gate(ts, score, source="realtime"):
    return influx_client.point_as_dict(influx_client.make_risk_point(
        score, "yellow", ts, {"gate": 0.5}, SHADOW_MODEL_VERSION, source,
        measurement=influx_client.RISK_SHADOW_MEASUREMENT,
    ))


class _Flux:
    """Stored points; a query sees the points of the measurements it names (exact match)."""

    def __init__(self, fake_tables, points):
        self.fake_tables = fake_tables
        self.points = points
        self.queries: list[str] = []

    def tables(self, query):
        self.queries.append(query)
        names = set(re.findall(r'r\._measurement == "([A-Za-z_]+)"', query))
        sel = [p for p in self.points if p["measurement"] in names]
        if "pivot(" in query:
            recs = [{"_time": p["timestamp"].replace(tzinfo=timezone.utc), "_measurement": p["measurement"],
                     **p["tags"], **p["fields"]} for p in sel]
            return self.fake_tables([recs])
        m = re.search(r'r\._field == "([a-z_]+)"', query)
        recs = [
            {"_time": p["timestamp"].replace(tzinfo=timezone.utc), "_value": v, "_field": f,
             "_measurement": p["measurement"], **p["tags"]}
            for p in sel for f, v in p["fields"].items()
            if (m is None or f == m.group(1)) and not isinstance(v, str)
        ]
        return self.fake_tables([recs])

    async def run_query(self, query):
        return self.tables(query)

    def query(self, query, org=None):  # synchronous query API (TimeMachine snapshot)
        return self.tables(query)


@pytest.fixture
def flux(monkeypatch, fake_tables):
    def _make(points):
        f = _Flux(fake_tables, points)
        monkeypatch.setattr(influx_client, "_run_query", f.run_query)
        monkeypatch.setattr(influx_client, "get_query_api", lambda: f)
        return f
    return _make


def _mixed_store(hours=12):
    """Green v3.4 every hour, RED 4.0a every hour (own measurement), a 3.4-gate,
    and a RED 4.0a wrongly stored in risk_score at hours without any v3.4 point."""
    pts = []
    for i in range(hours):
        ts = T + timedelta(hours=i)
        pts += [_v34(ts, 5.0), _v4(ts), _gate(ts, 2.5)]
    for i in range(hours, hours + 8):
        pts.append(_v4(T + timedelta(hours=i), measurement="risk_score"))
    return pts


def test_shadow_v4_version_is_a_shadow_version():
    assert risk_shadow_v4.SHADOW_V4_MODEL_VERSION == "4.0a"
    for v in ("4.0a", "3.4-gate", "3.3-gate", "4.0a-x", "4.0b", "v3.4", "3.4 "):
        assert influx_client.is_shadow_risk_version(v), v
    # purely numeric versions are production versions (a future "4.0" included)
    for v in ("3.4", "3.3", "3.2", "4.0", "4", None, "", "legacy"):
        assert not influx_client.is_shadow_risk_version(v), v


def test_shadow_v4_point_shape():
    d = _v4(T)
    assert d["measurement"] == "risk_score_shadow"
    assert d["tags"] == {"model_version": "4.0a", "source": "realtime"}
    f = d["fields"]
    assert f["level"] == "red" and f["score"] >= 55 and f["dominant"] in ("joint", "overbank")
    for k in ("backwater", "runoff", "overbank"):
        assert f[f"factor_{k}_available"] == 1.0 and f"factor_{k}" in f
    assert f["factor_groundwater_available"] == 0.0 and "factor_groundwater" not in f  # off in v4a_joint
    assert f["factor_h_aval_m"] == 6.8 and f["factor_q_eyraud_m3s"] == 40.0
    assert "factor_piezo_ngf" not in f
    # missing runoff: overbank only, recorded as unavailable
    p = risk_shadow_v4.shadow_v4_point(T, V4Features(backwater_level_m=3.2), "backfill")
    f = influx_client.point_as_dict(p)["fields"]
    assert f["factor_runoff_available"] == 0.0 and "factor_joint" not in f and f["dominant"] == "overbank"
    # nothing to score: no point
    assert risk_shadow_v4.shadow_v4_point(T, V4Features(runoff_q_m3s=30.0), "realtime") is None


async def test_displayed_score_readers_never_return_4_0a(flux):
    f = flux(_mixed_store())
    end = T + timedelta(hours=24)
    rows = await influx_client.query_risk_scores(T, end)
    assert {r["model_version"] for r in rows} == {"3.4"} and len(rows) == 12
    values = await influx_client.query_risk_score_values(T, end)
    assert {r["model_version"] for r in values} == {"3.4"} and max(r["score"] for r in values) == 5.0
    assert '"4.0a"' not in f.queries[-1]  # server-side version filter: current version and legacy only
    timeline_rows = await influx_client.query_risk_scores_timeline(T, end)
    assert [r["value"] for r in timeline_rows] == [5.0] * 12
    for q in f.queries:
        assert "risk_score_shadow" not in q


async def test_shadow_timeline_series_are_separated_by_version(flux):
    flux(_mixed_store())
    end = T + timedelta(hours=24)
    v4 = await influx_client.query_risk_shadow_timeline(T, end, V4)
    gate = await influx_client.query_risk_shadow_timeline(T, end, SHADOW_MODEL_VERSION)
    assert len(v4) == 12 and min(r["value"] for r in v4) >= 55
    assert [r["value"] for r in gate] == [2.5] * 12


async def test_timemachine_snapshot_never_returns_4_0a(flux):
    f = flux(_mixed_store())
    snap = await influx_client.query_historical_snapshot(T + timedelta(hours=14), window_hours=6)
    scores = [s["score"] for s in snap["risk_scores"] if s["score"] is not None]
    assert scores and set(scores) == {5.0}
    assert "risk_score_shadow" not in f.queries[-1]


async def test_episode_detection_never_uses_4_0a(flux, monkeypatch):
    async def enrich(ep):
        return ep

    monkeypatch.setattr(episode_detector, "_enrich_episode", enrich)
    flux(_mixed_store(hours=24))
    found = await episode_detector.detect_episodes_from_risk_scores(T - timedelta(hours=1), T + timedelta(days=2))
    assert found == []  # 4.0a is red for 32 h, v3.4 green

    # control: the same detection on a red v3.4 finds the episode
    flux([_v34(T + timedelta(hours=i), 70.0) for i in range(12)])
    assert len(await episode_detector.detect_episodes_from_risk_scores(T - timedelta(hours=1),
                                                                      T + timedelta(days=2))) == 1


def test_risk_score_shadow_is_read_only_by_the_shadow_timeline():
    app_dir = pathlib.Path(influx_client.__file__).resolve().parents[1]
    readers = []
    for p in app_dir.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        if "RISK_SHADOW_MEASUREMENT" in text or '"risk_score_shadow"' in text:
            readers.append(p.name)
    # writers (risk_v2, risk_shadow_v4), the timeline series type, a comment in risk_shadow
    assert set(readers) == {"influx_client.py", "risk_v2.py", "risk_shadow.py", "risk_shadow_v4.py", "timeline.py"}
    text = (app_dir / "services" / "influx_client.py").read_text(encoding="utf-8")
    # only the timeline helper queries the shadow measurement
    assert text.count("_query_risk_timeline_rows(RISK_SHADOW_MEASUREMENT") == 1


# --- timeline endpoint -------------------------------------------------------------------

@pytest.fixture
def app_client():
    app = FastAPI()
    app.include_router(timeline.router, prefix="/api/timeline")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_unified_shadow_v4_series_is_opt_in(app_client, monkeypatch):
    calls = []

    async def fake_shadow(start, end, model_version, resolution="1h", agg_fn="mean"):
        calls.append(model_version)
        return [{"timestamp": datetime(2024, 2, 15, 10), "value": 61.0 if model_version == V4 else 8.5}]

    monkeypatch.setattr(timeline, "query_risk_shadow_timeline", fake_shadow)
    params = {"start": "2024-02-14T00:00:00", "end": "2024-02-15T12:00:00", "include_infoclimat": "false"}

    async with app_client as client:
        off = await client.get("/api/timeline/unified", params=params)
        gate = await client.get("/api/timeline/unified", params={**params, "include_risk_shadow": "true"})
        v4 = await client.get("/api/timeline/unified", params={**params, "include_risk_shadow_v4": "true"})
    assert off.status_code == gate.status_code == v4.status_code == 200
    assert not {"risk:shadow", "risk:shadow_v4"} & set(off.json()["series"])
    assert "risk:shadow_v4" not in gate.json()["series"] and "risk:shadow" in gate.json()["series"]
    series = v4.json()["series"]
    assert "risk:shadow" not in series and "risk:score" not in series
    assert series["risk:shadow_v4"]["type"] == "risk_score_shadow"
    assert [p["value"] for p in series["risk:shadow_v4"]["data"]] == [61.0]
    assert calls == [SHADOW_MODEL_VERSION, V4]
