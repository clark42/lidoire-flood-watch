"""La Meteo Agricole data collector.

Scrapes agricultural weather data from lameteoagricole.net:
- 10-day precipitation forecast (min/max mm, probability %)
- Wind speed and gusts
- 30-day water balance (cumul, surplus/deficit %)
- 12-month deficit (mm)

URL: https://www.lameteoagricole.net/previsions-meteo-agricole/Castillon-la-Bataille-33350.html

Data is scraped every 6 hours (daily forecasts, stable data).
Uses InfluxDB cache-through to avoid unnecessary scraping.
"""

import re
import httpx
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import get_settings
from app.models.schemas import MeteoAgricoleData, NowcastRainData


# Cache (6h for daily agricultural data)
_cache: Optional[MeteoAgricoleData] = None
_cache_expiry: Optional[datetime] = None
CACHE_DURATION_HOURS = 6

# Nowcast cache (5 min for volatile radar data)
_nowcast_cache: Optional[NowcastRainData] = None
_nowcast_cache_expiry: Optional[datetime] = None
NOWCAST_CACHE_MINUTES = 5
NOWCAST_URL = "https://www.lameteoagricole.net/meteo-minute-par-minute/Belves-de-Castillon-33350.html"


async def fetch_meteo_agricole() -> Optional[MeteoAgricoleData]:
    """
    Scrape La Meteo Agricole for agricultural weather data.

    Returns:
        MeteoAgricoleData or None if scraping fails
    """
    settings = get_settings()
    url = settings.meteo_agricole_url

    try:
        from bs4 import BeautifulSoup

        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                timeout=30.0,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; LidoireFloodWatch/1.0)",
                },
                follow_redirects=True,
            )
            response.raise_for_status()
            html = response.text

        soup = BeautifulSoup(html, "html.parser")
        text_content = soup.get_text(separator=" ", strip=True)

        # Parse precipitation forecasts: patterns like "7 a 14mm" or "7 à 14 mm"
        rain_matches = re.findall(r'(\d+(?:\.\d+)?)\s*[aà]\s*(\d+(?:\.\d+)?)\s*mm', text_content)
        rain_min = 0.0
        rain_max = 0.0
        if rain_matches:
            # Sum all forecast days
            for match in rain_matches:
                rain_min += float(match[0])
                rain_max += float(match[1])

        # Parse rain probability: "XX%" near "probabilité" or "pluie"
        prob_match = re.search(r'(?:probabilit[eé]|pluie)[^\d]*(\d+)\s*%', text_content, re.IGNORECASE)
        rain_prob = float(prob_match.group(1)) if prob_match else 0.0

        # Parse wind: "XX km/h" patterns
        wind_matches = re.findall(r'(\d+)\s*km/h', text_content)
        wind_avg = float(wind_matches[0]) if wind_matches else 0.0

        # Parse gusts: "rafales XX" or "rafales de XX km/h"
        gust_match = re.search(r'rafales?\s*(?:de\s+)?(\d+)', text_content, re.IGNORECASE)
        wind_gust = float(gust_match.group(1)) if gust_match else 0.0

        # Parse wind direction
        dir_match = re.search(r'vent\s+(?:de\s+)?(?:secteur\s+)?(N|NE|E|SE|S|SO|O|NO|Nord|Sud|Est|Ouest)', text_content, re.IGNORECASE)
        wind_dir = dir_match.group(1).upper() if dir_match else ""

        # Parse water balance: "cumul 30j : XXmm" or similar
        balance_match = re.search(r'cumul\s*(?:30\s*j(?:ours?)?)?[:\s]*(\d+(?:\.\d+)?)\s*mm', text_content, re.IGNORECASE)
        precip_30d = float(balance_match.group(1)) if balance_match else None

        # Parse surplus/deficit: "+XX%" or "-XX%"
        surplus_match = re.search(r'(?:surplus|d[eé]ficit|exc[eé]dent)\s*[:\s]*([+-]?\d+(?:\.\d+)?)\s*%', text_content, re.IGNORECASE)
        surplus_deficit = float(surplus_match.group(1)) if surplus_match else None

        # Parse 12-month deficit: "deficit 12 mois : XXmm"
        deficit_12m_match = re.search(r'd[eé]ficit\s*12\s*mois?\s*[:\s]*(\d+(?:\.\d+)?)\s*mm', text_content, re.IGNORECASE)
        deficit_12m = float(deficit_12m_match.group(1)) if deficit_12m_match else None

        # Estimate 3-day and 7-day forecast rain
        rain_3d = 0.0
        rain_7d = 0.0
        if rain_matches:
            for i, match in enumerate(rain_matches):
                avg = (float(match[0]) + float(match[1])) / 2
                if i < 3:
                    rain_3d += avg
                if i < 7:
                    rain_7d += avg

        return MeteoAgricoleData(
            timestamp=datetime.utcnow(),
            commune="Castillon-la-Bataille",
            rain_min_mm=round(rain_min, 1),
            rain_max_mm=round(rain_max, 1),
            rain_probability_pct=rain_prob,
            wind_avg_kmh=wind_avg,
            wind_gust_kmh=wind_gust,
            wind_direction=wind_dir,
            precip_30d_mm=precip_30d,
            surplus_deficit_30d_pct=surplus_deficit,
            deficit_12m_mm=deficit_12m,
            rain_forecast_3d_mm=round(rain_3d, 1),
            rain_forecast_7d_mm=round(rain_7d, 1),
        )

    except ImportError:
        print("Meteo Agricole: beautifulsoup4 not installed, skipping")
        return None
    except Exception as e:
        print(f"Error scraping Meteo Agricole: {e}")
        return None


async def get_meteo_agricole_cached() -> Optional[MeteoAgricoleData]:
    """Get Meteo Agricole data with InfluxDB cache-through.

    1. Check InfluxDB for recent data (< 6h)
    2. If present: reconstruct from InfluxDB
    3. Otherwise: scrape HTML, store to InfluxDB, return
    """
    global _cache, _cache_expiry

    now = datetime.utcnow()

    # Check in-memory cache first (faster than InfluxDB)
    if _cache_expiry and _cache_expiry > now and _cache:
        return _cache

    from app.services import influx_client

    # Check InfluxDB
    cached = await influx_client.query_latest_point(
        "meteo_agricole", "castillon", max_age_hours=CACHE_DURATION_HOURS
    )
    if cached and "rain_min_mm" in cached:
        print("meteo_agricole: from InfluxDB cache")
        data = MeteoAgricoleData(
            timestamp=cached.get("_time", now),
            commune="Castillon-la-Bataille",
            rain_min_mm=cached.get("rain_min_mm", 0),
            rain_max_mm=cached.get("rain_max_mm", 0),
            rain_probability_pct=cached.get("rain_probability_pct", 0),
            wind_avg_kmh=cached.get("wind_avg_kmh", 0),
            wind_gust_kmh=cached.get("wind_gust_kmh", 0),
            wind_direction=cached.get("wind_direction", ""),
            precip_30d_mm=cached.get("precip_30d_mm"),
            surplus_deficit_30d_pct=cached.get("surplus_deficit_30d_pct"),
            deficit_12m_mm=cached.get("deficit_12m_mm"),
            rain_forecast_3d_mm=cached.get("rain_forecast_3d_mm", 0),
            rain_forecast_7d_mm=cached.get("rain_forecast_7d_mm", 0),
        )
        _cache = data
        _cache_expiry = now + timedelta(hours=CACHE_DURATION_HOURS)
        return data

    # Scrape from website
    print("meteo_agricole: scraping from lameteoagricole.net")
    data = await fetch_meteo_agricole()
    if not data:
        return None

    # Store to InfluxDB
    try:
        fields = {
            "rain_min_mm": data.rain_min_mm,
            "rain_max_mm": data.rain_max_mm,
            "rain_probability_pct": data.rain_probability_pct,
            "wind_avg_kmh": data.wind_avg_kmh,
            "wind_gust_kmh": data.wind_gust_kmh,
            "rain_forecast_3d_mm": data.rain_forecast_3d_mm,
            "rain_forecast_7d_mm": data.rain_forecast_7d_mm,
        }
        if data.wind_direction:
            fields["wind_direction"] = data.wind_direction
        if data.precip_30d_mm is not None:
            fields["precip_30d_mm"] = data.precip_30d_mm
        if data.surplus_deficit_30d_pct is not None:
            fields["surplus_deficit_30d_pct"] = data.surplus_deficit_30d_pct
        if data.deficit_12m_mm is not None:
            fields["deficit_12m_mm"] = data.deficit_12m_mm

        await influx_client.write_point(
            measurement="meteo_agricole",
            tags={"location": "castillon"},
            fields=fields,
            timestamp=now,
        )
    except Exception as e:
        print(f"Error storing meteo_agricole to InfluxDB: {e}")

    # Update memory cache
    _cache = data
    _cache_expiry = now + timedelta(hours=CACHE_DURATION_HOURS)

    return data


async def collect_and_store_meteo_agricole() -> int:
    """Collect Meteo Agricole data and store in database.

    Called by scheduler every 6 hours.
    """
    data = await get_meteo_agricole_cached()
    return 1 if data else 0


async def fetch_nowcast_rain() -> Optional[NowcastRainData]:
    """Scrape La Meteo Agricole minute-by-minute rain nowcast.

    Parses the Chart.js data block (dansHeure) from the HTML page
    to extract 60-minute precipitation forecast from radar.

    Returns:
        NowcastRainData or None if scraping fails
    """
    import json

    try:
        from bs4 import BeautifulSoup

        async with httpx.AsyncClient() as client:
            response = await client.get(
                NOWCAST_URL,
                timeout=30.0,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; LidoireFloodWatch/1.0)",
                },
                follow_redirects=True,
            )
            response.raise_for_status()
            html = response.text

        # Look for Chart.js data block containing 'dansHeure'
        # The page embeds chart data in <script> tags with Chart.js config
        soup = BeautifulSoup(html, "html.parser")
        scripts = soup.find_all("script")

        labels = []
        data_values = []

        for script in scripts:
            text = script.string or ""
            if "dansHeure" not in text and "labels" not in text:
                continue

            # Try to extract labels array: labels: ["21:30", "21:31", ...]
            labels_match = re.search(
                r'labels\s*:\s*\[([^\]]+)\]', text
            )
            if labels_match:
                raw_labels = labels_match.group(1)
                labels = [
                    s.strip().strip("'\"")
                    for s in raw_labels.split(",")
                    if s.strip()
                ]

            # Try to extract data array: data: [0, 0.01, 0.02, ...]
            data_match = re.search(
                r'data\s*:\s*\[([^\]]+)\]', text
            )
            if data_match:
                raw_data = data_match.group(1)
                try:
                    data_values = [
                        float(v.strip()) for v in raw_data.split(",")
                        if v.strip()
                    ]
                except ValueError:
                    data_values = []

            if labels and data_values:
                break

        if not labels or not data_values:
            print("nowcast_rain: could not parse Chart.js data from page")
            return None

        # Align lengths
        min_len = min(len(labels), len(data_values))
        labels = labels[:min_len]
        data_values = data_values[:min_len]

        # Build minute_data
        # Chart.js data is rain intensity in mm/h at each minute.
        # Per-minute precipitation = intensity_mm_h / 60.
        minute_data = [
            {"minute": labels[i], "mm": round(data_values[i] / 60, 4)}
            for i in range(min_len)
        ]

        # Total = integral of intensity over time: sum(mm_h) / 60
        total_60min_mm = round(sum(data_values) / 60, 2)
        max_intensity_mm = round(max(data_values) / 60, 4) if data_values else 0.0
        rain_minutes = sum(1 for v in data_values if v > 0)

        return NowcastRainData(
            timestamp=datetime.utcnow(),
            commune="Belves-de-Castillon",
            total_60min_mm=total_60min_mm,
            max_intensity_mm=max_intensity_mm,
            rain_minutes=rain_minutes,
            minute_data=minute_data,
        )

    except ImportError:
        print("nowcast_rain: beautifulsoup4 not installed, skipping")
        return None
    except Exception as e:
        print(f"Error scraping nowcast rain: {e}")
        return None


async def get_nowcast_rain_cached() -> Optional[NowcastRainData]:
    """Get nowcast rain data with InfluxDB cache-through (5 min).

    1. Check in-memory cache
    2. Check InfluxDB for recent data (< 5 min)
    3. Otherwise: scrape HTML, store to InfluxDB, return
    """
    global _nowcast_cache, _nowcast_cache_expiry

    now = datetime.utcnow()

    # In-memory cache
    if _nowcast_cache_expiry and _nowcast_cache_expiry > now and _nowcast_cache:
        return _nowcast_cache

    from app.services import influx_client

    # Check InfluxDB
    max_age_h = NOWCAST_CACHE_MINUTES / 60.0
    cached = await influx_client.query_latest_point(
        "nowcast_rain", "castillon", max_age_hours=max(1, int(max_age_h + 1))
    )
    if cached and "total_60min_mm" in cached:
        cache_time = cached.get("_time")
        if cache_time:
            # Ensure cache_time is naive for comparison
            # (convert to UTC first: stripping tzinfo alone is only right for UTC)
            ct = (
                cache_time.astimezone(timezone.utc).replace(tzinfo=None)
                if getattr(cache_time, "tzinfo", None) else cache_time
            )
            age_minutes = (now - ct).total_seconds() / 60
            if age_minutes < NOWCAST_CACHE_MINUTES:
                print("nowcast_rain: from InfluxDB cache")
                data = NowcastRainData(
                    timestamp=cache_time,
                    commune="Belves-de-Castillon",
                    total_60min_mm=cached.get("total_60min_mm", 0),
                    max_intensity_mm=cached.get("max_intensity_mm", 0),
                    rain_minutes=int(cached.get("rain_minutes", 0)),
                    minute_data=[],  # Raw minute data not stored in InfluxDB
                )
                _nowcast_cache = data
                _nowcast_cache_expiry = now + timedelta(minutes=NOWCAST_CACHE_MINUTES)
                return data

    # Scrape from website
    print("nowcast_rain: scraping from lameteoagricole.net")
    data = await fetch_nowcast_rain()
    if not data:
        return None

    # Store to InfluxDB
    try:
        await influx_client.write_point(
            measurement="nowcast_rain",
            tags={"location": "castillon"},
            fields={
                "total_60min_mm": data.total_60min_mm,
                "max_intensity_mm": data.max_intensity_mm,
                "rain_minutes": float(data.rain_minutes),
            },
            timestamp=now,
        )
    except Exception as e:
        print(f"Error storing nowcast_rain to InfluxDB: {e}")

    # Update memory cache
    _nowcast_cache = data
    _nowcast_cache_expiry = now + timedelta(minutes=NOWCAST_CACHE_MINUTES)

    return data


async def collect_and_store_nowcast_rain() -> int:
    """Collect nowcast rain data and store.

    Called by scheduler every 5 minutes.
    """
    data = await get_nowcast_rain_cached()
    return 1 if data else 0
