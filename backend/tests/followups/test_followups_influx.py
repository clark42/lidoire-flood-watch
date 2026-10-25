"""influx_client: batched writes, read-only query token, alias-aware rain cumul."""

import threading
from datetime import datetime, timedelta, timezone

import pytest
from influxdb_client import Point

from app.config import Settings
from app.services import influx_client as ic

T = datetime(2026, 10, 25, 12, 0)


class FakeWriteApi:
    def __init__(self, fail=False):
        self.calls = []
        self.threads = []
        self.fail = fail

    def write(self, bucket, org, record):
        self.threads.append(threading.current_thread().name)
        if self.fail:
            raise RuntimeError("influx down")
        self.calls.append(list(record) if isinstance(record, list) else [record])


# --- write_points_batch -------------------------------------------------------


async def test_followups_batch_is_one_threaded_write(monkeypatch):
    api = FakeWriteApi()
    monkeypatch.setattr(ic, "get_write_api", lambda: api)
    points = [
        ic.make_rainfall_point("open-meteo", 0.4, T, station_city="Moulin-Lidoire"),
        ic.make_tide_point("BORDEAUX", 2.5, T, tide_type="high", coefficient=80),
        {"measurement": "wind", "tags": {"location": "moulin"},
         "fields": {"speed_kmh": 12, "direction_deg": None}, "timestamp": T},
    ]
    assert await ic.write_points_batch(points) == 3
    assert len(api.calls) == 1 and len(api.calls[0]) == 3
    assert api.threads[0] != threading.main_thread().name
    # dict items follow write_point semantics (float numbers, None skipped)
    wind = ic.point_as_dict(api.calls[0][2])
    assert wind["fields"] == {"speed_kmh": 12.0}
    # tide coefficient keeps its integer type (same as write_tide)
    assert ic.point_as_dict(api.calls[0][1])["fields"]["coefficient"] == 80


async def test_followups_batch_chunks_and_skips_invalid(monkeypatch):
    api = FakeWriteApi()
    monkeypatch.setattr(ic, "get_write_api", lambda: api)
    points = [ic.make_point("x", {}, {"v": i}, T + timedelta(minutes=i)) for i in range(5)]
    points.append({"measurement": "x"})  # missing keys -> skipped
    assert await ic.write_points_batch(points, chunk_size=2) == 5
    assert [len(c) for c in api.calls] == [2, 2, 1]


async def test_followups_batch_failure_returns_zero(monkeypatch):
    monkeypatch.setattr(ic, "get_write_api", lambda: FakeWriteApi(fail=True))
    assert await ic.write_points_batch([ic.make_point("x", {}, {"v": 1}, T)]) == 0
    assert await ic.write_points_batch([]) == 0


async def test_followups_single_writers_unchanged(monkeypatch):
    api = FakeWriteApi()
    monkeypatch.setattr(ic, "get_write_api", lambda: api)
    assert await ic.write_rainfall("netatmo", None, T, precipitation_mm_24h=3, station_city="Flaujagues")
    d = ic.point_as_dict(api.calls[-1][0])
    assert d["fields"] == {"precipitation_mm_h": 0.0, "precipitation_mm_24h": 3.0}
    assert d["tags"] == {"source": "netatmo", "city": "Flaujagues"}


# --- read-only token ------------------------------------------------------------


class FakeClient:
    instances: list = []

    def __init__(self, url, token, org):
        self.token = token
        self.closed = False
        FakeClient.instances.append(self)

    def write_api(self, write_options=None):
        return ("write", self.token)

    def query_api(self):
        return ("query", self.token)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_clients(monkeypatch):
    FakeClient.instances = []
    monkeypatch.setattr(ic, "InfluxDBClient", FakeClient)
    ic.close_client()

    def _use(**overrides):
        settings = Settings(_env_file=None, **overrides)
        monkeypatch.setattr(ic, "get_settings", lambda: settings)
        return settings

    yield _use
    ic.close_client()


def test_followups_query_api_uses_read_token(fake_clients):
    fake_clients(influxdb_token="admin-token", influxdb_read_token="read-token")
    assert ic.get_query_api() == ("query", "read-token")
    assert ic.get_write_api() == ("write", "admin-token")
    assert sorted(c.token for c in FakeClient.instances) == ["admin-token", "read-token"]
    ic.close_client()
    assert all(c.closed for c in FakeClient.instances)


def test_followups_query_api_falls_back_to_write_token(fake_clients):
    fake_clients(influxdb_token="admin-token", influxdb_read_token="")
    assert ic.get_query_api() == ("query", "admin-token")
    assert ic.get_write_api() == ("write", "admin-token")
    assert len(FakeClient.instances) == 1  # one shared client


def test_followups_same_tokens_share_one_client(fake_clients):
    fake_clients(influxdb_token="tok", influxdb_read_token="tok")
    ic.get_write_api()
    ic.get_query_api()
    assert len(FakeClient.instances) == 1


# --- cumulative rain with legacy city aliases -----------------------------------


def _counter_rows(city, values_by_day, now):
    return [
        {"city": city, "_time": (now - timedelta(hours=24 * d, minutes=5)).replace(tzinfo=timezone.utc),
         "_value": v}
        for d, v in values_by_day.items()
    ]


async def test_followups_cumul_merges_alias_samples(monkeypatch, fake_query):
    queries, set_tables = fake_query
    monkeypatch.setattr(ic, "_utcnow", lambda: T)
    # Recent days under the canonical spelling, older ones under the raw name
    set_tables([
        _counter_rows("Saint-Rémy", {0: 5.0, 1: 3.0}, T),
        _counter_rows("Saint-Remy", {2: 1.0, 4: 10.0}, T),
    ])
    res = await ic.query_all_stations_rainfall_cumulative(
        ["Saint-Rémy"], aliases={"Saint-Rémy": ["Saint-Remy", "Saint-Rémy"]},
    )
    assert res == {"Saint-Rémy": {"rain_24h_mm": 5.0, "rain_72h_mm": 9.0, "rain_7d_mm": 19.0}}
    assert len(queries) == 1
    assert 'r.city == "Saint-Rémy" or r.city == "Saint-Remy"' in queries[0]


async def test_followups_cumul_without_alias_ignores_other_names(monkeypatch, fake_query):
    _, set_tables = fake_query
    monkeypatch.setattr(ic, "_utcnow", lambda: T)
    set_tables([_counter_rows("Saint-Remy", {0: 4.0}, T)])
    res = await ic.query_all_stations_rainfall_cumulative(["Saint-Rémy"])
    assert res["Saint-Rémy"]["rain_24h_mm"] is None
    # an unsafe alias is skipped, not interpolated
    res = await ic.query_all_stations_rainfall_cumulative(
        ["Saint-Rémy"], aliases={"Saint-Rémy": ['x") |> drop()']},
    )
    assert res["Saint-Rémy"]["rain_24h_mm"] is None


def test_followups_utcnow_is_naive_utc():
    now = ic._utcnow()
    assert now.tzinfo is None
    assert abs(now - datetime.now(timezone.utc).replace(tzinfo=None)) < timedelta(seconds=5)
    assert isinstance(ic.make_point("x", {}, {"v": 1}, now), Point)
