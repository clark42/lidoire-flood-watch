"""Shared fixtures for wave-1 follow-up tests (no InfluxDB / network needed)."""

import pytest

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


@pytest.fixture
def batch_writes(monkeypatch):
    """Capture influx_client.write_points_batch calls.

    Returns a list of batches; each batch is a list of point dicts
    ({measurement, tags, fields, timestamp}).
    """
    batches: list[list[dict]] = []

    async def fake_write_points_batch(points, chunk_size=5000):
        batches.append([
            p if isinstance(p, dict) else influx_client.point_as_dict(p) for p in points
        ])
        return len(points)

    monkeypatch.setattr(influx_client, "write_points_batch", fake_write_points_batch)
    return batches


@pytest.fixture
def forbid_single_writes(monkeypatch):
    """Fail the test if a collector falls back to point-by-point writes."""

    async def forbidden(*args, **kwargs):
        raise AssertionError("point-by-point InfluxDB write used instead of write_points_batch")

    for name in ("write_point", "write_rainfall", "write_tide"):
        monkeypatch.setattr(influx_client, name, forbidden)


@pytest.fixture
def fake_query(monkeypatch):
    """Replace influx_client._run_query with canned tables; returns the queries list."""
    queries: list[str] = []
    state = {"tables": []}

    async def _fake_run_query(query: str):
        queries.append(query)
        return [FakeTable(t) for t in state["tables"]]

    monkeypatch.setattr(influx_client, "_run_query", _fake_run_query)

    def _set(tables: list[list[dict]]):
        state["tables"] = tables

    return queries, _set


@pytest.fixture(autouse=True)
def _reset_influx_state():
    influx_client._rain_cumul_cache.clear()
    yield
    influx_client._rain_cumul_cache.clear()
