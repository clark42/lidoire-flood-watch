"""Forecast archive: row builder, scheduler job, forecast_runs table, forecast cache."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.services import forecast_archive
from app.services.forecast_archive import archive_forecast_run, forecast_archive_job, run_row_from_forecast
from app.services.risk_core import MODEL_VERSION

from outlook_fixtures import T0, MemoryRunStore, forecast_result, rising_inputs


def test_outlook_archive_row_is_compact_and_indexed_by_lead():
    result, breakdowns = forecast_result(rising_inputs())
    row = run_row_from_forecast(result)
    assert row["run_at"] == T0 and row["run_at"].tzinfo is None
    assert row["model_version"] == MODEL_VERSION
    assert row["horizon_hours"] == 168
    assert len(row["scores"]) == len(row["levels"]) == len(row["pessac_levels_m"]) == 169
    assert row["scores"][0] == result["forecasts"][0]["score"]
    assert row["scores"][24] == result["forecasts"][24]["score"]
    assert row["levels"][72] == result["forecasts"][72]["level"]
    inputs = result["inputs_summary"]
    assert row["rain_forecast_24h_mm"] == inputs["rain_forecast_24h_mm"] == pytest.approx(4.0 * 12, abs=0.1)
    assert row["rain_forecast_72h_mm"] == pytest.approx(48.0, abs=0.1)
    assert row["pessac_projected_max_m"] == max(f["projected_level_m"] for f in result["forecasts"][:73])
    assert row["tide_gate"] == pytest.approx(breakdowns[0].tide_gate, abs=1e-3)
    assert row["inputs"]["data_sources"] == {"river": "ok", "rain": "ok"}
    # Size estimate behind the "keep everything" retention policy
    size = len(json.dumps({k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}))
    assert size < 5000


def test_outlook_archive_row_rejects_empty_forecast():
    with pytest.raises(ValueError):
        run_row_from_forecast({"generated_at": T0.isoformat(), "forecasts": []})


async def test_outlook_archive_writer_stores_one_row_per_run(monkeypatch):
    result, _ = forecast_result(rising_inputs())
    store = MemoryRunStore()
    calls = []

    async def fresh_forecast():
        calls.append(1)
        return result

    invalidated = []

    async def fake_invalidate():
        invalidated.append(1)

    monkeypatch.setattr("app.services.outlook.invalidate_outlook_cache", fake_invalidate)
    row = await archive_forecast_run(store=store, forecast_fn=fresh_forecast)
    assert row is not None and len(store.rows) == 1
    assert invalidated == [1]
    # Same run again (same generated_at): not duplicated
    assert await archive_forecast_run(store=store, forecast_fn=fresh_forecast) is None
    assert len(store.rows) == 1 and len(calls) == 2


async def test_outlook_archive_default_forecast_bypasses_cache(monkeypatch):
    from app.services import forecast as forecast_mod

    seen = []

    async def fake_forecast(hours=168, use_cache=True):
        seen.append((hours, use_cache))
        return forecast_result(rising_inputs())[0]

    monkeypatch.setattr(forecast_mod, "get_risk_forecast_v2", fake_forecast)
    store = MemoryRunStore()
    await archive_forecast_run(store=store)
    assert seen == [(forecast_archive.ARCHIVE_HOURS, False)]


async def test_outlook_archive_job_never_raises(monkeypatch, caplog):
    async def boom(store=None, forecast_fn=None):
        raise RuntimeError("open-meteo down")

    monkeypatch.setattr(forecast_archive, "archive_forecast_run", boom)
    await forecast_archive_job()                       # logged, not raised
    assert "Error archiving forecast run" in caplog.text


async def test_outlook_archive_job_scheduled_after_risk_scorer(monkeypatch):
    from app.services import scheduler

    async def noop():
        return None

    for name in ("collect_tide_job", "collect_river_job", "collect_weather_job", "collect_netatmo_job",
                 "collect_infoclimat_job", "collect_meteo_agricole_job", "collect_nowcast_rain_job",
                 "compute_risk_job", "collect_camera_job"):
        monkeypatch.setattr(scheduler, name, noop)
    monkeypatch.setattr(forecast_archive, "forecast_archive_job", noop)

    await scheduler.start_scheduler()
    try:
        jobs = {j.id: j for j in scheduler._scheduler.get_jobs()}
        archive = jobs[forecast_archive.JOB_ID]
        assert archive.trigger.interval == timedelta(hours=1)
        assert archive.next_run_time - jobs["risk_scorer"].next_run_time == forecast_archive.JOB_OFFSET
        if "notifier" in jobs:                          # notifier reads the refreshed forecast
            assert archive.next_run_time < jobs["notifier"].next_run_time
    finally:
        await scheduler.stop_scheduler()


def test_outlook_forecast_runs_table_created_by_metadata():
    from app.db import Base
    from app.models.orm import ForecastRunRow

    row = run_row_from_forecast(forecast_result(rising_inputs(), hours=24)[0])
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(ForecastRunRow(**row))
        session.commit()
        stored = session.scalars(select(ForecastRunRow)).one()
        assert stored.scores == row["scores"] and stored.levels[0] == row["levels"][0]
        assert stored.inputs["tide_reliable"] is True
        assert stored.run_at == T0 and stored.created_at is not None
        session.add(ForecastRunRow(**row))             # same (run_at, model_version)
        with pytest.raises(IntegrityError):
            session.commit()
    engine.dispose()


async def test_outlook_forecast_payload_has_horizons_and_use_cache(monkeypatch):
    """get_risk_forecast_v2 exposes horizons / high tides / inputs; use_cache=False skips the read."""
    from app.models.schemas import WeatherForecast
    from app.services import forecast as forecast_mod
    from app.services import risk_v2

    reads, writes = [], []

    async def cached(hours):
        reads.append(hours)
        return {"generated_at": T0.isoformat(), "horizons": [], "forecasts": []}

    async def set_cached(hours, data):
        writes.append(hours)

    async def fake_gather(now=None):
        raise RuntimeError("no realtime inputs in this test")

    async def fake_fc(lat, lon, days):
        return [WeatherForecast(timestamp=T0 + timedelta(hours=k), point_id="m", point_name="m",
                                precipitation_mm=1.0) for k in range(1, 80)]

    async def fake_past(lat, lon, past_days=7):
        return {}

    async def fake_tides(port, start, end):
        return []

    monkeypatch.setattr(forecast_mod, "_get_cached_forecast", cached)
    monkeypatch.setattr(forecast_mod, "_set_cached_forecast", set_cached)
    monkeypatch.setattr(risk_v2, "gather_realtime_inputs", fake_gather)
    monkeypatch.setattr("app.services.collectors.weather.fetch_open_meteo_forecast", fake_fc)
    monkeypatch.setattr(forecast_mod, "_fetch_past_hourly_rain_utc", fake_past)
    monkeypatch.setattr(forecast_mod, "_fetch_tide_highs_from_influx", fake_tides)
    monkeypatch.setattr(forecast_mod, "_utcnow", lambda: T0)

    assert (await forecast_mod.get_risk_forecast_v2(hours=24))["forecasts"] == []   # cache hit
    fresh = await forecast_mod.get_risk_forecast_v2(hours=72, use_cache=False)
    assert reads == [24] and writes == [72]
    assert [h["hours"] for h in fresh["horizons"]] == [6, 24, 72]
    assert fresh["inputs_summary"]["rain_forecast_24h_mm"] == pytest.approx(24.0)
    assert fresh["high_tides"] == []
    assert len(fresh["forecasts"]) == 73
