"""Lidoire long profile: Z_c estimate, backwater reach, /api/geo/profile (synthetic profile, no InfluxDB)."""

from datetime import datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from app.api import geo as geo_api
from app.api import router as api_router
from app.config import get_settings
from app.services import episode_drivers as drivers
from app.services import lidoire_profile as lp

T = datetime(2024, 2, 26, 11, 0)

# Synthetic profile: confluence at 0 m (z 2.0), a step (weir) at 300 m, mill at 500 m
SYNTH_POINTS = [
    {"d_m": 0.0, "z_m": 2.0, "lon": -0.034, "lat": 44.852},
    {"d_m": 100.0, "z_m": 3.0, "lon": -0.033, "lat": 44.853},
    {"d_m": 200.0, "z_m": 4.0, "lon": -0.032, "lat": 44.854},
    {"d_m": 300.0, "z_m": 8.0, "lon": -0.031, "lat": 44.855},
    {"d_m": 400.0, "z_m": 9.0, "lon": -0.030, "lat": 44.856},
    {"d_m": 500.0, "z_m": 12.0, "lon": -0.029, "lat": 44.857},
    {"d_m": 600.0, "z_m": 12.5, "lon": -0.028, "lat": 44.858},
]
SYNTH_PROFILE = {
    "metadata": {
        "licence": "test", "line_source": "test", "elevation_source": "test",
        "confluence": {"lon": -0.034, "lat": 44.852, "z_m": 2.0},
        "mill": {"lon": -0.029, "lat": 44.857, "d_m": 500.0, "ground_z_m": 12.0},
    },
    "points": SYNTH_POINTS,
}


# --- Pure helpers --------------------------------------------------------------

def test_profile_backwater_reach_interpolates_the_crossing():
    reach = lp.backwater_reach(SYNTH_POINTS, 3.5)
    assert reach["status"] == "partial"
    assert reach["distance_m"] == pytest.approx(150.0)  # between 100 m (3.0) and 200 m (4.0)
    assert reach["z_m"] == 3.5
    assert reach["lon"] == pytest.approx(-0.0325) and reach["lat"] == pytest.approx(44.8535)
    assert reach["label"] == lp.APPROX_LABEL


def test_profile_backwater_reach_is_the_furthest_point_under_z_c():
    # A low point upstream of a high one still counts (literal definition)
    pts = [dict(p) for p in SYNTH_POINTS]
    pts[2]["z_m"] = 6.0   # 200 m above Z_c ...
    pts[3]["z_m"] = 4.5   # ... but 300 m below it
    reach = lp.backwater_reach(pts, 5.0)
    assert reach["distance_m"] == pytest.approx(300.0 + 100.0 * (5.0 - 4.5) / (9.0 - 4.5), abs=0.1)
    # ... while the water connected to the confluence stops at the 200 m rise
    assert reach["connected_distance_m"] == pytest.approx(100.0 + 100.0 * (5.0 - 3.0) / (6.0 - 3.0), abs=0.1)
    assert reach["disconnected_low_points"] is True


def test_profile_backwater_reach_edge_cases():
    assert lp.backwater_reach(SYNTH_POINTS, None)["status"] == "unknown"
    none = lp.backwater_reach(SYNTH_POINTS, 1.5)
    assert none["status"] == "none" and none["distance_m"] == 0.0 and none["connected_distance_m"] == 0.0
    whole = lp.backwater_reach(SYNTH_POINTS, 13.0)
    assert whole["status"] == "whole_profile" and whole["distance_m"] == 600.0
    assert whole["connected_distance_m"] == 600.0 and whole["disconnected_low_points"] is False
    exact = lp.backwater_reach(SYNTH_POINTS, 4.0)
    assert exact["distance_m"] == pytest.approx(200.0) == exact["connected_distance_m"]
    # Confluence sample above Z_c but a low point upstream: nothing connected
    pts = [dict(p) for p in SYNTH_POINTS]
    pts[0]["z_m"] = 3.5
    pocket = lp.backwater_reach(pts, 3.2)
    assert pocket["connected_distance_m"] == 0.0 and pocket["distance_m"] > 100.0


def test_profile_reach_relative_to_mill():
    assert lp.reach_relative_to_mill({"distance_m": 150.0, "connected_distance_m": 120.0}, 500.0) == {
        "distance_below_mill_m": 350.0, "reaches_mill": False, "connected_distance_below_mill_m": 380.0}
    assert lp.reach_relative_to_mill({"distance_m": 600.0}, 500.0)["reaches_mill"] is True
    assert lp.reach_relative_to_mill({"distance_m": None}, 500.0)["reaches_mill"] is None


def test_profile_confluence_stage_weights_and_fallbacks():
    z, notes = lp.confluence_stage(3.8, 5.2, 3.33, -1.17, 0.2)
    assert z == pytest.approx(0.8 * (3.33 + 3.8) + 0.2 * (-1.17 + 5.2), abs=1e-3)
    assert notes == []
    z, notes = lp.confluence_stage(3.8, None, 3.33, -1.17, 0.2)
    assert z == pytest.approx(7.13) and "Libourne" in notes[0]
    assert lp.confluence_stage(None, 5.2, 3.33, -1.17, 0.2)[0] is None


def test_profile_envelope_is_causal():
    pts = [(T - timedelta(hours=13), 9.0), (T - timedelta(hours=6), 4.0), (T, 3.5), (T + timedelta(hours=1), 8.0)]
    assert lp.envelope_max(pts, T, 12.5) == 4.0  # 13 h ago is out, the future is ignored
    assert lp.latest_value(pts, T, 3.0) == (T, 3.5)
    assert lp.latest_value(pts, T - timedelta(hours=7), 3.0) is None


def test_profile_polyline_helpers():
    line = [(0.0, 45.0), (0.0, 45.01), (0.01, 45.01)]
    cum = lp.cumulative_m(line)
    assert cum[1] == pytest.approx(1112.0, rel=1e-3)
    d, off = lp.project_onto(line, 0.0005, 45.005)
    assert d == pytest.approx(556.0, rel=1e-2) and off == pytest.approx(39.0, rel=0.05)
    assert lp.point_at(line, cum, cum[1]) == pytest.approx((0.0, 45.01))


def test_profile_committed_file_is_consistent():
    profile = lp.load_profile()
    meta, pts = profile["metadata"], profile["points"]
    assert "Licence Ouverte" in meta["licence"] and "RGE ALTI" in meta["elevation_source"]
    assert "TOPAGE" in meta["line_source"]
    ds = [p["d_m"] for p in pts]
    assert ds == sorted(ds) and ds[0] == 0.0
    assert all(abs(b - a - 100.0) < 1e-6 for a, b in zip(ds, ds[1:-1]))
    # From the confluence to ~1 km above the mill
    assert ds[-1] == pytest.approx(meta["mill"]["d_m"] + meta["upstream_of_mill_m"], abs=1.0)
    assert 1.0 < pts[0]["z_m"] < 4.0 and 10.0 < pts[-1]["z_m"] < 15.0
    assert meta["mill"]["offset_from_line_m"] < 50
    assert abs(meta["mill"]["ground_z_m"] - get_settings().mill_ground_z_m_ngf) < 0.5


def test_profile_config_values():
    s = get_settings()
    assert s.ngf_station_zeros_m == {"P555001001": 3.33, "P577001001": -1.17}
    assert s.confluence_downstream_weight == 0.2
    assert s.mill_ground_z_m_ngf == 12.0


# --- Route -----------------------------------------------------------------------

@pytest.fixture
def stub_levels(monkeypatch):
    """Pessac / Libourne points around T (station datum, m)."""
    s = get_settings()
    series = {
        s.confluence_upstream_station: [(T - timedelta(hours=h), 3.8 - 0.05 * h) for h in range(0, 14)],
        s.confluence_downstream_station: [(T - timedelta(hours=h), 5.0 - 0.4 * (h % 6)) for h in range(0, 14)],
    }
    calls = []

    async def fake_query(measurement, field, tags, start, end, every=None, fn="max"):
        calls.append((measurement, tags, start, end))
        pts = series.get(tags.get("station_id"), [])
        return [(ts, v) for ts, v in pts if start <= ts < end]

    monkeypatch.setattr(drivers, "query_points", fake_query)
    monkeypatch.setattr(drivers, "load_profile", lambda: SYNTH_PROFILE)
    monkeypatch.setattr(geo_api, "_utcnow", lambda: T + timedelta(hours=2))
    geo_api._profile_cache.clear()
    yield calls
    geo_api._profile_cache.clear()


def _client():
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_profile_route_payload(stub_levels):
    async with _client() as client:
        resp = await client.get("/api/geo/profile", params={"at": "2024-02-26T11:00:00Z"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["at"] == "2024-02-26T11:00:00Z" and body["model_input"] is False
    conf = body["confluence"]
    # Pessac max 3.8 (at T), Libourne max 5.0 over 12.5 h
    expected = round(0.8 * (3.33 + 3.8) + 0.2 * (-1.17 + 5.0), 3)
    assert conf["z_c_m_ngf"] == pytest.approx(expected)
    assert conf["label"] == "estime" and conf["alpha_downstream_weight"] == 0.2
    assert conf["ngf_zeros_m"] == {"P555001001": 3.33, "P577001001": -1.17}
    assert conf["inputs"]["pessac_max_m_ngf"] == pytest.approx(7.13)
    assert conf["instant"]["pessac_m"] == 3.8 and conf["instant"]["libourne_m"] == 5.0

    bw = body["backwater"]
    # Z_c ~ 6.47: between 200 m (4.0) and 300 m (8.0)
    assert bw["status"] == "partial"
    assert bw["distance_m"] == pytest.approx(200.0 + 100.0 * (expected - 4.0) / 4.0, abs=0.2)
    assert bw["reaches_mill"] is False
    assert bw["distance_below_mill_m"] == pytest.approx(500.0 - bw["distance_m"], abs=0.2)
    assert "approximation" in bw["label"] and bw["limits"]

    assert body["mill"]["ground_z_m_ngf"] == 12.0 and body["mill"]["d_m"] == 500.0
    assert body["margin"]["mill_ground_minus_z_c_m"] == pytest.approx(12.0 - expected, abs=0.01)
    assert len(body["profile"]["points"]) == len(SYNTH_POINTS)
    assert body["errors"] == []
    # Causal window: nothing after `at` is read
    assert all(end <= T + timedelta(seconds=1) for *_, end in stub_levels)


async def test_profile_route_without_points_and_cache(stub_levels):
    async with _client() as client:
        a = await client.get("/api/geo/profile", params={"at": "2024-02-26T11:00", "points": "false"})
        n = len(stub_levels)
        b = await client.get("/api/geo/profile", params={"at": "2024-02-26T11:00", "points": "false"})
    assert a.status_code == b.status_code == 200
    assert a.json()["profile"]["points"] is None and a.json()["profile"]["length_m"] == 600.0
    assert len(stub_levels) == n  # second call served from the cache


@pytest.mark.parametrize("at", ["not-a-date", "2099-01-01T00:00:00Z"])
async def test_profile_route_rejects_bad_at(stub_levels, at):
    async with _client() as client:
        resp = await client.get("/api/geo/profile", params={"at": at})
    assert resp.status_code == 422


async def test_profile_route_influx_down(monkeypatch, stub_levels):
    async def boom(*a, **k):
        raise ConnectionError("influx down")

    monkeypatch.setattr(drivers, "query_points", boom)
    async with _client() as client:
        resp = await client.get("/api/geo/profile", params={"at": "2024-02-26T11:00Z"})
    body = resp.json()
    assert resp.status_code == 200
    assert body["confluence"]["z_c_m_ngf"] is None and body["backwater"]["status"] == "unknown"
    assert set(body["errors"]) == {"pessac", "libourne"}
    assert len(geo_api._profile_cache) == 0  # failures are not cached
