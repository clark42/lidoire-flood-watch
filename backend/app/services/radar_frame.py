"""OPERA ACRR frame as a small PNG over the Lidoire basin (display only).

``GET /api/realtime/radar/frame.png`` serves the 1-hour accumulation of one
OPERA composite (EUMETNET, CC BY 4.0) cropped to the basin bounding box plus
``radar_frame_margin_km``, resampled (nearest neighbour, the 2 km pixels stay
visible) on a REGULAR LON/LAT grid of ``radar_frame_pixel_deg``, so that the
front can place it on its map with the bbox alone (``west, south, east,
north`` in degrees, north up). Same origin, so it passes the CSP
``img-src 'self'``.

Colours: transparent for 0 mm (radar "undetect" NaN, or below the first
step), a blue-to-purple ramp in mm per hour, translucent grey where the
composite has no coverage.

Reads the image with the in-house COG reader of
``collectors/opera_radar.py`` (header + the tiles under the window, HTTP
range requests); the PNG writer is ~30 lines of zlib (no imaging library).
Frames are immutable: rendered PNGs are cached per frame time. Never read by
the risk model.
"""

from __future__ import annotations

import logging
import math
import struct
import zlib
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

import httpx
import numpy as np

from app.config import get_settings
from app.services.basin import basin_ring
from app.services.cache import TTLCache, CircuitBreaker, is_upstream_failure
from app.services.collectors import opera_radar as radar

logger = logging.getLogger(__name__)

# (lower bound in mm over 1 h, RGBA). Below the first bound: transparent.
COLOR_RAMP: list[tuple[float, tuple[int, int, int, int]]] = [
    (0.1, (186, 228, 250, 140)),
    (0.5, (125, 196, 240, 165)),
    (1.0, (66, 146, 230, 185)),
    (2.0, (33, 102, 209, 205)),
    (4.0, (52, 64, 180, 220)),
    (8.0, (106, 50, 168, 230)),
    (16.0, (170, 35, 140, 240)),
    (32.0, (215, 25, 75, 250)),
]
NO_COVERAGE_RGBA = (128, 128, 128, 90)
ROLLING_HOURS = 24  # the MeteoGate bucket keeps 24 h of images
FRAME_CACHE_TTL_S = 26 * 3600

_frame_cache = TTLCache(ttl_seconds=FRAME_CACHE_TTL_S, max_entries=12)
_breaker = CircuitBreaker("OPERA radar frame")


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class FrameNotAvailable(Exception):
    """No image for this time (outside the rolling 24 h, not published, disabled)."""


class FrameUpstreamError(Exception):
    """The object store did not answer (timeout, 5xx, breaker open)."""


# ---------------------------------------------------------------------------
# PNG writer (RGBA, 8 bits, no filter)
# ---------------------------------------------------------------------------


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def png_bytes(rgba: np.ndarray) -> bytes:
    """Encode an (height, width, 4) uint8 array as a PNG (colour type 6)."""
    rgba = np.ascontiguousarray(rgba, dtype=np.uint8)
    if rgba.ndim != 3 or rgba.shape[2] != 4:
        raise ValueError(f"expected (h, w, 4), got {rgba.shape}")
    h, w, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[r].tobytes() for r in range(h))  # filter type 0 per row
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b""))


# ---------------------------------------------------------------------------
# Grid, resampling and colours (pure)
# ---------------------------------------------------------------------------


def frame_bbox(ring: Sequence[tuple[float, float]], margin_km: float) -> tuple[float, float, float, float]:
    """(west, south, east, north) of the ring plus ``margin_km``, rounded outwards to 1e-3 deg."""
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    lat_mid = (min(lats) + max(lats)) / 2
    dlat = margin_km / 110.574
    dlon = margin_km / (111.320 * math.cos(math.radians(lat_mid)))
    return (
        math.floor((min(lons) - dlon) * 1000) / 1000,
        math.floor((min(lats) - dlat) * 1000) / 1000,
        math.ceil((max(lons) + dlon) * 1000) / 1000,
        math.ceil((max(lats) + dlat) * 1000) / 1000,
    )


def grid_shape(bbox: tuple[float, float, float, float], px_deg: float) -> tuple[int, int]:
    """(height, width) of the output grid."""
    west, south, east, north = bbox
    return max(1, int(round((north - south) / px_deg))), max(1, int(round((east - west) / px_deg)))


def sample_indices(info: radar.GeoTiffInfo, bbox: tuple[float, float, float, float],
                   px_deg: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Raster (rows, cols) of each output pixel centre, and the inside-raster mask."""
    west, south, east, north = bbox
    h, w = grid_shape(bbox, px_deg)
    lons = west + (np.arange(w) + 0.5) * (east - west) / w
    lats = north - (np.arange(h) + 0.5) * (north - south) / h
    glon, glat = np.meshgrid(lons, lats)
    x, y = info.projection.forward(glon, glat)
    cols, rows = info.xy_to_colrow(x, y)
    cols = np.floor(cols).astype(int)
    rows = np.floor(rows).astype(int)
    inside = (cols >= 0) & (cols < info.width) & (rows >= 0) & (rows < info.height)
    return rows, cols, inside


def tiles_for_indices(info: radar.GeoTiffInfo, rows: np.ndarray, cols: np.ndarray,
                      inside: np.ndarray) -> list[int]:
    if not inside.any():
        return []
    idx = (rows[inside] // info.tile_height) * info.tiles_across + (cols[inside] // info.tile_width)
    return sorted(set(int(i) for i in np.unique(idx)))


def resample(info: radar.GeoTiffInfo, tiles: dict[int, np.ndarray], rows: np.ndarray,
             cols: np.ndarray, inside: np.ndarray) -> np.ndarray:
    """Band 1 at each output pixel; -inf where the raster has no data or no coverage."""
    out = np.full(rows.shape, -np.inf)
    for (r, c) in zip(*np.nonzero(inside)):
        row, col = rows[r, c], cols[r, c]
        idx = (row // info.tile_height) * info.tiles_across + (col // info.tile_width)
        tile = tiles.get(int(idx))
        if tile is None:
            continue
        out[r, c] = tile[row % info.tile_height, col % info.tile_width, 0]
    nodata = (out <= radar.NODATA_FLOOR)
    if info.nodata is not None:
        nodata |= np.isclose(out, info.nodata)
    out[nodata] = -np.inf
    return out


def colorize(mm: np.ndarray, ramp: Sequence[tuple[float, tuple[int, int, int, int]]] = COLOR_RAMP) -> np.ndarray:
    """mm (NaN = undetect = 0, -inf = no coverage) -> RGBA uint8."""
    rgba = np.zeros(mm.shape + (4,), dtype=np.uint8)
    values = np.where(np.isnan(mm), 0.0, mm)
    for lower, colour in ramp:
        rgba[values >= lower] = colour
    rgba[np.isneginf(mm)] = NO_COVERAGE_RGBA
    return rgba


def frame_stats(mm: np.ndarray) -> dict:
    covered = ~np.isneginf(mm)
    values = np.where(np.isnan(mm), 0.0, mm)[covered]
    return {
        "coverage": round(float(covered.mean()), 4) if mm.size else 0.0,
        "max_mm": round(float(values.max()), 2) if values.size else None,
        "rain_pixels": int((values >= COLOR_RAMP[0][0]).sum()) if values.size else 0,
    }


def legend() -> list[dict]:
    return [{"min_mm": lower, "rgba": list(colour)} for lower, colour in COLOR_RAMP]


# ---------------------------------------------------------------------------
# Frame times
# ---------------------------------------------------------------------------


def floor_step(t: datetime) -> datetime:
    t = t.replace(second=0, microsecond=0)
    return t - timedelta(minutes=t.minute % int(radar.IMAGE_STEP.total_seconds() // 60))


def parse_frame_time(value: Optional[str], now: datetime) -> Optional[datetime]:
    """None for ``latest``; else the image time (floored to 15 min, naive UTC).

    Raises ValueError for an invalid or future value, FrameNotAvailable
    outside the rolling 24 h of the object store.
    """
    if value is None or value.strip().lower() in ("", "latest"):
        return None
    from dateutil.parser import isoparse

    t = isoparse(value.strip())
    if t.tzinfo is not None:
        t = t.astimezone(timezone.utc).replace(tzinfo=None)
    t = floor_step(t)
    if t > now + timedelta(minutes=5):
        raise ValueError("t is in the future")
    if t < now - timedelta(hours=ROLLING_HOURS):
        raise FrameNotAvailable(f"{t.isoformat()} is outside the rolling {ROLLING_HOURS} h archive")
    return t


async def latest_frame_time(now: datetime) -> Optional[datetime]:
    """Newest image time: stored radar points first, the bucket listing otherwise."""
    from app.services.probabilistic_store import query_radar_points

    try:
        points = await query_radar_points(now - timedelta(hours=3), now + timedelta(minutes=1))
        if points:
            return points[-1]["timestamp"]
    except Exception as e:
        logger.warning("Radar frame: stored times unavailable (%r), listing the bucket", e)
    if not _breaker.allow():
        return None
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            times = await radar.list_available_times(client, now - timedelta(hours=2), now)
        _breaker.record_success()
    except Exception as e:
        if is_upstream_failure(e):
            _breaker.record_failure(e)
        logger.warning("Radar frame: listing failed: %r", e)
        return None
    return times[-1] if times else None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


async def _read_header(client: httpx.AsyncClient, url: str) -> radar.GeoTiffInfo:
    size = radar.HEADER_BYTES
    while True:
        header = await radar._get_range(client, url, 0, size)
        try:
            return radar.parse_geotiff_header(header)
        except radar.NeedMoreBytes as e:
            if len(header) < size or e.needed > radar.MAX_HEADER_BYTES:
                raise
            size = e.needed + 1024


def frame_geometry() -> tuple[tuple[float, float, float, float], float]:
    s = get_settings()
    return frame_bbox(basin_ring(), s.radar_frame_margin_km), s.radar_frame_pixel_deg


async def read_frame_mm(client: httpx.AsyncClient, url: str,
                        bbox: tuple[float, float, float, float], px_deg: float) -> np.ndarray:
    info = await _read_header(client, url)
    rows, cols, inside = sample_indices(info, bbox, px_deg)
    tiles = {}
    for idx in tiles_for_indices(info, rows, cols, inside):
        data = await radar._get_range(client, url, info.tile_offsets[idx], info.tile_byte_counts[idx])
        tiles[idx] = radar.decode_tile(info, data)
    return resample(info, tiles, rows, cols, inside)


async def render_frame(t: datetime) -> tuple[bytes, dict]:
    """(PNG, metadata) of the image at ``t`` (cached per frame time)."""
    cached = _frame_cache.get(t)
    if cached is not None:
        return cached
    if not get_settings().radar_enabled:
        raise FrameNotAvailable("radar disabled (RADAR_ENABLED=false)")
    if not _breaker.allow():
        raise FrameUpstreamError("OPERA object store skipped (circuit breaker open)")
    bbox, px_deg = frame_geometry()
    url = radar.image_url(t)
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            mm = await read_frame_mm(client, url, bbox, px_deg)
        _breaker.record_success()
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (403, 404):
            raise FrameNotAvailable(f"no OPERA image at {t.isoformat()}") from e
        if is_upstream_failure(e):
            _breaker.record_failure(e)
        raise FrameUpstreamError(repr(e)) from e
    except (radar.UnsupportedTiff, radar.NeedMoreBytes, ValueError, zlib.error) as e:
        raise FrameUpstreamError(f"unreadable OPERA image: {e!r}") from e
    except Exception as e:
        if is_upstream_failure(e):
            _breaker.record_failure(e)
        raise FrameUpstreamError(repr(e)) from e

    png = png_bytes(colorize(mm))
    h, w = mm.shape
    meta = frame_metadata(t, bbox, (h, w))
    meta.update(frame_stats(mm))
    meta["available"] = True
    _frame_cache.set(t, (png, meta))
    return png, meta


def frame_metadata(t: Optional[datetime], bbox: tuple[float, float, float, float],
                   shape: tuple[int, int]) -> dict:
    h, w = shape
    return {
        "available": False,
        "time": t,
        "accumulation_minutes": 60,
        "accumulation_start": t - timedelta(hours=1) if t else None,
        "product": radar.PRODUCT,
        "source": radar.SOURCE,
        "attribution": radar.ATTRIBUTION,
        "licence_url": radar.LICENCE_URL,
        "bbox": list(bbox),
        "bbox_order": "west,south,east,north (degres WGS84)",
        "crs": "EPSG:4326, grille reguliere lon/lat, nord en haut (reechantillonnage au plus proche voisin)",
        "width": w,
        "height": h,
        "pixel_deg": get_settings().radar_frame_pixel_deg,
        "url": f"/api/realtime/radar/frame.png?t={t.strftime('%Y-%m-%dT%H:%MZ')}" if t else None,
        "legend": legend(),
        "no_coverage_rgba": list(NO_COVERAGE_RGBA),
        "unit": "mm sur 1 h (cumul ACRR finissant a time)",
        "model_input": False,
    }
