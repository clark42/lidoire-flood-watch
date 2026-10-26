"""Lidoire topographic catchment (polygon) and pure geometry helpers.

The polygon is ``app/data/lidoire_basin.geojson``: union of the 5 BD TOPAGE
elementary catchments of the Lidoire and the Lechout (Sandre WFS layer
``BassinVersantTopographique_FXX``), 230.8 km2, simplified to 25 m. Source,
licence and processing are in the feature ``properties``.

Used by the OPERA radar collector (basin mask), the ensemble rain points and
the map (``GET /api/geo/basin``). Never read by the risk model.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

BASIN_ID = "LIDOIRE"
BASIN_FILE = Path(__file__).resolve().parents[1] / "data" / "lidoire_basin.geojson"


@lru_cache
def basin_geojson() -> dict:
    """The basin FeatureCollection, as committed (read once)."""
    with BASIN_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def basin_properties() -> dict:
    return dict(basin_geojson()["features"][0]["properties"])


def basin_ring() -> list[tuple[float, float]]:
    """Exterior ring of the basin as (lon, lat) pairs (closed)."""
    geometry = basin_geojson()["features"][0]["geometry"]
    if geometry["type"] != "Polygon":
        raise ValueError(f"basin geometry must be a Polygon, got {geometry['type']}")
    return [(float(x), float(y)) for x, y in geometry["coordinates"][0]]


def points_in_polygon(
    xs: np.ndarray, ys: np.ndarray, ring: Sequence[tuple[float, float]],
) -> np.ndarray:
    """Even-odd (ray casting) test of many points against one closed ring.

    ``xs``/``ys`` are arrays of the same shape, in the ring's coordinates.
    Returns a boolean array of that shape. Points exactly on an edge may fall
    either side (irrelevant for pixel sub-samples).
    """
    xs = np.asarray(xs, dtype=float)
    ys = np.asarray(ys, dtype=float)
    inside = np.zeros(xs.shape, dtype=bool)
    pts = list(ring)
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    for (x1, y1), (x2, y2) in zip(pts[:-1], pts[1:]):
        if y1 == y2:
            continue
        crosses = (y1 > ys) != (y2 > ys)
        x_at = x1 + (ys - y1) * (x2 - x1) / (y2 - y1)
        inside ^= crosses & (xs < x_at)
    return inside


def point_in_basin(lon: float, lat: float) -> bool:
    return bool(points_in_polygon(np.array([lon]), np.array([lat]), basin_ring())[0])


def ring_area_m2(ring: Iterable[tuple[float, float]]) -> float:
    """Shoelace area of a ring in projected metres (absolute value)."""
    pts = list(ring)
    xs = np.array([p[0] for p in pts], dtype=float)
    ys = np.array([p[1] for p in pts], dtype=float)
    return float(abs(np.dot(xs, np.roll(ys, -1)) - np.dot(ys, np.roll(xs, -1))) / 2.0)
