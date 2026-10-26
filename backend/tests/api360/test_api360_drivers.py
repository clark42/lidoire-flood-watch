"""Episode drivers and analogs (stubbed InfluxDB, labels and observations)."""

from datetime import datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from app.api import geo as geo_api
from app.api import risk as risk_api
from app.api import router as api_router
from app.models.schemas import EpisodeLabel, EpisodeLabelSet, Observation, ObservationKind
from app.services import episode_drivers as drivers
from app.services import labels as labels_service
from app.services import observations as observations_service

NOW = datetime(2026, 2, 12, 16, 0)
T_FLOOD = datetime(2024, 2, 26, 12, 0)
T_FP = datetime(2024, 5, 9, 6, 0)
T_NEAR = datetime(2024, 3, 30, 12, 0)

# Values at t: Pessac max, Libourne max, Eyraud max, rain mm/h (constant), piezo
SCENARIOS = {
    T_FLOOD: {"pessac": 4.4, "libourne": 5.2, "eyraud": 26.0, "rain": 0.8, "piezo": 7.3},
    T_FP: {"pessac": 3.6, "libourne": 4.1, "eyraud": 8.0, "rain": 0.3, "piezo": 6.6},
    T_NEAR: {"pessac": 3.9, "libourne": 4.8, "eyraud": 12.0, "rain": 0.5, "piezo": 6.9},
    NOW: {"pessac": 4.3, "libourne": 5.1, "eyraud": 25.0, "rain": 0.75, "piezo": 7.25},
}
CALM = {"pessac": 1.0, "libourne": 3.0, "eyraud": 0.3, "rain": 0.0, "piezo": 5.5}

HISTORY = {
    "pessac": [1.0 + 0.04 * i for i in range(100)],     # 1.00 .. 4.96
    "eyraud": [0.3 * i for i in range(100)],             # 0 .. 29.7
    "piezo": [5.0 + 0.025 * i for i in range(100)],      # 5.0 .. 7.475
}


def _series_name(measurement, tags):
    if measurement == "river_level":
        return {"P555001001": "pessac", "P577001001": "libourne"}[tags["station_id"]]
    if measurement == "river_flow":
        return "eyraud"
    if measurement == "rainfall":
        return "rain"
    if measurement == "groundwater":
        return "piezo"
    raise AssertionError(measurement)


def _window_points(name, t, sc):
    if name == "rain":
        # 72 hourly points in the window + one just outside
        return [(t - timedelta(hours=h), sc["rain"]) for h in range(0, 73)]
    if name == "piezo":
        return [(t - timedelta(hours=30), sc["piezo"] - 0.1), (t - timedelta(hours=6), sc["piezo"])]
    peak = sc[name]
    pts = [(t - timedelta(hours=h), peak - 0.3) for h in range(0, 72)]
    pts.append((t - timedelta(hours=2), peak))
    pts.append((t - timedelta(hours=13), peak + 2.0))  # outside the 12.5 h envelope
    return sorted(pts)


class FakeStore:
    def __init__(self):
        self.calls = []
        self.fail = set()

    async def query(self, measurement, field, tags, start, end, every=None, fn="max"):
        name = _series_name(measurement, tags)
        self.calls.append((name, every, fn, start, end))
        if name in self.fail:
            raise ConnectionError(f"{name} down")
        if every == "1d":
            if name == "rain":
                day0 = datetime(2023, 10, 1)
                return [(day0 + timedelta(days=i), float(i % 10)) for i in range(150)]
            if name == "libourne":
                return []
            return [(datetime(2023, 1, 1) + timedelta(days=i), v) for i, v in enumerate(HISTORY[name])]
        t = end - timedelta(seconds=1)
        sc = SCENARIOS.get(t, CALM)
        return [(ts, v) for ts, v in _window_points(name, t, sc) if start <= ts < end]


def _label(key, t, outcome, wh=None):
    return EpisodeLabel(key=key, peak_time=t, outcome=outcome, severity=3, water_height_cm=wh,
                        source="test", notes=f"note {key}")


LABELS = EpisodeLabelSet(
    version=1, updated_at="2026-10-26", description="test", episodes=[
        _label("2024-02-26", T_FLOOD, "flooded", 25),
        _label("2024-05-09", T_FP, "false_positive"),
        _label("2024-03-30", T_NEAR, "near_miss"),
    ],
)


def _obs(i, t, photo):
    return Observation(id=i, observed_at=t, kind=ObservationKind.FLOODED, has_photo=photo,
                       created_at=t, source="user")


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()

    async def no_glofas(start, end):
        return {T_FLOOD.date(): 31.5}

    observations = [
        _obs(1, T_FLOOD + timedelta(hours=3), True),
        _obs(2, T_FLOOD - timedelta(hours=20), False),
        _obs(3, T_FLOOD + timedelta(days=4), True),   # outside the window
    ]
    obs_calls = []

    async def fake_list(start=None, end=None, limit=500):
        obs_calls.append((start, end))
        return [o for o in observations if start <= o.observed_at <= end]

    monkeypatch.setattr(drivers, "query_points", fake.query)
    monkeypatch.setattr(drivers, "_glofas_daily", no_glofas)
    monkeypatch.setattr(drivers, "_utcnow", lambda: NOW)
    monkeypatch.setattr(labels_service, "load_label_set", lambda path=None: LABELS)
    monkeypatch.setattr(observations_service, "list_observations", fake_list)
    monkeypatch.setattr(geo_api, "_utcnow", lambda: NOW)
    for cache in (drivers._history_cache, drivers._drivers_cache, risk_api._analogs_cache):
        cache.clear()
    fake.obs_calls = obs_calls
    yield fake
    for cache in (drivers._history_cache, drivers._drivers_cache, risk_api._analogs_cache):
        cache.clear()


# --- Pure ------------------------------------------------------------------------

def test_drivers_rolling_sums_wet_season_complete_windows():
    daily = [(datetime(2024, 4, 28) + timedelta(days=i), 1.0 + i) for i in range(6)]  # 28/04 -> 03/05
    # Windows ending 30/04 (1+2+3) only: 28 and 29/04 incomplete, May excluded
    assert drivers.rolling_sums(daily, 3) == [6.0]
    assert drivers.rolling_sums(daily, 3, months=None) == [6.0, 9.0, 12.0, 15.0]


def test_drivers_analog_distance_rms_on_common_axes():
    a = {"pessac": 90.0, "eyraud_q": 80.0, "rain_72h": None, "piezo": 70.0}
    b = {"pessac": 60.0, "eyraud_q": 40.0, "rain_72h": 50.0, "piezo": None}
    dist, used = drivers.analog_distance(a, b, ["pessac", "eyraud_q", "rain_72h", "piezo"], 2)
    assert used == ["pessac", "eyraud_q"]
    assert dist == pytest.approx(((30 ** 2 + 40 ** 2) / 2) ** 0.5, abs=0.05)
    assert drivers.analog_distance(a, b, ["pessac", "eyraud_q", "rain_72h", "piezo"], 3) == (None, used)


def test_drivers_rain_sum_flags_incomplete_windows():
    t = datetime(2024, 1, 1, 12)
    pts = [(t - timedelta(hours=h), 1.0) for h in range(0, 30)]
    r = drivers.rain_sum(pts, t, 72)
    assert r == {"mm": 30.0, "hours_found": 30, "hours_expected": 72, "complete": False}
    assert drivers.rain_sum([], t, 72)["mm"] is None


def test_drivers_flux_builder_validates_and_merges_series():
    q = drivers.build_points_query("river_flow", "q_m3s", {"key": "EYRAUD"},
                                   datetime(2024, 1, 1), datetime(2024, 1, 2), every="1d", fn="max")
    assert 'r.key == "EYRAUD"' in q and "aggregateWindow(every: 1d, fn: max" in q
    assert 'group(columns: ["_measurement", "_field"])' in q
    with pytest.raises(ValueError):
        drivers.build_points_query("river_flow", "q_m3s", {"key": 'EY") or true //'},
                                   datetime(2024, 1, 1), datetime(2024, 1, 2))


# --- /api/episodes/drivers -----------------------------------------------------------

async def test_drivers_values_windows_and_percentiles(store):
    body = await drivers.get_episode_drivers()
    assert body["model_input"] is False and body["partial"] is False
    assert [e["key"] for e in body["episodes"]] == ["2024-02-26", "2024-05-09", "2024-03-30"]
    flood = body["episodes"][0]
    v = flood["values"]
    assert flood["peak_time"] == "2024-02-26T12:00:00Z" and flood["outcome"] == "flooded"
    assert flood["water_height_cm"] == 25 and flood["notes"] == "note 2024-02-26"
    # 12.5 h envelopes ignore the higher value 13 h before the peak
    assert v["pessac_m"] == 4.4 and v["libourne_m"] == 5.2 and v["eyraud_q_m3s"] == 26.0
    assert v["rain_72h_mm"] == pytest.approx(72 * 0.8, abs=0.05) and v["rain_72h_hours_found"] == 72
    assert v["piezo_m_ngf"] == 7.3 and v["piezo_time"] == "2024-02-26T06:00:00Z"
    assert v["glofas_m3s"] == 31.5
    assert v["z_c_m_ngf"] == pytest.approx(0.8 * (3.33 + 4.4) + 0.2 * (-1.17 + 5.2), abs=1e-3)
    p = flood["percentiles"]
    assert p["pessac"] == pytest.approx(85.5, abs=0.6)    # 4.4 in 1.00..4.96 step 0.04
    assert p["eyraud_q"] == pytest.approx(87.0)
    assert p["piezo"] > 90 and p["rain_72h"] == 100.0
    assert p["glofas"] is None  # one stored value: below MIN_HISTORY
    assert body["windows"]["pessac_m"].startswith("max de H Pessac sur (t - 12.5 h")
    assert body["confluence"]["alpha_downstream_weight"] == 0.2
    # Causal: no raw query reads past the peak
    raw = [c for c in store.calls if c[1] is None]
    assert {c[4] - timedelta(seconds=1) for c in raw} == {T_FLOOD, T_FP, T_NEAR}  # stop = peak + 1 s
    assert {c[0] for c in raw} == {"pessac", "libourne", "eyraud", "rain", "piezo"}


async def test_drivers_cached_6h_and_partial_not_cached(store):
    await drivers.get_episode_drivers()
    n = len(store.calls)
    await drivers.get_episode_drivers()
    assert len(store.calls) == n

    drivers._drivers_cache.clear()
    drivers._history_cache.clear()
    store.fail.add("eyraud")
    body = await drivers.get_episode_drivers()
    assert body["partial"] is True and "eyraud_q" in body["errors"]
    assert body["episodes"][0]["values"]["eyraud_q_m3s"] is None
    assert body["episodes"][0]["values"]["pessac_m"] == 4.4
    assert len(drivers._drivers_cache) == 0 and len(drivers._history_cache) == 0


def _client():
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_drivers_route_not_shadowed_by_episode_id(store):
    async with _client() as client:
        resp = await client.get("/api/episodes/drivers")
    assert resp.status_code == 200
    assert len(resp.json()["episodes"]) == 3


# --- /api/risk/analogs ---------------------------------------------------------------

async def test_analogs_nearest_episode_with_observations(store):
    async with _client() as client:
        resp = await client.get("/api/risk/analogs", params={"at": "2026-02-12T16:00:00Z", "k": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert body["timestamp"] == "2026-02-12T16:00:00Z" and body["k"] == 2
    assert body["axes"] == ["pessac", "eyraud_q", "rain_72h", "piezo"]
    assert body["nothing_close"] is False and body["threshold"] == 30.0
    first, second = body["analogs"]
    assert first["key"] == "2024-02-26" and first["outcome"] == "flooded" and first["close"] is True
    assert first["distance"] < 5 and first["distance"] == body["nearest_distance"]
    assert first["water_height_cm"] == 25 and first["notes"] == "note 2024-02-26"
    assert set(first["axes_used"]) == {"pessac", "eyraud_q", "rain_72h", "piezo"}
    assert first["percentiles"]["pessac"] == pytest.approx(85.5, abs=0.6)
    assert first["percentile_diff"]["pessac"] == pytest.approx(
        first["percentiles"]["pessac"] - body["situation"]["percentiles"]["pessac"], abs=0.1)
    # Peak +/- 36 h: 2 observations, 1 with a photo (the one 4 days later is out)
    assert first["observations"] == {"count": 2, "with_photo": 1}
    assert second["key"] == "2024-03-30" and second["distance"] > first["distance"]
    assert body["situation"]["values"]["pessac_m"] == 4.3
    assert body["observations_available"] is True


async def test_analogs_nothing_close(store):
    # A calm situation: far from every labelled episode
    body = await drivers.get_analogs(datetime(2025, 8, 1, 12, 0), k=3)
    assert body["nothing_close"] is True
    assert body["nearest_distance"] > 30
    assert "Aucun episode" in body["message"]
    assert all(a["close"] is False for a in body["analogs"])
    assert len(body["analogs"]) == 3


async def test_analogs_without_enough_axes(store):
    store.fail.update({"eyraud", "rain", "piezo"})
    body = await drivers.get_analogs(datetime(2025, 8, 1, 12, 0), k=1)
    assert body["nearest_distance"] is None and body["nothing_close"] is True
    assert "non calculable" in body["message"]


async def test_analogs_observations_unavailable(monkeypatch, store):
    async def boom(**kwargs):
        raise ConnectionError("postgres down")

    monkeypatch.setattr(observations_service, "list_observations", boom)
    body = await drivers.get_analogs(NOW, k=1)
    assert body["observations_available"] is False and body["analogs"][0]["observations"] is None


async def test_analogs_route_validates_k(store):
    async with _client() as client:
        assert (await client.get("/api/risk/analogs", params={"k": 0})).status_code == 422
        assert (await client.get("/api/risk/analogs", params={"at": "2099-01-01"})).status_code == 422
