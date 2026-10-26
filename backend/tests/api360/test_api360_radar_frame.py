"""Radar frame PNG: writer, colour ramp, resampling on a synthetic OPERA raster, routes.

Uses the synthetic GeoTIFF of tests/probabilistic/fixtures (64 x 48 px on the
OPERA grid, 16 px tiles, band 1 = 0.1 * col + 0.01 * row in local indices,
rows 0-3 NaN, rows 44-47 / cols 0-7 = -9999000). No network.
"""

import pathlib
import struct
import zlib
from datetime import datetime, timedelta

import httpx
import numpy as np
import pytest
import respx
from fastapi import FastAPI

from app.api import router as api_router
from app.api import realtime as realtime_api
from app.services import basin
from app.services import radar_frame as rf
from app.services.collectors import opera_radar as radar

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "probabilistic" / "fixtures"
P1 = (FIXTURES / "opera_synthetic_p1.tif").read_bytes()
P3 = (FIXTURES / "opera_synthetic_p3.tif").read_bytes()
NOW = datetime(2026, 10, 26, 21, 7)
T = datetime(2026, 10, 26, 20, 45)


def expected_band():
    rows = np.arange(48)[:, None]
    cols = np.arange(64)[None, :]
    rain = (0.1 * cols + 0.01 * rows).astype(np.float32).astype(float)
    rain[0:4, :] = np.nan
    rain[44:48, 0:8] = -9999000.0
    return rain


def decode_png(data: bytes) -> np.ndarray:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, chunks = 8, {}
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF
        chunks.setdefault(kind, b"")
        chunks[kind] += body
        pos += 12 + length
    w, h, depth, ctype, _, _, _ = struct.unpack(">IIBBBBB", chunks[b"IHDR"])
    assert (depth, ctype) == (8, 6) and b"IEND" in chunks
    raw = zlib.decompress(chunks[b"IDAT"])
    rows = np.frombuffer(raw, dtype=np.uint8).reshape(h, 1 + 4 * w)
    assert (rows[:, 0] == 0).all()
    return rows[:, 1:].reshape(h, w, 4)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    rf._frame_cache.clear()
    monkeypatch.setattr(rf, "_utcnow", lambda: NOW)
    monkeypatch.setattr(realtime_api, "_utcnow", lambda: NOW)
    yield
    rf._frame_cache.clear()


# --- Pure ------------------------------------------------------------------------

def test_radar_frame_png_writer_roundtrip():
    rgba = np.zeros((3, 5, 4), dtype=np.uint8)
    rgba[1, 2] = (10, 20, 30, 255)
    rgba[2, 4] = (1, 2, 3, 4)
    assert (decode_png(rf.png_bytes(rgba)) == rgba).all()
    with pytest.raises(ValueError):
        rf.png_bytes(np.zeros((3, 5, 3), dtype=np.uint8))


def test_radar_frame_colour_ramp_transparent_at_zero():
    mm = np.array([[np.nan, 0.0, 0.05, 0.1], [0.7, 3.0, 40.0, -np.inf]])
    rgba = rf.colorize(mm)
    assert (rgba[0, :3, 3] == 0).all()                     # undetect, 0 and < 0.1 mm: transparent
    assert tuple(rgba[0, 3]) == rf.COLOR_RAMP[0][1]
    assert tuple(rgba[1, 0]) == rf.COLOR_RAMP[1][1]        # 0.5 <= 0.7 < 1
    assert tuple(rgba[1, 1]) == rf.COLOR_RAMP[3][1]        # 2 <= 3 < 4
    assert tuple(rgba[1, 2]) == rf.COLOR_RAMP[-1][1]
    assert tuple(rgba[1, 3]) == rf.NO_COVERAGE_RGBA


def test_radar_frame_bbox_contains_basin_with_margin():
    ring = basin.basin_ring()
    west, south, east, north = rf.frame_bbox(ring, 10.0)
    lons, lats = [p[0] for p in ring], [p[1] for p in ring]
    assert west < min(lons) - 0.12 and east > max(lons) + 0.12
    assert south < min(lats) - 0.089 and north > max(lats) + 0.089
    assert rf.grid_shape((0.0, 44.0, 0.5, 44.3), 0.005) == (60, 100)


def test_radar_frame_time_parsing():
    assert rf.parse_frame_time(None, NOW) is None
    assert rf.parse_frame_time("latest", NOW) is None
    assert rf.parse_frame_time("2026-10-26T20:52:30Z", NOW) == datetime(2026, 10, 26, 20, 45)
    assert rf.parse_frame_time("2026-10-26T22:52:30+02:00", NOW) == datetime(2026, 10, 26, 20, 45)
    with pytest.raises(ValueError):
        rf.parse_frame_time("2026-10-27T03:00Z", NOW)
    with pytest.raises(ValueError):
        rf.parse_frame_time("garbage", NOW)
    with pytest.raises(rf.FrameNotAvailable):
        rf.parse_frame_time("2026-10-24T12:00Z", NOW)


# --- Rendering on the synthetic raster -------------------------------------------

def _serve(buf: bytes):
    def handler(request):
        rng = request.headers.get("Range")
        if not rng:
            return httpx.Response(200, content=buf)
        start, end = (int(v) for v in rng.split("=")[1].split("-"))
        return httpx.Response(206, content=buf[start:end + 1])
    return handler


@respx.mock
@pytest.mark.parametrize("buf", [P1, P3], ids=["predictor1", "predictor3"])
async def test_radar_frame_renders_the_synthetic_raster(buf):
    route = respx.get(radar.image_url(T)).mock(side_effect=_serve(buf))
    png, meta = await rf.render_frame(T)
    img = decode_png(png)

    bbox, px = rf.frame_geometry()
    h, w = rf.grid_shape(bbox, px)
    assert img.shape == (h, w, 4) and (meta["height"], meta["width"]) == (h, w)
    info = radar.parse_geotiff_header(buf)
    rows, cols, inside = rf.sample_indices(info, bbox, px)
    assert inside.all()  # the basin + 10 km fits in the synthetic window
    expected = rf.colorize(expected_band()[rows, cols])
    assert (img == expected).all()
    # Values 0.1 * col: 1.3-4.1 mm in the window, every pixel coloured
    assert (img[..., 3] > 0).all()
    assert meta["available"] is True and meta["max_mm"] == pytest.approx(expected_band()[rows, cols].max(), abs=0.01)
    assert meta["bbox"] == list(bbox) and meta["time"] == T
    assert meta["url"] == "/api/realtime/radar/frame.png?t=2026-10-26T20:45Z"
    # Header + only the tiles under the window, all as range requests
    n_tiles = len(rf.tiles_for_indices(info, rows, cols, inside))
    assert route.call_count == 1 + n_tiles < 1 + len(info.tile_offsets)
    assert all("Range" in c.request.headers for c in route.calls)

    # Cached per frame time
    again, _ = await rf.render_frame(T)
    assert again == png and route.call_count == 1 + n_tiles


@respx.mock
async def test_radar_frame_missing_image_and_upstream_error():
    respx.get(radar.image_url(T)).mock(return_value=httpx.Response(404))
    with pytest.raises(rf.FrameNotAvailable):
        await rf.render_frame(T)
    t2 = T - timedelta(minutes=15)
    respx.get(radar.image_url(t2)).mock(return_value=httpx.Response(503))
    with pytest.raises(rf.FrameUpstreamError):
        await rf.render_frame(t2)
    assert rf._breaker.allow() is False
    assert len(rf._frame_cache) == 0


# --- Routes ---------------------------------------------------------------------

def _client():
    app = FastAPI()
    app.include_router(api_router, prefix="/api")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@respx.mock
async def test_radar_frame_routes(monkeypatch):
    respx.get(radar.image_url(T)).mock(side_effect=_serve(P1))

    async def latest(now):
        return T

    monkeypatch.setattr(rf, "latest_frame_time", latest)
    async with _client() as client:
        png = await client.get("/api/realtime/radar/frame.png")
        explicit = await client.get("/api/realtime/radar/frame.png", params={"t": "2026-10-26T20:50Z"})
        meta = await client.get("/api/realtime/radar/frame.json")
        old = await client.get("/api/realtime/radar/frame.png", params={"t": "2026-10-20T00:00Z"})
        bad = await client.get("/api/realtime/radar/frame.json", params={"t": "nope"})
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert png.headers["x-radar-time"] == "2026-10-26T20:45:00Z"
    assert png.headers["cache-control"] == "public, max-age=300"
    assert len(png.headers["x-radar-bbox"].split(",")) == 4
    assert explicit.status_code == 200 and "immutable" in explicit.headers["cache-control"]
    assert explicit.content == png.content
    body = meta.json()
    assert body["time"] == "2026-10-26T20:45:00Z" and body["accumulation_start"] == "2026-10-26T19:45:00Z"
    assert body["bbox"] == [float(v) for v in png.headers["x-radar-bbox"].split(",")]
    assert body["legend"][0] == {"min_mm": 0.1, "rgba": list(rf.COLOR_RAMP[0][1])}
    assert "CC BY 4.0" in body["attribution"] and body["model_input"] is False
    assert old.status_code == 404 and bad.status_code == 422


async def test_radar_frame_route_without_recent_image(monkeypatch):
    async def latest(now):
        return None

    monkeypatch.setattr(rf, "latest_frame_time", latest)
    async with _client() as client:
        resp = await client.get("/api/realtime/radar/frame.png")
    assert resp.status_code == 404


async def test_radar_frame_latest_time_from_stored_points(monkeypatch):
    from app.services import probabilistic_store

    async def stored(start, end):
        return [{"timestamp": T - timedelta(minutes=15)}, {"timestamp": T}]

    monkeypatch.setattr(probabilistic_store, "query_radar_points", stored)
    assert await rf.latest_frame_time(NOW) == T
