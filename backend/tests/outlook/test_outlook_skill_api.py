"""Forecast-vs-actual pairing and the /api/risk/outlook, /api/risk/forecast-skill contracts."""

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import risk as risk_api
from app.services import forecast_archive, outlook
from app.services.forecast_archive import get_forecast_skill, pair_forecasts, parse_leads, skill_by_lead
from app.services.risk_core import MODEL_VERSION

from outlook_fixtures import T0, MemoryRunStore, forecast_result, rising_inputs


def _run(run_at, scores):
    return {"run_at": run_at, "model_version": MODEL_VERSION, "scores": scores}


# --- pairing -----------------------------------------------------------------------

def test_outlook_skill_pairs_nearest_actual_within_tolerance():
    run = _run(T0, [10.0] * 7 + [40.0] + [5.0] * 20)
    actuals = [
        (T0 + timedelta(hours=7, minutes=-20), 30.0),   # 20 min early
        (T0 + timedelta(hours=7, minutes=5), 45.0),     # nearest
        (T0 + timedelta(hours=24, minutes=50), 99.0),   # beyond 35 min of H+24
    ]
    pairs = pair_forecasts([run], actuals, [7, 24, 200])
    assert len(pairs) == 1                              # H+24 too far, H+200 beyond the horizon
    p = pairs[0]
    assert p["lead_hours"] == 7 and p["forecast_score"] == 40.0
    assert p["actual_score"] == 45.0 and p["actual_at"] == T0 + timedelta(hours=7, minutes=5)
    assert p["valid_at"] == T0 + timedelta(hours=7)


def test_outlook_skill_contingency_and_mae_at_orange():
    pairs = [
        {"lead_hours": 6, "forecast_score": 50.0, "actual_score": 40.0},   # hit
        {"lead_hours": 6, "forecast_score": 20.0, "actual_score": 36.0},   # miss
        {"lead_hours": 6, "forecast_score": 35.0, "actual_score": 10.0},   # false alarm (>= threshold)
        {"lead_hours": 6, "forecast_score": 10.0, "actual_score": 12.0},   # correct negative
        {"lead_hours": 24, "forecast_score": 10.0, "actual_score": 10.0},
    ]
    by_lead = {s["lead_hours"]: s for s in skill_by_lead(pairs, [6, 24, 72], 35.0)}
    s6 = by_lead[6]
    assert (s6["hits"], s6["misses"], s6["false_alarms"], s6["correct_negatives"]) == (1, 1, 1, 1)
    assert s6["mae"] == pytest.approx((10 + 16 + 25 + 2) / 4)
    assert s6["bias"] == pytest.approx((10 - 16 + 25 - 2) / 4)
    assert s6["pod"] == 0.5 and s6["far"] == 0.5 and s6["csi"] == pytest.approx(0.333)
    assert by_lead[24]["pod"] is None and by_lead[24]["mae"] == 0.0
    assert by_lead[72] == {**by_lead[72], "n": 0, "mae": None, "csi": None}


def test_outlook_skill_parse_leads():
    assert parse_leads("72, 6,24,6") == [6, 24, 72]
    for bad in ("", "x", "-1", "500", ",".join(str(i) for i in range(12))):
        with pytest.raises(ValueError):
            parse_leads(bad)


async def test_outlook_skill_empty_until_runs_accumulate():
    async def no_actuals(start, end, version):
        raise AssertionError("not queried without runs")

    out = await get_forecast_skill(store=MemoryRunStore(), actuals_fn=no_actuals, now=T0)
    assert out["n_runs"] == 0 and out["pairs"] == []
    assert [s["lead_hours"] for s in out["by_lead"]] == [6, 24, 72]
    assert all(s["n"] == 0 for s in out["by_lead"])
    assert out["threshold"] == 35.0 and out["notes"]


async def test_outlook_skill_from_archived_runs():
    store = MemoryRunStore()
    for k in range(48):
        run_at = T0 + timedelta(hours=k)
        await store.save({"run_at": run_at, "model_version": MODEL_VERSION, "scores": [20.0] * 169})
    await store.save({"run_at": T0, "model_version": "3.3", "scores": [99.0] * 169})   # other model
    actual_scores = {T0 + timedelta(hours=k, minutes=1): 20.0 + k for k in range(0, 80)}

    async def actuals(start, end, version):
        assert version == MODEL_VERSION
        return [(t, s) for t, s in actual_scores.items() if start <= t <= end]

    now = T0 + timedelta(hours=60)
    out = await get_forecast_skill(leads=[6, 24], days=7, store=store, actuals_fn=actuals, now=now)
    assert out["n_runs"] == 48
    by_lead = {s["lead_hours"]: s for s in out["by_lead"]}
    assert by_lead[6]["n"] == 48                 # all H+6 targets are in the past
    assert by_lead[24]["n"] == 37                # runs after T0 + 36 h: H+24 not reached yet
    assert all(p["forecast_score"] == 20.0 for p in out["pairs"])
    # Forecast 20 vs actual 20 + k + lead: bias negative, misses once actual >= 35
    assert by_lead[24]["bias"] < 0 and by_lead[24]["misses"] > 0 and by_lead[24]["hits"] == 0


# --- API contracts -----------------------------------------------------------------

@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(risk_api.router, prefix="/api/risk")
    return TestClient(app)


def test_outlook_route_contract_and_no_write(client, monkeypatch):
    from app.services import forecast as forecast_mod

    result, _ = forecast_result(rising_inputs())
    cache: dict = {}

    async def fake_forecast(hours=168, use_cache=True):
        assert hours == outlook.OUTLOOK_FORECAST_HOURS and use_cache is True
        return result

    async def fake_sources():
        return {"infoclimat": {"label": "Pluie Infoclimat", "status": "stale", "age_hours": 6.5,
                               "critical": False}}

    async def get_cached():
        return cache.get("v")

    async def set_cached(data):
        cache["v"] = data

    async def forbidden(*args, **kwargs):
        raise AssertionError("read route must not archive")

    monkeypatch.setattr(forecast_mod, "get_risk_forecast_v2", fake_forecast)
    monkeypatch.setattr(outlook, "_source_freshness", fake_sources)
    monkeypatch.setattr(outlook, "_get_cached_outlook", get_cached)
    monkeypatch.setattr(outlook, "_set_cached_outlook", set_cached)
    monkeypatch.setattr(outlook, "_utcnow", lambda: T0 + timedelta(minutes=10))
    monkeypatch.setattr(forecast_archive.PostgresForecastRunStore, "save", forbidden)

    response = client.get("/api/risk/outlook")
    assert response.status_code == 200
    body = response.json()
    assert body["model_version"] == MODEL_VERSION
    assert [h["label"] for h in body["horizons"]] == ["6 h", "24 h", "72 h"]
    h = body["horizons"][1]
    assert h["max_at"].endswith("Z") and h["first_at_level"].endswith("Z")
    assert h["level"] in ("green", "yellow", "orange", "red")
    assert {"factor", "label", "points", "text"} <= set(h["drivers"][0])
    assert body["generated_at"].endswith("Z") and body["forecast_generated_at"] == "2024-02-10T12:00:00Z"
    assert body["next_high_tide"]["timestamp"].endswith("Z")
    assert body["confidence"]["level"] in ("good", "reduced", "low")
    assert "infoclimat" in body["confidence"]["stale_sources"]
    assert body["checklist"]["level"] == body["highest_level"] == "red"
    assert body["checklist"]["actions"] and body["checklist"]["act_before"].endswith("Z")
    # Second call served from the cache
    assert cache and client.get("/api/risk/outlook").json() == body


def test_outlook_route_error_is_500(client, monkeypatch):
    async def broken():
        raise RuntimeError("redis and forecast down")

    monkeypatch.setattr(outlook, "get_outlook", broken)
    assert client.get("/api/risk/outlook").status_code == 500


def test_outlook_forecast_skill_route_contract(client, monkeypatch):
    store = MemoryRunStore()
    store.rows.append({"run_at": T0, "model_version": MODEL_VERSION, "scores": [40.0] * 169})

    async def actuals(start, end, version):
        return [(T0 + timedelta(hours=6, minutes=2), 50.0)]

    monkeypatch.setattr(forecast_archive, "PostgresForecastRunStore", lambda: store)
    monkeypatch.setattr(forecast_archive, "_default_actuals", actuals)
    monkeypatch.setattr(forecast_archive, "_utcnow", lambda: T0 + timedelta(days=4))

    response = client.get("/api/risk/forecast-skill?lead_hours=6,24,72&days=30")
    assert response.status_code == 200
    body = response.json()
    assert body["lead_hours"] == [6, 24, 72] and body["n_runs"] == 1
    assert body["first_run_at"] == "2024-02-10T12:00:00Z"
    assert body["pairs"] == [{
        "run_at": "2024-02-10T12:00:00Z", "lead_hours": 6, "valid_at": "2024-02-10T18:00:00Z",
        "forecast_score": 40.0, "actual_score": 50.0, "actual_at": "2024-02-10T18:02:00Z",
    }]
    s6 = body["by_lead"][0]
    assert s6["n"] == 1 and s6["hits"] == 1 and s6["mae"] == 10.0

    assert client.get("/api/risk/forecast-skill?lead_hours=abc").status_code == 422
    assert client.get("/api/risk/forecast-skill?days=0").status_code == 422


def test_outlook_forecast_skill_route_empty_and_db_error(client, monkeypatch):
    monkeypatch.setattr(forecast_archive, "PostgresForecastRunStore", MemoryRunStore)
    body = client.get("/api/risk/forecast-skill").json()
    assert body["n_runs"] == 0 and body["pairs"] == [] and len(body["by_lead"]) == 3
    assert body["first_run_at"] is None

    class Broken(MemoryRunStore):
        async def list_runs(self, start, end, model_version):
            raise RuntimeError("postgres down")

    monkeypatch.setattr(forecast_archive, "PostgresForecastRunStore", Broken)
    assert client.get("/api/risk/forecast-skill").status_code == 503
