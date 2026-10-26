"""OPERA radar: GeoTIFF reader, LAEA projection, basin mask, stats, collector, read side.

Fixtures (no network):
- ``opera_synthetic_p1.tif`` / ``_p3.tif``: 64 x 48 px, 16 px tiles, 2 float32
  samples, deflate, predictor 1 / 3, written by tifffile with the OPERA
  GeoKeys (LAEA 55N 10E) and placed on the OPERA grid (rows 1560-1607,
  cols 560-623) so that the basin is inside. Band 1 = 0.1 * col + 0.01 * row
  (local indices), rows 0-3 NaN (undetect), rows 44-47 / cols 0-7 = -9999000.
- ``opera_listing.xml``: a real bucket listing page, trimmed.
"""

import pathlib
from datetime import datetime, timedelta

import httpx
import numpy as np
import pytest
import respx

from app.services import basin, influx_client
from app.services.collectors import opera_radar as radar

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
P1 = (FIXTURES / "opera_synthetic_p1.tif").read_bytes()
P3 = (FIXTURES / "opera_synthetic_p3.tif").read_bytes()
H, W = 48, 64


def expected_band():
    rows = np.arange(H)[:, None]
    cols = np.arange(W)[None, :]
    rain = (0.1 * cols + 0.01 * rows).astype(np.float32).astype(float)
    rain[0:4, :] = np.nan
    rain[44:48, 0:8] = -9999000.0
    return rain


def decode_full(buf: bytes) -> np.ndarray:
    info = radar.parse_geotiff_header(buf)
    out = np.zeros((info.height, info.width, info.samples))
    for idx, (off, cnt) in enumerate(zip(info.tile_offsets, info.tile_byte_counts)):
        tr, tc = divmod(idx, info.tiles_across)
        tile = radar.decode_tile(info, buf[off:off + cnt])
        r0, c0 = tr * info.tile_height, tc * info.tile_width
        h = min(info.tile_height, info.height - r0)
        w = min(info.tile_width, info.width - c0)
        out[r0:r0 + h, c0:c0 + w] = tile[:h, :w]
    return out


@pytest.fixture(autouse=True)
def _reset_radar_state(monkeypatch):
    radar._mask_cache.clear()
    radar._stored_times.clear()
    monkeypatch.setattr(radar, "_stored_seeded", False)
    yield
    radar._mask_cache.clear()
    radar._stored_times.clear()


# --- Projection --------------------------------------------------------------

@pytest.mark.parametrize("lon,lat,x,y", [
    # Reference values from pyproj 3.8 "+proj=laea +lat_0=55 +lon_0=10 +x_0=1950000 +y_0=-2100000 +ellps=WGS84"
    (0.0015091, 44.8759811, 1159789.5865952205, -3169974.545592949),
    (10.0, 55.0, 1950000.0, -2100000.0),
    (-5.0, 40.0, 668193.0891402501, -3631649.4475884144),
    (20.0, 65.0, 2421684.9735028916, -952638.2254424805),
])
def test_radar_laea_forward_matches_pyproj(lon, lat, x, y):
    proj = radar.LaeaProjection(55.0, 10.0, 1950000.0, -2100000.0)
    px, py = proj.forward(lon, lat)
    assert float(px) == pytest.approx(x, abs=1e-3)
    assert float(py) == pytest.approx(y, abs=1e-3)


# --- TIFF reader -------------------------------------------------------------

def test_radar_header_parses_geotiff_tags():
    info = radar.parse_geotiff_header(P1)
    assert (info.width, info.height, info.tile_width, info.tile_height) == (64, 48, 16, 16)
    assert info.samples == 2 and info.bits == 32 and info.sample_format == 3
    assert info.compression == 8 and info.predictor == 1
    assert info.nodata == -9999000.0
    assert info.pixel_scale == (2000.0, 2000.0)
    assert info.origin == (-1000.0 + 560 * 2000.0, 1000.0 - 1560 * 2000.0)
    assert info.projection == radar.LaeaProjection(55.0, 10.0, 1950000.0, -2100000.0)
    assert len(info.tile_offsets) == 4 * 3


def test_radar_header_needs_more_bytes():
    with pytest.raises(radar.NeedMoreBytes):
        radar.parse_geotiff_header(P1[:16])
    with pytest.raises(radar.UnsupportedTiff):
        radar.parse_geotiff_header(b"GIF89a.........")


@pytest.mark.parametrize("buf", [P1, P3], ids=["predictor1", "predictor3"])
def test_radar_tiles_decode_to_the_written_values(buf):
    data = decode_full(buf)
    expected = expected_band()
    np.testing.assert_array_equal(np.isnan(data[..., 0]), np.isnan(expected))
    np.testing.assert_allclose(np.nan_to_num(data[..., 0]), np.nan_to_num(expected), rtol=0, atol=1e-6)
    np.testing.assert_allclose(data[..., 1], 0.5)


# --- Mask and statistics -----------------------------------------------------

def test_radar_basin_mask_area_on_synthetic_raster():
    info = radar.parse_geotiff_header(P1)
    mask = radar.build_basin_mask(info, basin.basin_ring())
    # Pixels are 2 km x 2 km: sum of weights x 4 km2 = basin area (230.8 km2)
    assert mask.weights.sum() * 4.0 == pytest.approx(230.8, rel=0.01)
    assert mask.weights.max() == 1.0 and mask.weights.min() == 0.0
    # Window on the OPERA grid: same as on the real 1900 x 2200 composite
    assert (mask.row0 + 1560, mask.col0 + 560) == (1580, 578)
    assert radar.tiles_for_mask(info, mask) == [5, 6]  # local rows 20-26, cols 18-35


def test_radar_basin_stats_undetect_nodata_and_weights():
    weights = np.array([[1.0, 0.5], [0.25, 0.0]])
    rain = np.array([[2.0, np.nan], [-9999000.0, 50.0]])
    stats = radar.basin_stats(rain, weights, quality=np.full((2, 2), 0.8), nodata=-9999000.0)
    # NaN = undetect = 0 mm; nodata excluded; weight 0 pixel ignored (even at 50 mm)
    assert stats["basin_mean_mm"] == pytest.approx(2.0 / 1.5, abs=1e-3)
    assert stats["max_pixel_mm"] == 2.0
    assert stats["coverage"] == pytest.approx(1.5 / 1.75, abs=1e-4)
    assert stats["pixels"] == 3
    assert stats["quality"] == 0.8


def test_radar_basin_stats_without_coverage():
    stats = radar.basin_stats(np.full((2, 2), -9999000.0), np.ones((2, 2)))
    assert stats["basin_mean_mm"] is None and stats["coverage"] == 0.0


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
async def test_radar_read_basin_rain_with_range_requests(buf):
    url = "https://radar.test/OPERA@20261026T2045@0@ACRR.tiff"
    route = respx.get(url).mock(side_effect=_serve(buf))
    async with httpx.AsyncClient() as client:
        stats = await radar.read_basin_rain(client, url)

    info = radar.parse_geotiff_header(buf)
    mask = radar.build_basin_mask(info, basin.basin_ring())
    band = expected_band()[mask.row0:mask.row0 + mask.rows, mask.col0:mask.col0 + mask.cols]
    w = mask.weights
    assert stats["basin_mean_mm"] == pytest.approx((band * w).sum() / w.sum(), abs=1e-3)
    assert stats["max_pixel_mm"] == pytest.approx(band[w > 0].max(), abs=1e-3)
    assert stats["coverage"] == 1.0 and stats["quality"] == 0.5
    # Header + the 2 tiles under the basin, never the whole file
    assert route.call_count == 1 + 2
    assert all("Range" in call.request.headers for call in route.calls)


def test_radar_listing_keeps_only_acrr_geotiffs():
    times, truncated = radar.parse_listing((FIXTURES / "opera_listing.xml").read_text())
    assert times == [datetime(2026, 10, 26, 20, 15), datetime(2026, 10, 26, 20, 30), datetime(2026, 10, 26, 20, 45)]
    assert truncated is False
    assert radar.image_key(datetime(2026, 10, 26, 20, 45)) == "2026/10/26/OPERA/COMP/OPERA@20261026T2045@0@ACRR.tiff"


@respx.mock
async def test_radar_collector_stores_new_images_only(monkeypatch):
    now = datetime(2026, 10, 26, 21, 0)
    monkeypatch.setattr(radar, "_utcnow", lambda: now)
    base = radar.get_settings().radar_opera_base_url.rstrip("/")
    respx.get(f"{base}/").mock(return_value=httpx.Response(200, text=(FIXTURES / "opera_listing.xml").read_text()))
    tif = respx.get(url__regex=r".*@ACRR\.tiff$").mock(side_effect=_serve(P1))

    async def stored(start, end):
        return {datetime(2026, 10, 26, 20, 15)}

    written = []

    async def write(points, chunk_size=5000):
        written.extend(points)
        return len(points)

    import app.services.probabilistic_store as store
    monkeypatch.setattr(store, "query_radar_times", stored)
    monkeypatch.setattr(influx_client, "write_points_batch", write)

    assert await radar.collect_and_store_radar() == 2
    assert [p["timestamp"] for p in written] == [datetime(2026, 10, 26, 20, 45), datetime(2026, 10, 26, 20, 30)]
    point = written[0]
    assert point["measurement"] == "radar_rain"
    assert point["tags"] == {"source": "opera", "basin": "LIDOIRE", "product": "ACRR"}
    assert point["fields"]["coverage"] == 1.0 and point["fields"]["accumulation_minutes"] == 60
    calls = tif.call_count

    # Second run: nothing new to fetch
    written.clear()
    assert await radar.collect_and_store_radar() == 0
    assert tif.call_count == calls and written == []


async def test_radar_collector_disabled(monkeypatch):
    settings = radar.get_settings()
    monkeypatch.setattr(settings, "radar_enabled", False)
    assert await radar.collect_and_store_radar() == 0


# --- Read side ---------------------------------------------------------------

def _pt(t, mm, mx=None):
    return {"timestamp": t, "basin_mean_mm": mm, "max_pixel_mm": mx if mx is not None else mm * 2,
            "coverage": 1.0, "quality": 0.7}


def test_radar_summary_sums_non_overlapping_hours():
    last = datetime(2026, 10, 26, 20, 45)
    points = [_pt(last - timedelta(minutes=15 * k), 1.0 if k % 4 == 0 else 9.0) for k in range(0, 96)]
    out = radar.summarize_radar(points, now=last + timedelta(minutes=12))
    assert out["available"] is True and out["last_image_time"] == last and out["age_minutes"] == 12.0
    # Only the images at last, last-1h, ... count (the 15-min ones overlap)
    assert out["windows"]["1h"]["mm"] == 1.0
    assert out["windows"]["3h"]["mm"] == 3.0 and out["windows"]["3h"]["complete"] is True
    assert out["windows"]["24h"]["mm"] == 24.0 and out["windows"]["24h"]["hours_found"] == 24
    assert len(out["hourly"]) == 24 and out["hourly"][-1]["timestamp"] == last
    assert "CC BY 4.0" in out["attribution"]


def test_radar_summary_flags_missing_hours():
    last = datetime(2026, 10, 26, 20, 0)
    points = [_pt(last, 2.0), _pt(last - timedelta(hours=2), 3.0)]
    out = radar.summarize_radar(points, now=last)
    assert out["windows"]["3h"] == {
        "mm": 5.0, "max_pixel_mm": 6.0, "hours_found": 2, "hours_expected": 3, "complete": False,
        "start": last - timedelta(hours=3), "end": last,
    }


def test_radar_summary_empty():
    out = radar.summarize_radar([], now=datetime(2026, 10, 26))
    assert out["available"] is False and out["windows"] == {} and out["last_image_time"] is None


def test_radar_timeline_aggregation():
    from app.services.probabilistic_store import aggregate_radar_series

    day = datetime(2026, 10, 26)
    rows = [_pt(day + timedelta(minutes=15 * k), 1.0) for k in range(1, 4 * 24 + 1)]
    assert len(aggregate_radar_series(rows, "15m")) == 96
    hourly = aggregate_radar_series(rows, "1h")
    assert len(hourly) == 24 and hourly[0]["timestamp"] == day + timedelta(hours=1)
    six = aggregate_radar_series(rows, "6h")
    assert [p["value"] for p in six] == [6.0, 6.0, 6.0, 6.0]
    assert six[0]["timestamp"] == day + timedelta(hours=6)
    daily = aggregate_radar_series(rows, "1d")
    assert daily == [{"timestamp": day + timedelta(days=1), "value": 24.0}]
