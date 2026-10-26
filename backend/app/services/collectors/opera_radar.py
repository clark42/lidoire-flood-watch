"""OPERA radar rain over the Lidoire basin (EUMETNET, CC BY 4.0).

Product: OPERA NIMBUS 1-hour accumulation composite (``ACRR``), one file
every 15 min, 2 km, Lambert azimuthal equal-area (LAEA 55N 10E), as a
cloud-optimised GeoTIFF published without key on the MeteoGate object store
(rolling 24 h):

    https://s3.waw3-1.cloudferro.com/openradar-24h/{YYYY}/{MM}/{DD}/OPERA/COMP/
        OPERA@{YYYYMMDDTHHMM}@0@ACRR.tiff

The file time is the END of the 1-hour accumulation (GDAL metadata
starttime/endtime). Band 1 = ACRR (mm), band 2 = quality index (0-1).
NaN = "undetect" (radar sees no echo: 0 mm), -9999000 = no coverage.

No raster library: the file is a classic tiled TIFF (512 px tiles, deflate,
float32). We read the header with one HTTP range request, then only the tiles
under the basin (one 512 x 512 tile, ~100-200 KB, instead of the 4 MB file),
decode them with zlib + numpy, and average the pixels weighted by the share
of each pixel inside the basin polygon (8 x 8 sub-samples per pixel). The
basin polygon is projected with an ellipsoidal LAEA forward formula (Snyder
1987), parameters read from the file's GeoKeys.

Stored as measurement ``radar_rain`` (tags ``source=opera``, ``basin``,
``product=ACRR``), one point per image, at the image time. Never read by the
risk model (v3.4 is unchanged).
"""

from __future__ import annotations

import logging
import math
import re
import struct
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

import httpx
import numpy as np

from app.config import get_settings
from app.services.basin import BASIN_ID, basin_ring, points_in_polygon
from app.services.cache import EXTERNAL_TIMEOUT_S, CircuitBreaker, describe_error, is_upstream_failure

logger = logging.getLogger(__name__)

SOURCE = "opera"
PRODUCT = "ACRR"
MEASUREMENT = "radar_rain"
ATTRIBUTION = "Radar : EUMETNET OPERA (composite NIMBUS ACRR), CC BY 4.0"
LICENCE_URL = "https://creativecommons.org/licenses/by/4.0/"

# Images are published every 15 min, about 10-20 min after their time.
IMAGE_STEP = timedelta(minutes=15)
HEADER_BYTES = 64 * 1024
MAX_HEADER_BYTES = 1024 * 1024
# Values at or below this are the no-coverage fill (-9999000)
NODATA_FLOOR = -1.0e5
SUBSAMPLES = 8

_KEY_RE = re.compile(r"<Key>([^<]*@(\d{8}T\d{4})@0@ACRR\.tiff)</Key>")
_TRUNCATED_RE = re.compile(r"<IsTruncated>\s*true\s*</IsTruncated>", re.IGNORECASE)

_breaker = CircuitBreaker("OPERA radar")


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def image_key(t: datetime) -> str:
    return f"{t:%Y/%m/%d}/OPERA/COMP/OPERA@{t:%Y%m%dT%H%M}@0@{PRODUCT}.tiff"


def image_url(t: datetime, base_url: Optional[str] = None) -> str:
    base = (base_url or get_settings().radar_opera_base_url).rstrip("/")
    return f"{base}/{image_key(t)}"


# ---------------------------------------------------------------------------
# Minimal GeoTIFF reader (classic TIFF, tiled, contiguous samples)
# ---------------------------------------------------------------------------

_TYPE_FORMATS = {
    1: ("B", 1), 2: ("s", 1), 3: ("H", 2), 4: ("I", 4), 6: ("b", 1), 7: ("B", 1),
    8: ("h", 2), 9: ("i", 4), 11: ("f", 4), 12: ("d", 8), 16: ("Q", 8), 17: ("q", 8),
}


class NeedMoreBytes(Exception):
    """The header buffer ends before a tag value: fetch at least ``needed`` bytes."""

    def __init__(self, needed: int):
        super().__init__(f"TIFF header needs {needed} bytes")
        self.needed = needed


class UnsupportedTiff(ValueError):
    pass


@dataclass(frozen=True)
class LaeaProjection:
    """Ellipsoidal Lambert azimuthal equal-area (oblique aspect), forward only."""

    lat0: float
    lon0: float
    false_easting: float
    false_northing: float
    a: float = 6378137.0
    inv_f: float = 298.257223563

    def forward(self, lon, lat):
        """(lon, lat) degrees -> (x, y) metres (Snyder 1987, eq. 24-11 to 24-17)."""
        lon = np.asarray(lon, dtype=float)
        lat = np.asarray(lat, dtype=float)
        f = 1.0 / self.inv_f
        e2 = 2 * f - f * f
        e = math.sqrt(e2)

        def q(phi):
            s = np.sin(phi)
            return (1 - e2) * (s / (1 - e2 * s * s) - (1 / (2 * e)) * np.log((1 - e * s) / (1 + e * s)))

        phi0 = math.radians(self.lat0)
        qp = float(q(math.pi / 2))
        rq = self.a * math.sqrt(qp / 2)
        beta1 = math.asin(float(q(phi0)) / qp)
        d = self.a * (math.cos(phi0) / math.sqrt(1 - e2 * math.sin(phi0) ** 2)) / (rq * math.cos(beta1))
        beta = np.arcsin(np.clip(q(np.radians(lat)) / qp, -1.0, 1.0))
        dlon = np.radians(lon - self.lon0)
        b = rq * np.sqrt(2 / (1 + math.sin(beta1) * np.sin(beta) + math.cos(beta1) * np.cos(beta) * np.cos(dlon)))
        x = b * d * np.cos(beta) * np.sin(dlon)
        y = (b / d) * (math.cos(beta1) * np.sin(beta) - math.sin(beta1) * np.cos(beta) * np.cos(dlon))
        return x + self.false_easting, y + self.false_northing


@dataclass
class GeoTiffInfo:
    byteorder: str
    width: int
    height: int
    tile_width: int
    tile_height: int
    samples: int
    bits: int
    sample_format: int
    compression: int
    predictor: int
    tile_offsets: tuple
    tile_byte_counts: tuple
    pixel_scale: tuple[float, float]
    origin: tuple[float, float]  # projected coordinates of the top-left corner of pixel (0, 0)
    projection: LaeaProjection
    nodata: Optional[float] = None
    tiles_across: int = field(init=False)

    def __post_init__(self):
        self.tiles_across = -(-self.width // self.tile_width)

    @property
    def dtype(self) -> np.dtype:
        kinds = {1: "u", 2: "i", 3: "f"}
        if self.sample_format not in kinds:
            raise UnsupportedTiff(f"sample format {self.sample_format}")
        return np.dtype(f"{self.byteorder}{kinds[self.sample_format]}{self.bits // 8}")

    @property
    def geometry_key(self) -> tuple:
        return (self.width, self.height, self.pixel_scale, self.origin, self.projection)

    def xy_to_colrow(self, x, y):
        """Projected metres -> fractional (col, row) pixel coordinates."""
        sx, sy = self.pixel_scale
        x0, y0 = self.origin
        return (np.asarray(x) - x0) / sx, (y0 - np.asarray(y)) / sy


def _tag_values(buf: bytes, bo: str, typ: int, count: int, value_field: int, entry_pos: int):
    fmt, size = _TYPE_FORMATS.get(typ, (None, None))
    if fmt is None:
        return None  # rationals, undefined...: not needed
    nbytes = size * count
    if nbytes <= 4:
        pos = entry_pos + 8
    else:
        pos = value_field
        if pos + nbytes > len(buf):
            raise NeedMoreBytes(pos + nbytes)
    raw = buf[pos:pos + nbytes]
    if fmt == "s":
        return raw.split(b"\x00", 1)[0].decode("latin-1")
    return struct.unpack(f"{bo}{count}{fmt}", raw)


def parse_geotiff_header(buf: bytes) -> GeoTiffInfo:
    """Parse the first IFD (full resolution) of a tiled GeoTIFF.

    Raises ``NeedMoreBytes`` when ``buf`` is too short and ``UnsupportedTiff``
    for layouts this reader does not handle (BigTIFF, strips, planar
    separate, projection other than LAEA).
    """
    if len(buf) < 8:
        raise NeedMoreBytes(8)
    if buf[:2] == b"II":
        bo = "<"
    elif buf[:2] == b"MM":
        bo = ">"
    else:
        raise UnsupportedTiff("not a TIFF file")
    magic, ifd = struct.unpack(f"{bo}HI", buf[2:8])
    if magic != 42:
        raise UnsupportedTiff("BigTIFF or unknown magic")
    if ifd + 2 > len(buf):
        raise NeedMoreBytes(ifd + 2)
    (n,) = struct.unpack(f"{bo}H", buf[ifd:ifd + 2])
    end = ifd + 2 + 12 * n
    if end > len(buf):
        raise NeedMoreBytes(end + 4)
    tags: dict[int, object] = {}
    for i in range(n):
        pos = ifd + 2 + 12 * i
        tag, typ, count, value_field = struct.unpack(f"{bo}HHII", buf[pos:pos + 12])
        tags[tag] = _tag_values(buf, bo, typ, count, value_field, pos)

    def one(tag, default=None):
        v = tags.get(tag)
        if v is None:
            return default
        return v[0] if isinstance(v, tuple) else v

    if 322 not in tags or 324 not in tags:
        raise UnsupportedTiff("not a tiled TIFF")
    if one(284, 1) != 1:
        raise UnsupportedTiff("planar configuration must be contiguous")
    bits = tags.get(258, (8,))
    formats = tags.get(339, (1,))
    if len(set(bits)) != 1 or len(set(formats)) != 1:
        raise UnsupportedTiff("mixed sample types")

    scale = tags.get(33550)
    tie = tags.get(33922)
    if not scale or not tie:
        raise UnsupportedTiff("missing georeferencing")
    sx, sy = float(scale[0]), float(scale[1])
    origin = (float(tie[3]) - float(tie[0]) * sx, float(tie[4]) + float(tie[1]) * sy)

    nodata = None
    raw_nodata = tags.get(42113)
    if isinstance(raw_nodata, str) and raw_nodata.strip():
        try:
            nodata = float(raw_nodata.strip())
        except ValueError:
            nodata = None

    return GeoTiffInfo(
        byteorder=bo,
        width=int(one(256)),
        height=int(one(257)),
        tile_width=int(one(322)),
        tile_height=int(one(323)),
        samples=int(one(277, 1)),
        bits=int(bits[0]),
        sample_format=int(formats[0]),
        compression=int(one(259, 1)),
        predictor=int(one(317, 1)),
        tile_offsets=tuple(int(v) for v in tags[324]),
        tile_byte_counts=tuple(int(v) for v in tags[325]),
        pixel_scale=(sx, sy),
        origin=origin,
        projection=_projection_from_geokeys(tags.get(34735), tags.get(34736)),
        nodata=nodata,
    )


def _projection_from_geokeys(directory, doubles) -> LaeaProjection:
    if not directory or len(directory) < 4:
        raise UnsupportedTiff("missing GeoKeyDirectory")
    doubles = doubles or ()
    keys: dict[int, float] = {}
    for i in range(directory[3]):
        key_id, location, count, value = directory[4 + 4 * i:8 + 4 * i]
        if location == 0:
            keys[key_id] = value
        elif location == 34736 and value < len(doubles):
            keys[key_id] = doubles[value]
    if keys.get(3075) != 10:  # ProjCoordTransGeoKey: CT_LambertAzimEqualArea
        raise UnsupportedTiff(f"projection {keys.get(3075)!r} is not LAEA")
    try:
        return LaeaProjection(
            lat0=float(keys.get(3089, keys.get(3085))),  # ProjCenterLat (or NatOriginLat)
            lon0=float(keys.get(3088, keys.get(3080))),  # ProjCenterLong (or NatOriginLong)
            false_easting=float(keys.get(3082, 0.0)),
            false_northing=float(keys.get(3083, 0.0)),
            a=float(keys.get(2057, 6378137.0)),
            inv_f=float(keys.get(2059, 298.257223563)),
        )
    except TypeError as e:
        raise UnsupportedTiff("incomplete LAEA parameters") from e


def decode_tile(info: GeoTiffInfo, data: bytes) -> np.ndarray:
    """Decode one tile to an array (tile_height, tile_width, samples), native floats."""
    if info.compression in (8, 32946):
        raw = zlib.decompress(data)
    elif info.compression == 1:
        raw = data
    else:
        raise UnsupportedTiff(f"compression {info.compression}")
    th, tw, spp = info.tile_height, info.tile_width, info.samples
    itemsize = info.bits // 8
    expected = th * tw * spp * itemsize
    if len(raw) < expected:
        raise ValueError(f"tile too short: {len(raw)} < {expected} bytes")
    raw = raw[:expected]

    if info.predictor == 3:
        # Floating-point predictor (libtiff fpAcc): bytes differenced along the
        # row with a stride of ``samples`` bytes, then each value's bytes are
        # stored in planes, most significant first.
        rows = np.frombuffer(raw, dtype=np.uint8).reshape(th, tw * itemsize, spp)
        rows = np.cumsum(rows, axis=1, dtype=np.uint8).reshape(th, tw * spp * itemsize)
        planes = rows.reshape(th, itemsize, tw * spp).transpose(0, 2, 1)
        values = np.ascontiguousarray(planes).view(f">f{itemsize}").reshape(th, tw, spp)
        return values.astype(np.float64)

    values = np.frombuffer(raw, dtype=info.dtype).reshape(th, tw, spp)
    if info.predictor == 2:
        if info.sample_format == 3:
            raise UnsupportedTiff("horizontal predictor on floats")
        values = np.cumsum(values, axis=1, dtype=values.dtype)
    elif info.predictor != 1:
        raise UnsupportedTiff(f"predictor {info.predictor}")
    return values.astype(np.float64)


# ---------------------------------------------------------------------------
# Basin mask and statistics (pure)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BasinMask:
    row0: int
    col0: int
    weights: np.ndarray  # (rows, cols): share of each pixel inside the basin, 0-1

    @property
    def rows(self) -> int:
        return self.weights.shape[0]

    @property
    def cols(self) -> int:
        return self.weights.shape[1]


def build_basin_mask(
    info: GeoTiffInfo,
    ring_lonlat: Sequence[tuple[float, float]],
    subsamples: int = SUBSAMPLES,
) -> BasinMask:
    """Pixel weights of the basin: fraction of ``subsamples``^2 points inside."""
    lons = np.array([p[0] for p in ring_lonlat])
    lats = np.array([p[1] for p in ring_lonlat])
    xs, ys = info.projection.forward(lons, lats)
    ring_xy = list(zip(xs.tolist(), ys.tolist()))
    cols, rows = info.xy_to_colrow(xs, ys)
    col0 = max(0, int(math.floor(cols.min())))
    col1 = min(info.width - 1, int(math.floor(cols.max())))
    row0 = max(0, int(math.floor(rows.min())))
    row1 = min(info.height - 1, int(math.floor(rows.max())))
    if col1 < col0 or row1 < row0:
        raise ValueError("basin is outside the raster")

    sx, sy = info.pixel_scale
    x_orig, y_orig = info.origin
    offsets = (np.arange(subsamples) + 0.5) / subsamples
    cc = np.arange(col0, col1 + 1)[:, None] + offsets[None, :]   # (ncols, s)
    rr = np.arange(row0, row1 + 1)[:, None] + offsets[None, :]   # (nrows, s)
    px = x_orig + cc.reshape(-1) * sx
    py = y_orig - rr.reshape(-1) * sy
    gx, gy = np.meshgrid(px, py)  # (nrows*s, ncols*s)
    inside = points_in_polygon(gx, gy, ring_xy)
    nr, nc = row1 - row0 + 1, col1 - col0 + 1
    weights = inside.reshape(nr, subsamples, nc, subsamples).mean(axis=(1, 3))
    return BasinMask(row0=row0, col0=col0, weights=weights)


def basin_stats(
    rain: np.ndarray,
    weights: np.ndarray,
    quality: Optional[np.ndarray] = None,
    nodata: Optional[float] = None,
) -> dict:
    """Weighted basin mean, max pixel and coverage of one accumulation window.

    NaN = undetect (0 mm); ``nodata`` (or anything <= NODATA_FLOOR) = no radar
    coverage, left out of the mean and counted in ``coverage``.
    """
    rain = np.asarray(rain, dtype=float)
    weights = np.asarray(weights, dtype=float)
    missing = rain <= NODATA_FLOOR
    if nodata is not None:
        missing |= np.isclose(rain, nodata)
    values = np.where(np.isnan(rain), 0.0, rain)
    in_basin = weights > 0
    total_w = float(weights.sum())
    valid = in_basin & ~missing
    valid_w = float(weights[valid].sum())
    result = {
        "pixels": int(in_basin.sum()),
        "coverage": round(valid_w / total_w, 4) if total_w > 0 else 0.0,
        "basin_mean_mm": None,
        "max_pixel_mm": None,
        "quality": None,
    }
    if valid_w <= 0:
        return result
    result["basin_mean_mm"] = round(float((values[valid] * weights[valid]).sum() / valid_w), 3)
    result["max_pixel_mm"] = round(float(values[valid].max()), 3)
    if quality is not None:
        q = np.asarray(quality, dtype=float)
        ok = valid & ~np.isnan(q) & (q > NODATA_FLOOR)
        if ok.any():
            result["quality"] = round(float((q[ok] * weights[ok]).sum() / weights[ok].sum()), 3)
    return result


def window_from_tiles(info: GeoTiffInfo, mask: BasinMask, tiles: dict[int, np.ndarray]) -> np.ndarray:
    """Assemble the (rows, cols, samples) window under ``mask`` from decoded tiles."""
    out = np.full((mask.rows, mask.cols, info.samples), np.nan)
    th, tw = info.tile_height, info.tile_width
    for r in range(mask.rows):
        row = mask.row0 + r
        for c in range(mask.cols):
            col = mask.col0 + c
            idx = (row // th) * info.tiles_across + (col // tw)
            tile = tiles[idx]
            out[r, c, :] = tile[row % th, col % tw, :]
    return out


def tiles_for_mask(info: GeoTiffInfo, mask: BasinMask) -> list[int]:
    th, tw = info.tile_height, info.tile_width
    rows = range(mask.row0 // th, (mask.row0 + mask.rows - 1) // th + 1)
    cols = range(mask.col0 // tw, (mask.col0 + mask.cols - 1) // tw + 1)
    return [r * info.tiles_across + c for r in rows for c in cols]


_mask_cache: dict[tuple, BasinMask] = {}


def mask_for(info: GeoTiffInfo) -> BasinMask:
    """Basin mask for this raster geometry (cached: every OPERA file shares it)."""
    key = info.geometry_key
    mask = _mask_cache.get(key)
    if mask is None:
        mask = build_basin_mask(info, basin_ring())
        _mask_cache[key] = mask
    return mask


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

def parse_listing(xml: str) -> tuple[list[datetime], bool]:
    """ACRR GeoTIFF times of an S3 ListObjects page, and whether it is truncated."""
    times = []
    for _key, stamp in _KEY_RE.findall(xml or ""):
        try:
            times.append(datetime.strptime(stamp, "%Y%m%dT%H%M"))
        except ValueError:
            continue
    return sorted(set(times)), bool(_TRUNCATED_RE.search(xml or ""))


async def list_available_times(client: httpx.AsyncClient, start: datetime, end: datetime) -> list[datetime]:
    """Image times in [start, end] listed by the bucket (one or two days)."""
    base = get_settings().radar_opera_base_url.rstrip("/")
    found: set[datetime] = set()
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        prefix = f"{day:%Y/%m/%d}/OPERA/COMP/"
        marker = prefix + f"OPERA@{max(start, day) - timedelta(minutes=1):%Y%m%dT%H%M}"
        for _page in range(5):
            resp = await client.get(
                f"{base}/", params={"prefix": prefix, "marker": marker, "max-keys": "1000"},
                timeout=EXTERNAL_TIMEOUT_S,
            )
            resp.raise_for_status()
            times, truncated = parse_listing(resp.text)
            found.update(t for t in times if start <= t <= end)
            keys = re.findall(r"<Key>([^<]*)</Key>", resp.text)
            if not truncated or not keys:
                break
            marker = keys[-1]
        day += timedelta(days=1)
    return sorted(found)


async def _get_range(client: httpx.AsyncClient, url: str, start: int, length: int) -> bytes:
    resp = await client.get(
        url, headers={"Range": f"bytes={start}-{start + length - 1}"}, timeout=EXTERNAL_TIMEOUT_S,
    )
    resp.raise_for_status()
    if resp.status_code == 206:
        return resp.content
    return resp.content[start:start + length]  # server ignored the range


async def read_basin_rain(client: httpx.AsyncClient, url: str) -> dict:
    """Header + basin tiles of one ACRR file -> ``basin_stats`` dict."""
    size = HEADER_BYTES
    while True:
        header = await _get_range(client, url, 0, size)
        try:
            info = parse_geotiff_header(header)
            break
        except NeedMoreBytes as e:
            if len(header) < size or e.needed > MAX_HEADER_BYTES:
                raise
            size = e.needed + 1024
    mask = mask_for(info)
    tiles = {}
    for idx in tiles_for_mask(info, mask):
        data = await _get_range(client, url, info.tile_offsets[idx], info.tile_byte_counts[idx])
        tiles[idx] = decode_tile(info, data)
    window = window_from_tiles(info, mask, tiles)
    quality = window[..., 1] if info.samples > 1 else None
    return basin_stats(window[..., 0], mask.weights, quality, info.nodata)


def make_radar_point(t: datetime, stats: dict) -> dict:
    return {
        "measurement": MEASUREMENT,
        "tags": {"source": SOURCE, "basin": BASIN_ID, "product": PRODUCT},
        "fields": {
            "basin_mean_mm": stats.get("basin_mean_mm"),
            "max_pixel_mm": stats.get("max_pixel_mm"),
            "coverage": stats.get("coverage"),
            "quality": stats.get("quality"),
            "pixels": stats.get("pixels"),
            "accumulation_minutes": 60,
        },
        "timestamp": t,
    }


# Image times already stored (seeded from InfluxDB on the first run)
_stored_times: set[datetime] = set()
_stored_seeded = False


async def collect_and_store_radar() -> int:
    """Fetch the new ACRR images of the last ``radar_backfill_hours`` and store them.

    Newest first, at most ``radar_max_images_per_run`` per run. Returns the
    number of points written. Only the scheduler calls it.
    """
    global _stored_seeded
    settings = get_settings()
    if not settings.radar_enabled:
        return 0
    if not _breaker.allow():
        return 0
    from app.services import influx_client
    from app.services.probabilistic_store import query_radar_times

    now = _utcnow()
    start = now - timedelta(hours=settings.radar_backfill_hours)
    if not _stored_seeded:
        try:
            _stored_times.update(await query_radar_times(start, now))
            _stored_seeded = True
        except Exception as e:
            logger.warning("OPERA radar: stored times unavailable (%s), fetching anyway", describe_error(e))

    points = []
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            available = await list_available_times(client, start, now)
            todo = [t for t in reversed(available) if t not in _stored_times]
            for t in todo[: settings.radar_max_images_per_run]:
                try:
                    stats = await read_basin_rain(client, image_url(t))
                except (UnsupportedTiff, NeedMoreBytes, ValueError, zlib.error) as e:
                    logger.warning("OPERA radar %s unreadable: %r", t.isoformat(), e)
                    _stored_times.add(t)  # do not retry a broken file every run
                    continue
                if stats.get("basin_mean_mm") is None:
                    logger.info("OPERA radar %s: no coverage over the basin", t.isoformat())
                    _stored_times.add(t)
                    continue
                points.append(make_radar_point(t, stats))
        _breaker.record_success()
    except Exception as e:
        if is_upstream_failure(e):
            _breaker.record_failure(e)
        else:
            logger.warning("OPERA radar collection failed: %s", describe_error(e))

    if not points:
        return 0
    written = await influx_client.write_points_batch(points)
    if written:
        _stored_times.update(p["timestamp"] for p in points)
        cutoff = now - timedelta(hours=48)
        for t in [t for t in _stored_times if t < cutoff]:
            _stored_times.discard(t)
    return written


# ---------------------------------------------------------------------------
# Read side: basin rain over the last hours (pure)
# ---------------------------------------------------------------------------

def hourly_slots(points: list[dict], last: datetime, hours: int) -> list[Optional[dict]]:
    """Points at ``last``, ``last - 1 h``, ... (``hours`` slots, None if missing).

    Each ACRR point is a 1-hour accumulation ending at its time, so the sum of
    these non-overlapping slots is the rain over the last ``hours`` hours.
    """
    by_time = {p["timestamp"]: p for p in points}
    return [by_time.get(last - timedelta(hours=k)) for k in range(hours)]


def summarize_radar(points: list[dict], now: datetime, windows: Sequence[int] = (1, 3, 6, 24)) -> dict:
    """``/api/realtime/radar`` payload from stored points (sorted or not)."""
    pts = sorted(
        (p for p in points if p.get("basin_mean_mm") is not None),
        key=lambda p: p["timestamp"],
    )
    base = {
        "available": False,
        "source": SOURCE,
        "product": PRODUCT,
        "basin": BASIN_ID,
        "attribution": ATTRIBUTION,
        "licence_url": LICENCE_URL,
        "last_image_time": None,
        "age_minutes": None,
        "last": None,
        "windows": {},
        "hourly": [],
    }
    if not pts:
        return base
    last = pts[-1]
    t_last = last["timestamp"]
    out = dict(base)
    out["available"] = True
    out["last_image_time"] = t_last
    out["age_minutes"] = round((now - t_last).total_seconds() / 60, 1)
    out["last"] = {
        "basin_mean_mm": last.get("basin_mean_mm"),
        "max_pixel_mm": last.get("max_pixel_mm"),
        "coverage": last.get("coverage"),
        "quality": last.get("quality"),
    }
    for hours in windows:
        slots = hourly_slots(pts, t_last, hours)
        found = [s for s in slots if s is not None]
        out["windows"][f"{hours}h"] = {
            "mm": round(sum(s["basin_mean_mm"] for s in found), 2) if found else None,
            "max_pixel_mm": round(max(s.get("max_pixel_mm") or 0.0 for s in found), 2) if found else None,
            "hours_found": len(found),
            "hours_expected": hours,
            "complete": len(found) == hours,
            "start": t_last - timedelta(hours=hours),
            "end": t_last,
        }
    out["hourly"] = [
        {"timestamp": s["timestamp"], "mm": s["basin_mean_mm"], "max_pixel_mm": s.get("max_pixel_mm")}
        for s in reversed(hourly_slots(pts, t_last, 24)) if s is not None
    ]
    return out
