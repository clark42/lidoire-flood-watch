"""Indicator collection jobs, breakers, backfill wiring and scheduler (mocked HTTP)."""

import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx

from app.services import influx_client
from app.services.collectors import hydro_indicators as hi

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 26, 13, 0)
TR_URL = f"{hi.HUBEAU_API_BASE}/observations_tr"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def written(monkeypatch):
    """Capture the points sent to InfluxDB (no server needed)."""
    out: list[dict] = []

    async def fake_batch(points, chunk_size=5000):
        out.extend(influx_client.point_as_dict(p) for p in points)
        return len(points)

    monkeypatch.setattr(influx_client, "write_points_batch", fake_batch)
    monkeypatch.setattr(hi, "_utcnow", lambda: NOW)
    return out


def _tr_response(request: httpx.Request) -> httpx.Response:
    grandeur = request.url.params["grandeur_hydro"]
    payload = load("hubeau_tr_q_eyraud.json" if grandeur == "Q" else "hubeau_tr_h_eyraud.json")
    return httpx.Response(200, json=payload)


@respx.mock
async def test_collect_flows_writes_q_and_eyraud_h(written):
    route = respx.get(TR_URL).mock(side_effect=_tr_response)
    count = await hi.collect_and_store_flows()
    # 5 stations x 3 timestamps; only Eyraud also asks for H
    assert count == 15
    calls = [(c.request.url.params["code_entite"], c.request.url.params["grandeur_hydro"]) for c in route.calls]
    assert ("P540401002", "H") in calls
    assert sorted(code for code, g in calls if g == "Q") == sorted(
        ["P540401002", "P524401001", "P555001001", "P532001001", "P135001001"]
    )
    assert [g for code, g in calls if code != "P540401002"] == ["Q"] * 4
    eyraud = [p for p in written if p["tags"]["key"] == "EYRAUD"]
    assert all({"q_m3s", "h_m"} <= set(p["fields"]) for p in eyraud)
    # 24 h window ending now (Eyraud publishes with ~7 h latency)
    params = route.calls[0].request.url.params
    assert params["date_debut_obs"] == "2026-10-25T13:00:00Z"
    assert params["date_fin_obs"] == "2026-10-26T13:00:00Z"


@respx.mock
async def test_collect_flows_opens_breaker_on_5xx(written):
    route = respx.get(TR_URL).mock(return_value=httpx.Response(503))
    assert await hi.collect_and_store_flows() == 0
    n = len(route.calls)
    assert n > 0
    # Breaker open: the next round does not call Hub'Eau at all
    assert await hi.collect_and_store_flows() == 0
    assert len(route.calls) == n


@respx.mock
async def test_collect_flows_4xx_does_not_open_breaker(written):
    route = respx.get(TR_URL).mock(return_value=httpx.Response(400))
    await hi.collect_and_store_flows()
    n = len(route.calls)
    await hi.collect_and_store_flows()
    assert len(route.calls) == 2 * n


@respx.mock
async def test_collect_flows_uses_external_timeout(written, monkeypatch):
    seen = []
    real = hi._get_json

    async def spy(client, url, params, timeout, headers=None):
        seen.append(timeout)
        return await real(client, url, params, timeout, headers)

    monkeypatch.setattr(hi, "_get_json", spy)
    respx.get(TR_URL).mock(side_effect=_tr_response)
    await hi.collect_and_store_flows()
    assert seen and set(seen) == {hi.EXTERNAL_TIMEOUT_S}


@respx.mock
async def test_collect_groundwater(written):
    route = respx.get(f"{hi.NAPPES_API_BASE}/chroniques_tr").mock(
        return_value=httpx.Response(206, json=load("piezo_tr_mouliets.json"))
    )
    assert await hi.collect_and_store_groundwater() == 3
    assert route.calls[0].request.url.params["code_bss"] == "08048X0088/P"
    assert {p["tags"]["source"] for p in written} == {"ades-tr"}
    assert written[-1]["fields"]["level_m_ngf"] == pytest.approx(5.836)


@respx.mock
async def test_collect_tide_observed_shom_and_hubeau(written):
    shom = respx.get(hi.SHOM_OBSERVATION_URL.format(shom_id=196)).mock(
        return_value=httpx.Response(200, json=load("shom_bordeaux.json"))
    )
    hub = respx.get(TR_URL).mock(return_value=httpx.Response(200, json=load("hubeau_tr_h_eyraud.json")))
    count = await hi.collect_and_store_tide_observed()
    assert count == 3 + 3
    assert shom.calls[0].request.url.params["sources"] == "1"
    assert hub.calls[0].request.url.params["code_entite"] == "P916000101"
    keys = {(p["tags"]["key"], p["tags"]["source"]) for p in written}
    assert keys == {("BORDEAUX", "shom-refmar"), ("BEC_AMBES", "hubeau-tr")}


@respx.mock
async def test_tide_observed_shom_down_keeps_hubeau(written):
    respx.get(hi.SHOM_OBSERVATION_URL.format(shom_id=196)).mock(side_effect=httpx.ConnectError("down"))
    respx.get(TR_URL).mock(return_value=httpx.Response(200, json=load("hubeau_tr_h_eyraud.json")))
    assert await hi.collect_and_store_tide_observed() == 3
    assert not hi._shom_breaker.allow()


@respx.mock
async def test_backfill_flows_falls_back_to_daily_mean(written, monkeypatch):
    monkeypatch.setattr(hi, "get_settings", lambda: _only_station("EYRAUD"))
    respx.get(f"{hi.HYDROPORTAIL_BASE}/P540401002/series").mock(
        return_value=httpx.Response(200, json={"series": {"data": []}})
    )
    respx.get(f"{hi.HUBEAU_API_BASE}/obs_elab").mock(
        return_value=httpx.Response(200, json=load("obs_elab_qmnj_eyraud.json"))
    )
    result = await hi.backfill_flows(datetime(2024, 2, 1), datetime(2024, 3, 1), pause_s=0)
    assert result["EYRAUD"]["points"] == 3
    assert result["EYRAUD"]["daily_fallback_batches"] == 1
    assert {p["tags"]["source"] for p in written} == {"hubeau-elab-qmnj"}


@respx.mock
async def test_backfill_flows_hydroportail_batches(written, monkeypatch):
    monkeypatch.setattr(hi, "get_settings", lambda: _only_station("EYRAUD"))
    route = respx.get(f"{hi.HYDROPORTAIL_BASE}/P540401002/series").mock(
        return_value=httpx.Response(200, json=load("hydroportail_q_eyraud.json"))
    )
    result = await hi.backfill_flows(datetime(2024, 1, 1), datetime(2024, 3, 1), batch_days=30, pause_s=0)
    # 2 batches x (Q + H) requests, same recorded payload
    assert len(route.calls) == 4
    variables = [c.request.url.params["hydro_series[simpleAndInterpolatedAndHourlyVariable]"] for c in route.calls]
    assert variables == ["Q", "H", "Q", "H"]
    assert result["EYRAUD"]["points"] == 8
    assert {p["tags"]["source"] for p in written} == {"hydroportail"}


def _only_station(key):
    from app.config import get_settings

    real = get_settings()

    class _S:
        indicator_flow_stations = [s for s in real.indicator_flow_stations if s["key"] == key]
        indicator_groundwater_stations = real.indicator_groundwater_stations
        indicator_tide_gauges = real.indicator_tide_gauges

    return _S()


async def test_run_full_backfill_indicators_is_opt_in(monkeypatch):
    from app.services import backfill

    calls = []

    async def fake(start, end):
        calls.append((start, end))
        return {"flows": {}, "groundwater": {}, "tide_observed": {}}

    monkeypatch.setattr(hi, "backfill_indicators", fake)
    flags = dict(skip_rivers=True, skip_rainfall=True, skip_tides=True, skip_soil=True, skip_risk=True)
    s, e = datetime(2022, 11, 1), datetime(2022, 11, 2)
    res = await backfill.run_full_backfill(s, e, **flags)
    assert calls == [] and "indicators" not in res
    res = await backfill.run_full_backfill(s, e, indicators=True, **flags)
    assert calls == [(s, e)]
    assert res["indicators"] == {"flows": {}, "groundwater": {}, "tide_observed": {}}


async def test_backfill_route_forwards_indicators_flag(monkeypatch):
    from app.api import episodes

    seen = {}

    async def fake_run(**kwargs):
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(episodes, "run_full_backfill", fake_run)
    episodes._backfill_status["running"] = False
    await episodes._run_backfill_task(None, None, True, True, True, True, True, indicators=True)
    assert seen["indicators"] is True


async def test_scheduler_registers_indicator_jobs(monkeypatch):
    from app.services import scheduler

    added = {}

    class FakeScheduler:
        def __init__(self, *a, **kw):
            pass

        def add_job(self, func, trigger=None, id=None, **kw):
            added[id] = (func, trigger)

        def start(self):
            pass

        def get_jobs(self):
            return []

    monkeypatch.setattr(scheduler, "AsyncIOScheduler", FakeScheduler)
    monkeypatch.setattr(scheduler, "_scheduler", None)
    from app.services import notifier
    monkeypatch.setattr(notifier, "schedule_notifier", lambda *a, **kw: None)
    await scheduler.start_scheduler()
    try:
        assert added["indicator_flow_collector"][1].interval.total_seconds() == 15 * 60
        assert added["tide_observed_collector"][1].interval.total_seconds() == 15 * 60
        assert added["groundwater_collector"][1].interval.total_seconds() == 3 * 3600
        # Existing jobs untouched
        assert {"river_collector", "risk_scorer", "tide_collector"} <= set(added)
    finally:
        monkeypatch.setattr(scheduler, "_scheduler", None)


async def test_jobs_never_raise(monkeypatch):
    from app.services import scheduler

    async def boom(*a, **kw):
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(hi, "collect_and_store_flows", boom)
    monkeypatch.setattr(hi, "collect_and_store_groundwater", boom)
    monkeypatch.setattr(hi, "collect_and_store_tide_observed", boom)
    await scheduler.collect_indicator_flows_job()
    await scheduler.collect_groundwater_job()
    await scheduler.collect_tide_observed_job()
