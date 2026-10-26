"""Geographic reference data (public)."""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.services.basin import basin_geojson
from app.services.cache import TTLCache
from app.services.episode_drivers import get_profile_at

router = APIRouter()

_profile_cache = TTLCache(ttl_seconds=60, max_entries=256)


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_at(value: Optional[str], now: Optional[datetime] = None) -> datetime:
    """``at`` query parameter -> naive UTC (now when absent); 422 if invalid or future."""
    now = now or _utcnow()
    if value is None or not value.strip() or value.strip().lower() == "now":
        return now
    from dateutil.parser import isoparse

    try:
        t = isoparse(value.strip())
    except (ValueError, OverflowError):
        raise HTTPException(status_code=422, detail="at: invalid ISO 8601 datetime")
    if t.tzinfo is not None:
        t = t.astimezone(timezone.utc).replace(tzinfo=None)
    if t > now + timedelta(minutes=5):
        raise HTTPException(status_code=422, detail="at: must not be in the future")
    return t


@router.get("/basin")
async def get_basin():
    """Lidoire topographic catchment (GeoJSON FeatureCollection, WGS84).

    Sandre BD TOPAGE, union of the Lidoire and Lechout elementary catchments
    (230.8 km2), simplified to 25 m. Source and licence in ``properties``.
    """
    return basin_geojson()


@router.get("/profile")
async def get_lidoire_profile(
    at: Optional[str] = Query(None, description="ISO 8601 time (UTC if naive); default now"),
    points: bool = Query(True, description="Include the profile points (false for a replay scrubber)"),
):
    """Lidoire long profile from ~1 km above the mill to the Dordogne confluence.

    - ``profile``: ground z (m NGF, IGN RGE ALTI, low point of a +/- 30 m
      cross-section) every ~100 m along the Sandre BD TOPAGE line, ``d_m`` =
      distance from the confluence; source and licence in ``profile.metadata``;
    - ``mill``: position on the profile and ground z (config);
    - ``confluence``: stage Z_c at ``at``, ESTIMATED from the Pessac and
      Libourne levels (max over 12.5 h, points <= at), NGF zeros and alpha
      from the config; ``instant`` = same from the latest points;
    - ``backwater``: furthest point upstream with ground z <= Z_c (flat
      water APPROXIMATION) and its distance below the mill;
    - ``margin``: mill ground minus Z_c (not the floor margin: unknown).

    Reads InfluxDB only; never a model input.
    """
    now = _utcnow()
    t = parse_at(at, now)
    key = (t.replace(second=0, microsecond=0), points)
    cached = _profile_cache.get(key)
    if cached is not None:
        return cached
    payload = await get_profile_at(t, include_points=points)
    if not payload.get("errors"):
        _profile_cache.set(key, payload, ttl_seconds=3600 if now - t > timedelta(days=1) else 60)
    return payload
