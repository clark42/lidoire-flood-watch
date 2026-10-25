"""Tide data collector.

Fetches tide data from:
- SHOM API (requires API key) -> source="shom"
- Open-Meteo Marine API (free, model sea level) -> source="open-meteo-marine"
- Computed placeholder tides (fallback) -> source="estimated"

Honesty rules:
- Estimated tides are NEVER written to InfluxDB.
- When only estimated tides are available, ``TideForecast.available`` is False
  and ``TideForecast.source`` is "estimated" so the risk model can ignore them.
- Open-Meteo Marine only provides a sea level series: high/low tides are
  extracted from it and the coefficient is *estimated* from the tidal range
  (see ``_estimate_coefficient``).
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app.config import get_settings
from app.models.schemas import TideData, TideForecast, TideType

logger = logging.getLogger(__name__)

SOURCE_SHOM = "shom"
SOURCE_OPEN_METEO = "open-meteo-marine"
SOURCE_ESTIMATED = "estimated"

OPEN_METEO_MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
# Gironde estuary mouth (must be an oceanic point for sea_level_height_msl)
MARINE_LAT = 45.6
MARINE_LON = -1.2

# Approximate tidal range (m) at the Gironde mouth for a coefficient of 100.
# Tidal coefficients scale linearly with the range (coef = 100 * range / R100).
# Rough order of magnitude for Le Verdon / Pointe de Grave; only used to turn
# the Open-Meteo model range into an indicative coefficient.
RANGE_AT_COEF_100_M = 5.0

REAL_CACHE_DURATION = timedelta(hours=6)
# Retry real sources quickly when we only have estimates
ESTIMATED_CACHE_DURATION = timedelta(minutes=15)

# Cache for tide data
_tide_cache: dict = {}
_cache_expiry: Optional[datetime] = None


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.utcnow()


def _to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _estimate_coefficient(range_m: float) -> int:
    """Indicative tide coefficient from a tidal range in metres (clamped 20-120)."""
    coef = int(round(100.0 * range_m / RANGE_AT_COEF_100_M))
    return max(20, min(120, coef))


async def fetch_open_meteo_marine() -> list[TideData]:
    """
    Fetch tide data from Open-Meteo Marine API.

    This API provides sea level data which can be used to infer tides.
    Free, no API key required.
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(
                OPEN_METEO_MARINE_URL,
                params={
                    "latitude": MARINE_LAT,
                    "longitude": MARINE_LON,
                    "hourly": "sea_level_height_msl",
                    "past_days": 2,
                    "forecast_days": 7,
                    "timezone": "UTC",
                },
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()

            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            levels = hourly.get("sea_level_height_msl", [])

            if not times or not levels:
                return []

            return _extract_tides_from_levels(times, levels)

    except Exception as e:
        logger.warning("Error fetching tide data from Open-Meteo Marine: %s", e)
        return []


def _extract_tides_from_levels(times: list[str], levels: list[Optional[float]]) -> list[TideData]:
    """
    Extract high and low tides from an hourly sea level time series (UTC).

    Finds local maxima (high tides) and minima (low tides). The coefficient of
    each high tide is estimated from its range to the adjacent low tides.
    """
    if len(times) < 3 or len(levels) < 3:
        return []

    # (index, type) of each extremum
    extrema: list[tuple[int, TideType]] = []
    for i in range(1, min(len(levels), len(times)) - 1):
        if levels[i] is None:
            continue

        prev_level = levels[i - 1] if levels[i - 1] is not None else levels[i]
        next_level = levels[i + 1] if levels[i + 1] is not None else levels[i]

        if levels[i] > prev_level and levels[i] >= next_level:
            extrema.append((i, TideType.HIGH))
        elif levels[i] < prev_level and levels[i] <= next_level:
            extrema.append((i, TideType.LOW))

    tides: list[TideData] = []
    for k, (i, tide_type) in enumerate(extrema):
        height = float(levels[i])
        coef = None
        if tide_type == TideType.HIGH:
            ranges = []
            for j in (k - 1, k + 1):
                if 0 <= j < len(extrema) and extrema[j][1] == TideType.LOW:
                    ranges.append(height - float(levels[extrema[j][0]]))
            if ranges:
                coef = _estimate_coefficient(sum(ranges) / len(ranges))

        tides.append(
            TideData(
                timestamp=_to_naive_utc(datetime.fromisoformat(times[i])),
                port="BORDEAUX",
                tide_type=tide_type,
                height_m=height,
                coefficient=coef,
                source=SOURCE_OPEN_METEO,
            )
        )

    return tides


async def fetch_shom_api() -> list[TideData]:
    """
    Fetch tide data from SHOM API.

    Requires SHOM API key for full access.
    """
    settings = get_settings()

    if not settings.shom_api_key:
        return []

    url = "https://services.data.shom.fr/predictions/tides"

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(
                url,
                params={
                    "port": "BORDEAUX",
                    "duration": 7,  # days
                },
                headers={
                    "Authorization": f"Bearer {settings.shom_api_key}",
                },
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()

            tides = []
            for item in data.get("predictions", []):
                tides.append(
                    TideData(
                        timestamp=_to_naive_utc(datetime.fromisoformat(item["datetime"])),
                        port="BORDEAUX",
                        tide_type=TideType.HIGH if item["type"] == "PM" else TideType.LOW,
                        height_m=float(item["height"]),
                        coefficient=item.get("coefficient"),
                        source=SOURCE_SHOM,
                    )
                )
            return tides

    except Exception as e:
        logger.warning("Error fetching tide data from SHOM: %s", e)
        return []


def _generate_estimated_tides(now: Optional[datetime] = None) -> list[TideData]:
    """Computed placeholder tides (semi-diurnal, fixed phase) — NOT real data.

    Only used so the UI has a shape to draw; flagged source="estimated" and
    never stored.
    """
    now = now or _utcnow()
    tides = []

    current = now.replace(hour=6, minute=30, second=0, microsecond=0)
    if current < now:
        current += timedelta(hours=12, minutes=25)

    coef_base = 75

    for i in range(28):  # 7 days * 4 tides
        is_high = i % 2 == 0
        coef = coef_base + (i % 7) * 5 - 15

        tides.append(
            TideData(
                timestamp=current,
                port="BORDEAUX",
                tide_type=TideType.HIGH if is_high else TideType.LOW,
                height_m=5.2 + (0.5 if is_high else -0.5) + (coef - 75) * 0.02,
                coefficient=coef if is_high else None,
                source=SOURCE_ESTIMATED,
            )
        )

        current += timedelta(hours=6, minutes=12)

    return tides


# Backward-compatible alias
_generate_mock_tides = _generate_estimated_tides


async def _fetch_real_tides() -> list[TideData]:
    """Try real sources in priority order. Returns [] if none answered."""
    tides = await fetch_shom_api()
    if not tides:
        tides = await fetch_open_meteo_marine()
    return tides


async def get_current_tide_data() -> TideForecast:
    """Get current tide information with forecast.

    ``available`` is False (and ``source`` == "estimated") when no real source
    answered; the tides are then computed placeholders.
    """
    global _tide_cache, _cache_expiry

    now = _utcnow()

    if _cache_expiry and _cache_expiry > now and _tide_cache.get("tides"):
        tides = _tide_cache["tides"]
        source = _tide_cache["source"]
    else:
        tides = await _fetch_real_tides()
        if tides:
            source = tides[0].source or SOURCE_OPEN_METEO
            await store_tides_to_influx(tides)
            _cache_expiry = now + REAL_CACHE_DURATION
        else:
            logger.warning("No tide source available: using estimated tides (not stored)")
            tides = _generate_estimated_tides(now)
            source = SOURCE_ESTIMATED
            _cache_expiry = now + ESTIMATED_CACHE_DURATION
        _tide_cache = {"tides": tides, "source": source}

    available = source != SOURCE_ESTIMATED

    future_tides = [t for t in tides if t.timestamp > now]
    next_high = next((t for t in future_tides if t.tide_type == TideType.HIGH), None)
    next_low = next((t for t in future_tides if t.tide_type == TideType.LOW), None)

    # Current coefficient (from most recent high tide)
    past_high_tides = [t for t in tides if t.tide_type == TideType.HIGH and t.timestamp <= now]
    current_coef = past_high_tides[-1].coefficient if past_high_tides else None

    return TideForecast(
        port="BORDEAUX",
        next_high_tide=next_high,
        next_low_tide=next_low,
        current_coefficient=current_coef or (next_high.coefficient if next_high else None),
        tides=future_tides[:8],  # Next 8 tides (2 days)
        available=available,
        source=source,
    )


async def store_tides_to_influx(tides: list[TideData]) -> int:
    """Store tide data to InfluxDB. Estimated tides are always skipped."""
    from app.services import influx_client

    stored = 0
    for tide in tides:
        if tide.source == SOURCE_ESTIMATED:
            continue
        success = await influx_client.write_tide(
            port=tide.port,
            height_m=tide.height_m,
            timestamp=tide.timestamp,
            tide_type=tide.tide_type.value if tide.tide_type else None,
            coefficient=tide.coefficient,
        )
        if success:
            stored += 1

    return stored


async def collect_and_store_tides():
    """Collect real tide data and store it in InfluxDB (nothing stored if unavailable)."""
    tides = await _fetch_real_tides()

    if not tides:
        logger.warning("No tide source available: nothing stored")
        return 0

    stored = await store_tides_to_influx(tides)
    logger.info("Stored %d tide data points to InfluxDB", stored)
    return stored


async def fetch_and_store_tides_range(start: datetime, end: datetime) -> int:
    """Fetch hourly tide heights for a specific date range and store in InfluxDB.

    Uses Open-Meteo Marine API with start_date/end_date parameters to get
    historical and forecast sea level data. Stores all hourly points for
    continuous timeline display.
    """
    from app.services import influx_client

    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(
                OPEN_METEO_MARINE_URL,
                params={
                    "latitude": MARINE_LAT,
                    "longitude": MARINE_LON,
                    "hourly": "sea_level_height_msl",
                    "start_date": start_str,
                    "end_date": end_str,
                    "timezone": "UTC",
                },
                timeout=30.0,
            )
            response.raise_for_status()
            data = response.json()

            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            levels = hourly.get("sea_level_height_msl", [])

            if not times or not levels:
                logger.info("No tide data from Open-Meteo Marine for %s -> %s", start_str, end_str)
                return 0

            # Store all hourly height points for continuous timeline
            stored = 0
            for t, h in zip(times, levels):
                if h is not None:
                    success = await influx_client.write_tide(
                        port="BORDEAUX",
                        height_m=h,
                        timestamp=datetime.fromisoformat(t),
                    )
                    if success:
                        stored += 1

            # Also extract and store PM/BM events with (estimated) coefficients
            await store_tides_to_influx(_extract_tides_from_levels(times, levels))

            logger.info("Stored %d hourly tide points for %s -> %s", stored, start_str, end_str)
            return stored

    except Exception as e:
        logger.warning("Error fetching tide range from Open-Meteo Marine: %s", e)
        return 0
