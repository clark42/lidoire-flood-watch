"""Contracts of the new endpoints, timeline series, status entries, jobs, isolation from v3.4."""

import pathlib
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import realtime as realtime_api
from app.api import status
from app.services import influx_client
import app.services.probabilistic_store as store

BACKEND = pathlib.Path(__file__).resolve().parents[2]
ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@pytest.fixture
def client():
    from app.api import router

    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


def test_forecast_ensemble_contract(client, monkeypatch):
    import app.api.forecast_ensemble as fe

    start = datetime(2026, 10, 26, 21, 0)

    async def rain():
        return {
            "available": True,
            "thresholds": {"24h": [25.0, 40.0, 60.0], "72h": [50.0, 80.0, 120.0]},
            "issued_at": start,
            "models": [{"id": "ecmwf_ifs025", "available": True, "run_init": datetime(2026, 10, 26)}],
            "horizons": {"24h": [{
                "start": start, "end": start + timedelta(hours=24), "lead_start_h": 0, "lead_end_h": 24,
                "models": {"ecmwf_ifs025": {"p50": 1.0, "prob": {"25": 0.1, "40": 0.0, "60": 0.0}}},
                "blend": {"prob": {"25": 0.1, "40": 0.0, "60": 0.0}, "models": ["ecmwf_ifs025"]},
            }], "72h": []},
        }

    async def boom():
        raise RuntimeError("upstream down")

    monkeypatch.setattr(fe, "get_ensemble_rain", rain)
    monkeypatch.setattr(fe, "get_glofas_forecast", boom)
    body = client.get("/api/forecast/ensemble").json()
    assert body["rain"]["available"] is True
    assert ISO_Z.match(body["rain"]["issued_at"])
    row = body["rain"]["horizons"]["24h"][0]
    assert ISO_Z.match(row["start"]) and ISO_Z.match(row["end"])
    assert row["blend"]["prob"]["25"] == 0.1
    # One source down never hides the other
    assert body["glofas"] == {"available": False, "note": "GloFAS indisponible"}


def test_realtime_radar_contract(client, monkeypatch):
    now = datetime(2026, 10, 26, 21, 5)
    monkeypatch.setattr(realtime_api, "_utcnow", lambda: now)
    last = datetime(2026, 10, 26, 20, 45)

    async def points(start, end):
        assert end > now and start <= now - timedelta(hours=24)
        return [
            {"timestamp": last - timedelta(hours=k), "basin_mean_mm": 1.5, "max_pixel_mm": 4.0,
             "coverage": 1.0, "quality": 0.6, "pixels": 81}
            for k in range(6)
        ]

    monkeypatch.setattr(store, "query_radar_points", points)
    body = client.get("/api/realtime/radar").json()
    assert body["available"] is True
    assert body["last_image_time"] == "2026-10-26T20:45:00Z"
    assert body["age_minutes"] == 20.0
    assert body["windows"]["6h"]["mm"] == 9.0 and body["windows"]["6h"]["complete"] is True
    assert body["windows"]["24h"]["complete"] is False
    assert set(body["windows"]) == {"1h", "3h", "6h", "24h"}
    assert "OPERA" in body["attribution"] and "CC BY 4.0" in body["attribution"]


def test_realtime_radar_influx_down(client, monkeypatch):
    async def fail(start, end):
        raise ConnectionError("influx down")

    monkeypatch.setattr(store, "query_radar_points", fail)
    body = client.get("/api/realtime/radar").json()
    assert body["available"] is False and body["note"]


def test_timeline_radar_and_glofas_series(client, monkeypatch):
    t = datetime(2026, 10, 26, 18, 0)

    async def radar_points(start, end):
        return [{"timestamp": t + timedelta(minutes=15 * k), "basin_mean_mm": 0.5} for k in range(9)]

    async def glofas_rows(start, end, fields=store.GLOFAS_FIELDS):
        day = datetime(2026, 10, 26)
        return [
            {"timestamp": day, "run": "2026-10-25", "lead": "1", "p50": 1.0},
            {"timestamp": day, "run": "2026-10-26", "lead": "0", "p50": 2.0},
            {"timestamp": day + timedelta(days=1), "run": "2026-10-26", "lead": "1", "p50": 3.0},
        ]

    monkeypatch.setattr(store, "query_radar_points", radar_points)
    monkeypatch.setattr(store, "query_glofas_rows", glofas_rows)
    params = {
        "start": "2026-10-25T00:00:00", "end": "2026-10-27T12:00:00", "include_infoclimat": "false",
        "include_radar": "true", "include_glofas": "true",
    }
    body = client.get("/api/timeline/unified", params=params).json()
    radar = body["series"]["radar:LIDOIRE"]
    assert radar["type"] == "rainfall" and radar["unit"] == "mm/h"
    assert [p["value"] for p in radar["data"]] == [0.5, 0.5, 0.5]  # images on the hour only
    glofas = body["series"]["glofas:LIDOIRE"]
    assert glofas["unit"] == "m³/s"
    assert [p["value"] for p in glofas["data"]] == [2.0, 3.0]  # newest run wins per day
    # Off by default
    body = client.get("/api/timeline/unified", params={**params, "include_radar": "false",
                                                       "include_glofas": "false"}).json()
    assert "radar:LIDOIRE" not in body["series"] and "glofas:LIDOIRE" not in body["series"]


async def test_status_lists_new_sources_as_informative(monkeypatch):
    status._sources_cache.clear()
    now = datetime(2026, 10, 26, 21, 0)
    monkeypatch.setattr(status, "_utcnow", lambda: now)
    calls = {}

    async def last_time(measurement, tags=None, field=None, start=None, stop=None):
        calls[measurement] = (tags, field)
        return None if measurement in ("radar_rain", "glofas_forecast", "ensemble_rain") else now

    monkeypatch.setattr(influx_client, "query_last_time", last_time)
    try:
        sources = await status.check_sources()
    finally:
        status._sources_cache.clear()
    for key in ("radar_opera", "glofas", "ensemble_rain"):
        assert sources[key]["status"] == "unavailable"
        assert sources[key]["critical"] is False
    assert calls["radar_rain"] == ({"source": ["opera"]}, "basin_mean_mm")
    assert calls["glofas_forecast"] == ({"lead": ["0"]}, "p50")
    assert calls["ensemble_rain"] == ({"window": ["run"]}, "members")


def test_scheduler_registers_enabled_jobs_only(monkeypatch):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from app.services import probabilistic_jobs as jobs

    now = datetime.now(timezone.utc)
    scheduler = AsyncIOScheduler(timezone=timezone.utc)
    assert jobs.schedule_probabilistic_jobs(scheduler, now) == [
        "radar_collector", "glofas_collector", "ensemble_rain_collector",
    ]
    radar_job = scheduler.get_job("radar_collector")
    assert radar_job.trigger.interval == timedelta(minutes=15)
    assert scheduler.get_job("glofas_collector").trigger.interval == timedelta(hours=6)

    settings = jobs.get_settings()
    monkeypatch.setattr(settings, "radar_enabled", False)
    scheduler2 = AsyncIOScheduler(timezone=timezone.utc)
    assert "radar_collector" not in jobs.schedule_probabilistic_jobs(scheduler2, now)


def test_risk_model_never_reads_the_new_series():
    """v3.4 isolation: no scoring/backfill/forecast module imports the new code."""
    new_modules = ("opera_radar", "glofas", "ensemble_rain", "probabilistic_store", "probabilistic_jobs",
                   "radar_rain", "glofas_forecast")
    risk_paths = [
        "app/services/risk_core.py", "app/services/risk_v2.py", "app/services/risk_shadow.py",
        "app/services/forecast.py", "app/services/backfill.py", "app/services/episode_detector.py",
        "app/services/notifier.py", "scripts/eval",
    ]
    for rel in risk_paths:
        path = BACKEND / rel
        files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
        for f in files:
            text = f.read_text()
            for name in new_modules:
                assert name not in text, f"{f.relative_to(BACKEND)} references {name}"


def test_new_external_endpoints_are_https():
    from app.config import Settings
    from app.services.collectors import ensemble_rain, glofas

    urls = [glofas.FLOOD_API_URL, glofas.FLOOD_META_URL, ensemble_rain.ENSEMBLE_API_URL,
            ensemble_rain.META_URL, Settings(_env_file=None).radar_opera_base_url]
    assert all(u.startswith("https://") for u in urls)
