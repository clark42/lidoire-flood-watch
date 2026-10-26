"""Lidoire basin polygon: provenance, geometry, point-in-polygon, /api/geo/basin."""

import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.services import basin
from app.services.collectors.opera_radar import LaeaProjection

OPERA_LAEA = LaeaProjection(lat0=55.0, lon0=10.0, false_easting=1950000.0, false_northing=-2100000.0)


def test_basin_file_is_small_and_documents_source_and_licence():
    assert basin.BASIN_FILE.stat().st_size < 30_000
    props = basin.basin_properties()
    assert "BD TOPAGE" in props["source"] and "sandre.eaufrance.fr" in props["source_url"]
    assert "Licence Ouverte" in props["licence"]
    assert len(props["source_ids"]) == 5
    assert props["area_km2"] == 230.8


def test_basin_area_matches_sage_figure():
    # LAEA is equal-area: the projected ring area is the true area (~231 km2, SAGE)
    ring = basin.basin_ring()
    xs, ys = OPERA_LAEA.forward([p[0] for p in ring], [p[1] for p in ring])
    area_km2 = basin.ring_area_m2(zip(xs, ys)) / 1e6
    assert abs(area_km2 - 230.8) < 1.0
    assert ring[0] == ring[-1]


def test_basin_contains_the_mill_and_the_ensemble_points():
    settings = Settings(_env_file=None)
    assert basin.point_in_basin(settings.moulin_lon, settings.moulin_lat)
    for p in settings.ensemble_rain_points:
        assert basin.point_in_basin(p["lon"], p["lat"]), p["id"]
    # Bergerac, Libourne: outside
    assert not basin.point_in_basin(0.4836, 44.8531)
    assert not basin.point_in_basin(-0.2333, 44.9167)


def test_points_in_polygon_unit_square():
    ring = [(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]
    xs = np.array([[0.5, 1.5], [0.25, -0.1]])
    ys = np.array([[0.5, 0.5], [0.99, 0.5]])
    assert basin.points_in_polygon(xs, ys, ring).tolist() == [[True, False], [True, False]]


def test_geo_basin_route_serves_the_geojson():
    from app.api import router

    app = FastAPI()
    app.include_router(router, prefix="/api")
    body = TestClient(app).get("/api/geo/basin").json()
    assert body["type"] == "FeatureCollection"
    feature = body["features"][0]
    assert feature["geometry"]["type"] == "Polygon"
    assert feature["properties"]["attribution"] == "Sandre - BD TOPAGE"
