"""Episode analyzer: stored data first, unknown tide coefficient, /patterns works."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import episodes as episodes_api
from app.models.schemas import EpisodeOutcome, FloodEpisode
from app.services import episode_analyzer_v2 as analyzer
from app.services import influx_client
from app.services.collectors import tide as tide_collector

PEAK = datetime(2024, 2, 26, 12, 0)
UTC = timezone.utc


class _StoredData:
    """influx_client stand-in: aware timestamps, as the real client returns them."""

    def __init__(self, with_tides: bool = False):
        self.with_tides = with_tides

    async def query_river_levels(self, station_id, start, end):
        if station_id not in ("P555001001", "P726151001"):
            return []
        out = []
        t = start
        while t < end:
            h = 4.0 + (t - start).total_seconds() / 3600 / 100
            out.append({"timestamp": t.replace(tzinfo=UTC), "height_m": h, "station_id": station_id,
                        "station_name": None, "river": None})
            t += timedelta(hours=1)
        return out

    async def query_rainfall(self, start, end, source=None):
        hours = int((end - start).total_seconds() // 3600)
        return [
            {"timestamp": (start + timedelta(hours=k)).replace(tzinfo=UTC), "precipitation_mm_h": 1.0,
             "precipitation_mm_24h": None, "source": "open-meteo", "city": "Moulin-Lidoire"}
            for k in range(hours + 1)
        ]

    async def query_tides(self, port, start, end):
        if not self.with_tides:
            return []
        pm = PEAK - timedelta(hours=1)
        return [
            {"timestamp": pm.replace(tzinfo=UTC), "height_m": 2.1, "coefficient": None, "tide_type": "PM", "port": port},
            {"timestamp": pm.replace(tzinfo=UTC), "height_m": None, "coefficient": 104, "tide_type": "PM", "port": port},
        ]


@pytest.fixture
def stored(monkeypatch):
    data = _StoredData()
    for name in ("query_river_levels", "query_rainfall", "query_tides"):
        monkeypatch.setattr(influx_client, name, getattr(data, name))

    async def no_network(*a, **k):
        raise AssertionError("stored data must be used before any external API")

    async def no_marine(*a, **k):
        return 0  # Open-Meteo Marine has nothing either

    monkeypatch.setattr(analyzer, "fetch_episode_context", no_network)
    monkeypatch.setattr(analyzer, "_fetch_historical_rainfall", no_network)
    monkeypatch.setattr(tide_collector, "fetch_and_store_tides_range", no_marine)
    return data


async def test_groundtruth_analyzer_unknown_tide_coefficient_is_none(stored):
    analysis = await analyzer.analyze_episode(PEAK)
    assert analysis.tides == []
    assert analysis.context.max_tide_coefficient is None  # was a hardcoded 70
    assert not any("marée" in c.lower() or "maree" in c.lower() for c in analysis.matched_criteria)
    # Stored series were used: Pessac max over the window, 24 h of 1 mm/h before the peak
    assert analysis.context.max_dordogne_level_m == pytest.approx(4.0 + 96 / 100)
    assert analysis.context.rain_24h_before_mm == pytest.approx(24.0)
    assert {lv.station_id for lv in analysis.river_levels} == {"P555001001", "P726151001"}
    assert analysis.river_levels[0].station_name == "Pessac-sur-Dordogne"


async def test_groundtruth_analyzer_uses_stored_tides(stored):
    stored.with_tides = True
    analysis = await analyzer.analyze_episode(PEAK)
    assert analysis.context.max_tide_coefficient == 104
    assert analysis.context.tide_phase_at_peak == "après PM"
    assert any("Grande marée" in c for c in analysis.matched_criteria)


async def test_groundtruth_analyzer_falls_back_to_hubeau_without_stored_levels(monkeypatch, stored):
    async def nothing(station_id, start, end):
        return []

    calls = []

    async def fake_context(date, hours_before=72, hours_after=24):
        calls.append((date, hours_before, hours_after))
        return {}

    monkeypatch.setattr(influx_client, "query_river_levels", nothing)
    monkeypatch.setattr(analyzer, "fetch_episode_context", fake_context)
    analysis = await analyzer.analyze_episode(PEAK, hours_before=48, hours_after=12)
    assert calls == [(PEAK, 48, 12)]
    assert analysis.context.max_dordogne_level_m == 0.0


# --- routes ------------------------------------------------------------------


def _episode(ep_id: int, peak: datetime, outcome=EpisodeOutcome.FLOODED) -> FloodEpisode:
    return FloodEpisode(
        id=ep_id, start_time=peak - timedelta(days=2), peak_time=peak, severity=4,
        description=f"ep {ep_id}", outcome=outcome, water_height_cm=25.0,
    )


@pytest.fixture
def client(monkeypatch, stored):
    episodes = {i: _episode(i, PEAK - timedelta(days=30 * i)) for i in (1, 2, 3)}

    async def get_all(limit=50, offset=0, min_severity=None):
        return list(episodes.values())

    async def get_one(episode_id):
        return episodes.get(episode_id)

    monkeypatch.setattr(episodes_api, "get_all_episodes", get_all)
    monkeypatch.setattr(episodes_api, "get_episode_by_id", get_one)
    app = FastAPI()
    app.include_router(episodes_api.router, prefix="/api/episodes")
    return TestClient(app)


def test_groundtruth_analyze_existing_episode_keeps_ground_truth(client):
    body = client.get("/api/episodes/analyze/2").json()
    assert body["episode"]["id"] == 2
    assert body["episode"]["outcome"] == "flooded"
    assert body["episode"]["water_height_cm"] == 25.0
    assert body["context"]["max_tide_coefficient"] is None


def test_groundtruth_patterns_succeeds_on_stored_episodes(client):
    # Used to answer "analysis_failed": the v1 analyzer compared naive episode
    # dates with the aware InfluxDB timestamps and every episode raised.
    body = client.get("/api/episodes/patterns").json()
    assert body["status"] == "success"
    patterns = body["patterns"]
    assert patterns["episode_count"] == 3
    assert patterns["failed_episodes"] == []
    assert patterns["rainfall_24h"]["count"] == 3
    assert patterns["tide_coefficient"] is None  # no tide data: unknown, not 70


def test_groundtruth_post_analyze_uses_the_single_analyzer(client, monkeypatch):
    seen = {}
    real = episodes_api.analyze_episode_v2

    async def spy(**kwargs):
        seen.update(kwargs)
        return await real(**kwargs)

    monkeypatch.setattr(episodes_api, "analyze_episode_v2", spy)
    resp = client.post("/api/episodes/analyze", params={
        "date": PEAK.isoformat(), "window_before_days": 2, "window_after_days": 1,
    })
    assert resp.status_code == 200
    assert seen["hours_before"] == 48 and seen["hours_after"] == 24
    assert resp.json()["context"]["max_tide_coefficient"] is None


def test_groundtruth_compare_tolerates_unknown_tide(client):
    resp = client.post("/api/episodes/compare", params={"dates": [PEAK.isoformat(), (PEAK - timedelta(days=9)).isoformat()]})
    assert resp.status_code == 200
    assert resp.json()["summary"]["tide_coef"] is None
