"""Shared fixtures for storage tests (no InfluxDB / Redis / Postgres needed)."""

import pytest

from app.api import timeline
from app.services import influx_client


class FakeRecord:
    def __init__(self, values: dict):
        self.values = values

    def get_time(self):
        return self.values.get("_time")

    def get_value(self):
        return self.values.get("_value")

    def get_field(self):
        return self.values.get("_field")


class FakeTable:
    def __init__(self, records: list[dict]):
        self.records = [FakeRecord(r) for r in records]


@pytest.fixture(autouse=True)
def _reset_caches():
    timeline._ensure_cache.clear()
    timeline._inflight.clear()
    influx_client._rain_cumul_cache.clear()
    yield
    timeline._ensure_cache.clear()
    timeline._inflight.clear()
    influx_client._rain_cumul_cache.clear()


@pytest.fixture
def fake_tables():
    """Build fake Flux tables: fake_tables([[rec, rec], [rec]])."""
    def _build(tables: list[list[dict]]):
        return [FakeTable(t) for t in tables]
    return _build


@pytest.fixture
def capture_queries(monkeypatch):
    """Replace influx_client._run_query; returns (queries list, set_response fn)."""
    queries: list[str] = []
    state = {"response": []}

    async def _fake_run_query(query: str):
        queries.append(query)
        resp = state["response"]
        return resp(query) if callable(resp) else resp

    monkeypatch.setattr(influx_client, "_run_query", _fake_run_query)

    def _set(response):
        state["response"] = response

    return queries, _set
