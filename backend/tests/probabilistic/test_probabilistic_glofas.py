"""GloFAS (Open-Meteo Flood API): parsing, climatology, exceedance, runs, storage.

Fixtures are trimmed real answers (2026-10-26) for the Lidoire cell
(44.875, -0.025): ``glofas_forecast_ensemble.json`` (forecast_v4, 51
members, 4 days) and ``glofas_history.json`` (consolidated_v4, 405 days).
"""

import json
import pathlib
from datetime import date, datetime

import httpx
import numpy as np
import pytest
import respx

from app.services import influx_client
from app.services.collectors import glofas

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
FORECAST = json.loads((FIXTURES / "glofas_forecast_ensemble.json").read_text())
HISTORY = json.loads((FIXTURES / "glofas_history.json").read_text())
NOW = datetime(2026, 10, 26, 14, 0)
META = {"last_run_initialisation_time": 1792972800, "data_end_time": 1795651200}  # 2026-10-26 00Z


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    monkeypatch.setattr(glofas, "_latest", None)
    monkeypatch.setattr(glofas, "_latest_at", None)
    monkeypatch.setattr(glofas, "_climatology", None)
    monkeypatch.setattr(glofas, "_climatology_stored", False)
    monkeypatch.setattr(glofas, "_utcnow", lambda: NOW)


def test_glofas_parse_members_control_first():
    days, members = glofas.parse_flood_members(FORECAST)
    assert days == [date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 29)]
    assert members.shape == (4, 51)
    assert members[0, 0] == FORECAST["daily"]["river_discharge"][0]
    assert members[0, 50] == FORECAST["daily"]["river_discharge_member50"][0]
    no_days, empty = glofas.parse_flood_members({})
    assert no_days == [] and empty.size == 0


def test_glofas_history_climatology_percentiles():
    values, first, last = glofas.parse_flood_history(HISTORY)
    assert first == date(2023, 10, 27) and values.size == 405
    clim = glofas.climatology_from_values(values, first, last)
    assert clim["p95"] == pytest.approx(np.percentile(values, 95), abs=1e-3)
    assert clim["p50"] <= clim["p90"] <= clim["p95"] <= clim["p99"] <= clim["max"]
    assert clim["n_days"] == 405 and clim["model"] == "consolidated_v4"
    # Too short a history gives no climatology
    assert glofas.climatology_from_values(values[:100], first, last) is None


def test_glofas_exceedance_probabilities():
    clim = {"p95": 3.0, "p99": 6.0}
    row = np.array([1.0] * 30 + [4.0] * 15 + [7.0] * 6)  # 51 members
    stats = glofas.summarize_members(row, clim)
    assert stats["prob_gt_p95"] == round(21 / 51, 3)
    assert stats["prob_gt_p99"] == round(6 / 51, 3)
    assert stats["members"] == 51 and stats["min"] == 1.0 and stats["max"] == 7.0
    assert stats["p50"] == 1.0 and stats["p90"] == pytest.approx(np.percentile(row, 90))
    # Missing members: computed on the valid ones, None below half
    assert glofas.summarize_members(np.array([np.nan] * 30 + [1.0] * 21), clim) is None
    assert glofas.summarize_members(row, None)["prob_gt_p95"] is None


def test_glofas_run_date_from_meta():
    assert glofas.run_date_from_meta(META, NOW) == date(2026, 10, 26)
    assert glofas.run_date_from_meta({"last_run_initialisation_time": 1791244800}, NOW) is None  # 2026-10-06
    assert glofas.run_date_from_meta(None, NOW) is None


def test_glofas_summary_leads_and_points():
    days, members = glofas.parse_flood_members(FORECAST)
    clim = {"p95": 0.085, "p99": 6.35}
    summary = glofas.build_summary(days, members, clim, date(2026, 10, 27), NOW)
    # The day before the run (lead -1) is dropped
    assert [d["lead_days"] for d in summary["days"]] == [0, 1, 2]
    assert summary["run"] == "2026-10-27" and summary["run_known"] is True and summary["members"] == 51
    points = glofas.make_glofas_points(summary)
    assert [p["tags"] for p in points] == [
        {"cell": "LIDOIRE", "run": "2026-10-27", "lead": str(k)} for k in range(3)
    ]
    assert points[0]["measurement"] == "glofas_forecast"
    assert points[0]["timestamp"] == datetime(2026, 10, 27)
    assert set(points[0]["fields"]) >= {"p10", "p50", "p90", "prob_gt_p95", "prob_gt_p99", "clim_p95"}
    # Every tag value passes the Flux tag validator
    for p in points:
        for value in p["tags"].values():
            influx_client.validate_tag_value(value)


def _mock_api():
    respx.get(glofas.FLOOD_META_URL).mock(return_value=httpx.Response(200, json=META))

    def flood(request):
        model = request.url.params["models"]
        return httpx.Response(200, json=FORECAST if model == "forecast_v4" else HISTORY)

    return respx.get(glofas.FLOOD_API_URL).mock(side_effect=flood)


@respx.mock
async def test_glofas_route_path_fetches_but_never_stores(monkeypatch):
    route = _mock_api()

    async def no_clim(max_age_days=400):
        return None

    async def must_not_write(points, chunk_size=5000):
        raise AssertionError("route path must not write")

    import app.services.probabilistic_store as store
    monkeypatch.setattr(store, "query_glofas_climatology", no_clim)
    monkeypatch.setattr(influx_client, "write_points_batch", must_not_write)

    summary = await glofas.get_glofas_forecast()
    assert summary["available"] is True and summary["run"] == "2026-10-26"
    assert summary["climatology"]["n_days"] == 405
    params = [dict(c.request.url.params) for c in route.calls]
    assert {"models": "forecast_v4", "ensemble": "true"}.items() <= params[0].items()
    assert all(c.request.url.scheme == "https" for c in route.calls)
    # Cached: no new call
    await glofas.get_glofas_forecast()
    assert route.call_count == 2


@respx.mock
async def test_glofas_job_stores_run_and_climatology(monkeypatch):
    _mock_api()

    async def no_clim(max_age_days=400):
        return None

    written = []

    async def write(points, chunk_size=5000):
        written.extend(points)
        return len(points)

    import app.services.probabilistic_store as store
    monkeypatch.setattr(store, "query_glofas_climatology", no_clim)
    monkeypatch.setattr(influx_client, "write_points_batch", write)

    # A route fetched the climatology first (memory only): the job still stores it, once
    monkeypatch.setattr(glofas, "CACHE_TTL", glofas.timedelta(0))
    await glofas.get_glofas_forecast()
    assert written == []
    count = await glofas.collect_and_store_glofas()
    measurements = [p["measurement"] for p in written]
    assert measurements.count("glofas_climatology") == 1
    assert measurements.count("glofas_forecast") == count == 4
    await glofas.collect_and_store_glofas()
    assert [p["measurement"] for p in written].count("glofas_climatology") == 1


async def test_glofas_disabled(monkeypatch):
    monkeypatch.setattr(glofas.get_settings(), "glofas_enabled", False)
    assert (await glofas.get_glofas_forecast())["available"] is False
    assert await glofas.collect_and_store_glofas() == 0
