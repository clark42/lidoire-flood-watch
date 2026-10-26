"""Shadow risk series (3.4-gate): own measurement, never mixed into risk_score reads."""

import pathlib
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI

from app.api import timeline
from app.services import influx_client
from app.services.risk_shadow import SHADOW_MODEL_VERSION

T = datetime(2024, 2, 15, 10, tzinfo=timezone.utc)


def _score_row(ts, score, version, source="realtime", **extra):
    return {"_time": ts, "score": score, "level": "yellow", "model_version": version, "source": source, **extra}


def test_shadow_point_uses_dedicated_measurement():
    p = influx_client.make_risk_point(
        12.0, "green", T.replace(tzinfo=None), {"gate": 0.5}, SHADOW_MODEL_VERSION, "realtime",
        measurement=influx_client.RISK_SHADOW_MEASUREMENT,
    )
    d = influx_client.point_as_dict(p)
    assert d["measurement"] == "risk_score_shadow"
    assert d["tags"] == {"model_version": "3.4-gate", "source": "realtime"}
    assert d["fields"]["factor_gate"] == 0.5
    # default stays the displayed series
    assert influx_client.point_as_dict(
        influx_client.make_risk_point(12.0, "green", T.replace(tzinfo=None), model_version="3.3")
    )["measurement"] == "risk_score"


def test_shadow_version_detection():
    assert influx_client.is_shadow_risk_version("3.3-gate")
    assert influx_client.is_shadow_risk_version("3.4-gate")
    for v in ("3.4", "3.3", "3.2", None, "", "legacy"):
        assert not influx_client.is_shadow_risk_version(v)


async def test_query_risk_scores_never_returns_shadow_rows(capture_queries, fake_tables):
    queries, set_response = capture_queries
    # A shadow row that would (wrongly) live in risk_score at an hour without v3.3
    set_response(fake_tables([
        [_score_row(T, 20.0, "3.3")],
        [_score_row(T, 10.0, "3.3-gate"), _score_row(T + timedelta(hours=1), 9.0, "3.3-gate")],
    ]))
    rows = await influx_client.query_risk_scores(T, T + timedelta(hours=2))
    assert [(r["score"], r["model_version"]) for r in rows] == [(20.0, "3.3")]
    assert 'r._measurement == "risk_score")' in queries[0]


async def test_risk_timeline_drops_shadow_and_shadow_timeline_reads_own_measurement(capture_queries, fake_tables):
    queries, set_response = capture_queries

    def _rec(ts, value, version, source):
        return {"_time": ts, "_value": value, "model_version": version, "source": source}

    set_response(fake_tables([
        [_rec(T, 20.0, "3.3", "realtime")],
        [_rec(T + timedelta(hours=1), 9.0, "3.3-gate", "realtime")],
    ]))
    rows = await influx_client.query_risk_scores_timeline(T, T + timedelta(hours=2))
    assert [r["value"] for r in rows] == [20.0]
    assert 'r._measurement == "risk_score")' in queries[-1]

    set_response(fake_tables([
        [_rec(T, 11.0, "3.4-gate", "backfill"), _rec(T + timedelta(hours=1), 12.0, "3.4-gate", "backfill")],
        [_rec(T + timedelta(hours=1), 13.0, "3.4-gate", "realtime")],
        [_rec(T, 99.0, "3.3-gate", "realtime")],  # older shadow version: not this series
    ]))
    rows = await influx_client.query_risk_shadow_timeline(T, T + timedelta(hours=2), SHADOW_MODEL_VERSION)
    assert [r["value"] for r in rows] == [11.0, 13.0]  # realtime wins over backfill per window
    assert 'r._measurement == "risk_score_shadow")' in queries[-1]


def test_no_prefix_or_regex_measurement_filter_in_app():
    """Every risk_score reader filters on the exact measurement name, so the
    ``risk_score_shadow`` measurement can never match a risk_score query."""
    app_dir = pathlib.Path(influx_client.__file__).resolve().parents[1]
    offenders = []
    for p in app_dir.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        if re.search(r"_measurement\s*=~", text) or re.search(r"hasPrefix\(.*_measurement", text):
            offenders.append(p.name)
    assert offenders == []


# --- timeline endpoint -------------------------------------------------------------------

@pytest.fixture
def app_client():
    app = FastAPI()
    app.include_router(timeline.router, prefix="/api/timeline")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_unified_shadow_series_is_opt_in(app_client, monkeypatch):
    calls = []

    async def fake_shadow(start, end, model_version, resolution="1h"):
        calls.append(model_version)
        return [{"timestamp": datetime(2024, 2, 15, 10), "value": 8.5}]

    monkeypatch.setattr(timeline, "query_risk_shadow_timeline", fake_shadow)
    params = {"start": "2024-02-14T00:00:00", "end": "2024-02-15T12:00:00", "include_infoclimat": "false"}

    async with app_client as client:
        off = await client.get("/api/timeline/unified", params=params)
        on = await client.get("/api/timeline/unified", params={**params, "include_risk_shadow": "true"})
    assert off.status_code == on.status_code == 200
    assert "risk:shadow" not in off.json()["series"]
    assert calls == [SHADOW_MODEL_VERSION]
    series = on.json()["series"]["risk:shadow"]
    assert series["type"] == "risk_score_shadow"
    assert [p["value"] for p in series["data"]] == [8.5]
    assert "risk:score" not in on.json()["series"]  # the displayed score is a separate opt-in
