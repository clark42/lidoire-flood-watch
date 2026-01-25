"""Tide data collector.

Fetches tide data from:
- maree.info (primary, free)
- SHOM API (secondary, requires API key)
"""

import httpx
from datetime import datetime, timedelta
from typing import Optional
import re

from app.config import get_settings
from app.models.schemas import TideData, TideForecast, TideType

# Cache for tide data
_tide_cache: dict = {}
_cache_expiry: Optional[datetime] = None


async def fetch_maree_info(port: str = "bordeaux") -> list[TideData]:
    """
    Fetch tide data from maree.info.

    Port codes:
    - bordeaux: Port 161
    - pauillac: Port 160
    - libourne: Port 162 (if available)
    """
    port_mapping = {
        "bordeaux": "161",
        "pauillac": "160",
        "libourne": "162",
    }

    port_id = port_mapping.get(port.lower(), "161")
    url = f"https://maree.info/{port_id}"

    tides = []

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=30.0)
            response.raise_for_status()

            # Parse HTML response to extract tide data
            # This is a simplified parser - real implementation would need proper HTML parsing
            html = response.text

            # Look for tide times and heights in the HTML
            # Format varies, this is approximate
            # Real implementation would use BeautifulSoup

            # For now, return mock data structure
            # TODO: Implement proper HTML parsing

    except Exception as e:
        print(f"Error fetching tide data from maree.info: {e}")

    return tides


async def fetch_shom_api() -> list[TideData]:
    """
    Fetch tide data from SHOM API.

    Requires SHOM API key for full access.
    """
    settings = get_settings()

    if not settings.shom_api_key:
        return []

    # SHOM API endpoint
    url = "https://services.data.shom.fr/predictions/tides"

    try:
        async with httpx.AsyncClient() as client:
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
                        timestamp=datetime.fromisoformat(item["datetime"]),
                        port="BORDEAUX",
                        tide_type=TideType.HIGH if item["type"] == "PM" else TideType.LOW,
                        height_m=float(item["height"]),
                        coefficient=item.get("coefficient"),
                    )
                )
            return tides

    except Exception as e:
        print(f"Error fetching tide data from SHOM: {e}")
        return []


def _generate_mock_tides() -> list[TideData]:
    """Generate mock tide data for development/testing."""
    now = datetime.utcnow()
    tides = []

    # Generate tides for the next 7 days
    # Approximate: 2 high tides and 2 low tides per day
    # ~12h25m between successive high tides

    current = now.replace(hour=6, minute=30, second=0, microsecond=0)
    if current < now:
        current += timedelta(hours=12, minutes=25)

    coef_base = 75  # Base coefficient

    for i in range(28):  # 7 days * 4 tides
        is_high = i % 2 == 0
        # Vary coefficient slightly
        coef = coef_base + (i % 7) * 5 - 15

        tides.append(
            TideData(
                timestamp=current,
                port="BORDEAUX",
                tide_type=TideType.HIGH if is_high else TideType.LOW,
                height_m=5.2 + (0.5 if is_high else -0.5) + (coef - 75) * 0.02,
                coefficient=coef if is_high else None,
            )
        )

        current += timedelta(hours=6, minutes=12)

    return tides


async def get_current_tide_data() -> TideForecast:
    """Get current tide information with forecast."""
    global _tide_cache, _cache_expiry

    now = datetime.utcnow()

    # Check cache
    if _cache_expiry and _cache_expiry > now and _tide_cache:
        tides = _tide_cache.get("tides", [])
    else:
        # Try to fetch real data
        tides = await fetch_shom_api()

        if not tides:
            tides = await fetch_maree_info("bordeaux")

        if not tides:
            # Fall back to mock data
            tides = _generate_mock_tides()

        # Update cache (6 hours)
        _tide_cache = {"tides": tides}
        _cache_expiry = now + timedelta(hours=6)

    # Find next high and low tides
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
    )


async def collect_and_store_tides():
    """Collect tide data and store in database."""
    # This would be called by the scheduler
    tides = await fetch_shom_api()

    if not tides:
        tides = await fetch_maree_info("bordeaux")

    if tides:
        # Store in InfluxDB
        # TODO: Implement storage
        pass

    return len(tides)
