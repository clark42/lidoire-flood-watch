"""Timeline data endpoints."""

from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Query

from app.models.schemas import (
    AvailableSources,
    RiverStationInfo,
    RainfallStationInfo,
    UnifiedTimelineData,
    TimelineSeries,
    TimelineDataPoint,
)
from app.services.influx_client import (
    query_rainfall_by_station,
    query_river_levels_multi_station,
    query_tides_timeline,
    query_risk_scores_timeline,
    query_soil_timeline,
    query_wind_timeline,
)


async def _ensure_river_data(start: datetime, end: datetime, station_ids: list[str]) -> None:
    """Check if InfluxDB has river data for the period; if not, fetch from Hub'Eau.

    Uses realtime observations (TR) for recent data (< 30 days old).
    Falls back to elaborated data (obs_elab, daily max HXI) for older periods.
    """
    from app.services.data_store import _fetch_and_store_river_levels, _fetch_and_store_river_levels_elab
    from app.services.influx_client import query_all_stations_levels

    s = start.replace(tzinfo=None) if start.tzinfo else start
    e = end.replace(tzinfo=None) if end.tzinfo else end
    now = datetime.utcnow()
    thirty_days_ago = now - timedelta(days=30)

    existing = await query_all_stations_levels(s, e)

    # Adapt expected density to data source
    # Hydroportail provides sub-hourly historical data, so we expect
    # similar density to TR data even for periods > 30 days.
    # Fallback to obs_elab (daily) only happens if Hydroportail fails.
    period_hours = (e - s).total_seconds() / 3600
    if s < thirty_days_ago:
        # Hydroportail = sub-hourly → expect ~1 point per 2 hours
        # (if only obs_elab available, re-fetch will try Hydroportail first)
        expected_min_points = max(4, int(period_hours / 2))
    else:
        # TR = data every 5-15min → expect ~1 point per 2 hours
        expected_min_points = max(4, int(period_hours / 2))

    need_fetch = False
    for sid in station_ids:
        station_data = existing.get(sid, [])
        if len(station_data) < expected_min_points:
            print(f"Insufficient river data for {sid}: {len(station_data)} points, need ~{expected_min_points}")
            need_fetch = True
            break

        # Also detect coverage gaps: if data starts much later than requested start
        if station_data and s < thirty_days_ago:
            first_ts = station_data[0]["timestamp"]
            if hasattr(first_ts, "tzinfo") and first_ts.tzinfo:
                first_ts = first_ts.replace(tzinfo=None)
            if first_ts > s + timedelta(days=2):
                print(f"Coverage gap for {sid}: data starts at {first_ts}, requested from {s}")
                need_fetch = True
                break

    if need_fetch:
        # Split into obs_elab (old) and TR (recent) if period spans the 30-day boundary
        if s < thirty_days_ago:
            elab_end = min(e, thirty_days_ago)
            print(f"Backfilling river data from Hub'Eau obs_elab for {s} -> {elab_end}...")
            await _fetch_and_store_river_levels_elab(s, elab_end, station_ids)

            # Also fetch recent portion via TR if period extends into last 30 days
            if e > thirty_days_ago:
                print(f"Backfilling recent river data from Hub'Eau TR for {thirty_days_ago} -> {e}...")
                await _fetch_and_store_river_levels(thirty_days_ago, e)
        else:
            print(f"Backfilling river data from Hub'Eau TR for {s} -> {e}...")
            await _fetch_and_store_river_levels(s, e)


async def _ensure_tide_data(start: datetime, end: datetime) -> None:
    """Check if InfluxDB has tide data for the period; if not, fetch from APIs."""
    from app.services.influx_client import query_tides

    s = start.replace(tzinfo=None) if start.tzinfo else start
    e = end.replace(tzinfo=None) if end.tzinfo else end

    existing = await query_tides("LIBOURNE", s, e)
    # Also check BORDEAUX port (Open-Meteo writes to BORDEAUX)
    if not existing:
        existing = await query_tides("BORDEAUX", s, e)

    # Check data density: we expect at least 1 point per 2 hours for a good timeline
    period_hours = (e - s).total_seconds() / 3600
    expected_min_points = max(4, int(period_hours / 2))
    has_enough = len(existing) >= expected_min_points if existing else False

    if not has_enough:
        print(f"Insufficient tide data in InfluxDB ({len(existing) if existing else 0} points, need ~{expected_min_points}) for {s} -> {e}, fetching from Open-Meteo Marine...")
        from app.services.collectors.tide import fetch_and_store_tides_range
        await fetch_and_store_tides_range(s, e)


async def _ensure_open_meteo_rainfall(start: datetime, end: datetime) -> None:
    """Ensure Open-Meteo historical rainfall data exists in InfluxDB."""
    from app.services.influx_client import query_rainfall_by_station

    s = start.replace(tzinfo=None) if start.tzinfo else start
    e = end.replace(tzinfo=None) if end.tzinfo else end

    existing = await query_rainfall_by_station(
        start=s, end=e,
        sources=["open-meteo"],
        cities=["Moulin-Lidoire"],
        resolution="1h",
    )

    open_meteo_data = existing.get("open-meteo:Moulin-Lidoire", [])
    period_hours = (e - s).total_seconds() / 3600
    expected_min_points = max(4, int(period_hours / 3))

    if len(open_meteo_data) < expected_min_points:
        print(f"Insufficient Open-Meteo rainfall ({len(open_meteo_data)} points, need ~{expected_min_points}) for {s} -> {e}, backfilling...")
        from app.services.collectors.weather import fetch_and_store_historical_rainfall
        await fetch_and_store_historical_rainfall(s, e)


async def _ensure_netatmo_rainfall(start: datetime, end: datetime, cities: list[str]) -> None:
    """Cache-through for Netatmo rainfall data.

    Pattern: query InfluxDB → if sparse → fetch historical via getmeasure API → store.
    Also triggers current collection if period includes recent time.
    """
    from app.services.influx_client import query_rainfall_by_station

    s = start.replace(tzinfo=None) if start.tzinfo else start
    e = end.replace(tzinfo=None) if end.tzinfo else end
    now = datetime.utcnow()

    # Check data density per requested city
    period_hours = (e - s).total_seconds() / 3600
    expected_min_points = max(4, int(period_hours / 3))

    existing = await query_rainfall_by_station(
        start=s, end=e,
        sources=["netatmo-lidoire"],
        cities=cities,
        resolution="1h",
    )

    # Check if any city needs backfill
    cities_needing_backfill = []
    for city in cities:
        key = f"netatmo-lidoire:{city}"
        city_points = existing.get(key, [])
        if len(city_points) < expected_min_points * 0.5:
            print(f"Netatmo '{city}': {len(city_points)} points in InfluxDB, "
                  f"need ~{expected_min_points}, will backfill via getmeasure")
            cities_needing_backfill.append(city)

    # Backfill historical data via getmeasure API
    if cities_needing_backfill:
        try:
            from app.services.collectors.netatmo import fetch_and_store_netatmo_historical_range
            stored = await fetch_and_store_netatmo_historical_range(s, e, cities_needing_backfill)
            print(f"Netatmo historical backfill: stored {stored} points")
        except Exception as e_hist:
            print(f"Netatmo historical backfill failed: {e_hist}")

    # Also trigger current collection if period includes recent time
    period_includes_recent = e >= now - timedelta(hours=1)
    if period_includes_recent:
        recent_start = now - timedelta(minutes=30)
        recent_data = await query_rainfall_by_station(
            start=recent_start, end=now,
            sources=["netatmo-lidoire"],
            cities=cities,
            resolution="15m",
        )
        has_recent = any(len(pts) > 0 for pts in recent_data.values())
        if not has_recent:
            try:
                from app.services.scheduler import collect_netatmo_job
                await collect_netatmo_job()
            except Exception as e_netatmo:
                print(f"Netatmo current collection failed: {e_netatmo}")

async def _ensure_risk_data(start: datetime, end: datetime) -> None:
    """Cache-through for risk scores.

    If risk score data is sparse for the period, backfill by recalculating
    from historical river, rainfall, and tide data already in InfluxDB.

    Important: call AFTER _ensure_river_data, _ensure_tide_data, and
    _ensure_*_rainfall so that input data is present.
    """
    from app.services.influx_client import query_risk_scores_timeline

    s = start.replace(tzinfo=None) if start.tzinfo else start
    e = end.replace(tzinfo=None) if end.tzinfo else end
    now = datetime.utcnow()

    # Don't backfill future periods
    if s > now:
        return

    # Cap end to now for backfill
    backfill_end = min(e, now)

    # Check existing risk data
    existing = await query_risk_scores_timeline(start=s, end=backfill_end, resolution="1h")

    period_hours = (backfill_end - s).total_seconds() / 3600
    # Adaptive expected density
    if period_hours > 30 * 24:
        expected_min_points = max(4, int(period_hours / 12))  # ~2 per day for long periods
    elif period_hours > 7 * 24:
        expected_min_points = max(4, int(period_hours / 6))   # ~4 per day
    else:
        expected_min_points = max(4, int(period_hours / 2))   # ~12 per day

    if len(existing) >= expected_min_points * 0.5:
        return  # Enough data

    print(f"Insufficient risk data ({len(existing)} points, need ~{expected_min_points}) "
          f"for {s} -> {backfill_end}, backfilling from historical inputs...")

    try:
        from app.services.risk_v2 import backfill_risk_scores
        stored = await backfill_risk_scores(s, backfill_end)
        print(f"Risk backfill completed: {stored} scores stored")
    except Exception as e_risk:
        print(f"Risk score backfill failed: {e_risk}")


async def _ensure_infoclimat_rainfall(start: datetime, end: datetime) -> None:
    """Cache-through for Infoclimat historical rainfall data.

    Uses the Infoclimat OpenData API (chunked 7-day requests).
    Fails gracefully if API key is missing or IP not authorized.
    """
    from app.services.influx_client import query_rainfall_by_station

    s = start.replace(tzinfo=None) if start.tzinfo else start
    e = end.replace(tzinfo=None) if end.tzinfo else end

    existing = await query_rainfall_by_station(
        start=s, end=e,
        sources=["infoclimat"],
        resolution="1h",
    )

    # Check if we have any infoclimat data
    total_points = sum(len(pts) for pts in existing.values())
    period_hours = (e - s).total_seconds() / 3600
    expected_min_points = max(4, int(period_hours / 3))

    if total_points >= expected_min_points * 0.5:
        return  # Enough data

    print(f"Insufficient Infoclimat data ({total_points} points, need ~{expected_min_points}) "
          f"for {s} -> {e}, backfilling...")

    try:
        from app.services.collectors.infoclimat import fetch_and_store_infoclimat_historical_range
        stored = await fetch_and_store_infoclimat_historical_range(s, e)
        print(f"Infoclimat backfill completed: {stored} points stored")
    except Exception as e_ic:
        print(f"Infoclimat backfill failed: {e_ic}")


async def _ensure_soil_data(start: datetime, end: datetime) -> None:
    """Check if InfluxDB has soil timeline data for the period; if not, backfill from Open-Meteo.

    Same cache-through pattern as _ensure_river_data.
    """
    from app.services.influx_client import query_soil_timeline

    s = start.replace(tzinfo=None) if start.tzinfo else start
    e = end.replace(tzinfo=None) if end.tzinfo else end

    # Quick density check on one field (moisture_shallow)
    existing = await query_soil_timeline(s, e, resolution="6h", fields=["moisture_shallow"])
    data_points = existing.get("moisture_shallow", [])

    period_hours = (e - s).total_seconds() / 3600
    # Expect ~1 point per 6h at 6h resolution
    expected_min_points = max(2, int(period_hours / 12))

    if len(data_points) >= expected_min_points:
        return  # Enough data

    print(f"Insufficient soil data ({len(data_points)} points, need ~{expected_min_points}) "
          f"for {s} -> {e}, backfilling from Open-Meteo...")

    try:
        from app.services.collectors.weather import fetch_and_store_soil_historical
        stored = await fetch_and_store_soil_historical(s, e)
        print(f"Soil backfill completed: {stored} points stored")
    except Exception as e_soil:
        print(f"Soil data backfill failed: {e_soil}")


async def _ensure_wind_data(start: datetime, end: datetime) -> None:
    """Check if InfluxDB has wind data for the period; if not, backfill from Open-Meteo."""
    from app.services.influx_client import query_wind_timeline

    s = start.replace(tzinfo=None) if start.tzinfo else start
    e = end.replace(tzinfo=None) if end.tzinfo else end

    existing = await query_wind_timeline(s, e, resolution="6h")
    data_points = existing.get("speed_kmh", [])

    period_hours = (e - s).total_seconds() / 3600
    expected_min_points = max(2, int(period_hours / 12))

    if len(data_points) >= expected_min_points:
        return

    print(f"Insufficient wind data ({len(data_points)} points, need ~{expected_min_points}) "
          f"for {s} -> {e}, backfilling from Open-Meteo...")

    try:
        from app.services.collectors.weather import fetch_and_store_wind_historical
        stored = await fetch_and_store_wind_historical(s, e)
        print(f"Wind backfill completed: {stored} points stored")
    except Exception as e_wind:
        print(f"Wind data backfill failed: {e_wind}")


router = APIRouter()

# Station configurations
RIVER_STATIONS = {
    "P577001001": {"name": "Libourne", "river": "Dordogne", "color": "#0891b2", "distance_km": 5, "delay_hours": 0},
    "P555001001": {"name": "Pessac-sur-Dordogne", "river": "Dordogne", "color": "#0ea5e9", "distance_km": 30, "delay_hours": 4},
    "P514001001": {"name": "Bergerac", "river": "Dordogne", "color": "#06b6d4", "distance_km": 60, "delay_hours": 8},
    "P726151001": {"name": "Abzac", "river": "Isle", "color": "#8b5cf6", "distance_km": 15, "delay_hours": 2},
    "P704151001": {"name": "Perigueux", "river": "Isle", "color": "#a855f7", "distance_km": 80, "delay_hours": 12},
    "P846251001": {"name": "Coutras", "river": "Dronne", "color": "#f59e0b", "distance_km": 25, "delay_hours": 3},
    "P816251001": {"name": "Riberac", "river": "Dronne", "color": "#fbbf24", "distance_km": 70, "delay_hours": 10},
}

RAINFALL_STATIONS = {
    "open-meteo:Moulin-Lidoire": {"name": "Open-Meteo (Moulin)", "position": "milieu", "color": "#3b82f6"},
    "netatmo-lidoire:Saint-Remy": {"name": "Saint-Remy", "position": "amont", "color": "#22c55e"},
    "netatmo-lidoire:Saint-Antoine-sur-l'Isle": {"name": "Saint-Antoine-sur-l'Isle", "position": "amont", "color": "#16a34a"},
    "netatmo-lidoire:Flaujagues": {"name": "Flaujagues", "position": "milieu", "color": "#15803d"},
    "netatmo-lidoire:Castillon-la-Bataille": {"name": "Castillon-la-Bataille", "position": "aval", "color": "#166534"},
    "netatmo-lidoire:Lamothe-Montravel": {"name": "Lamothe-Montravel", "position": "aval", "color": "#14532d"},
    "netatmo-lidoire:Les Salles-de-Castillon": {"name": "Les Salles-de-Castillon", "position": "aval", "color": "#052e16"},
}


@router.get("/sources", response_model=AvailableSources)
async def get_available_sources():
    """
    Get available data sources for timeline comparison.

    Returns information about all river stations (Hub'Eau) and
    rainfall stations (Netatmo Lidoire) that can be selected.
    """
    river_stations = [
        RiverStationInfo(
            id=station_id,
            name=info["name"],
            river=info["river"],
            color=info["color"],
            distance_km=info["distance_km"],
            delay_hours=info["delay_hours"],
        )
        for station_id, info in RIVER_STATIONS.items()
    ]

    rainfall_stations = [
        RainfallStationInfo(
            id=source_id,
            name=info["name"],
            position=info["position"],
            color=info["color"],
        )
        for source_id, info in RAINFALL_STATIONS.items()
    ]

    return AvailableSources(
        river_stations=river_stations,
        rainfall_stations=rainfall_stations,
        has_tide=True,
        has_risk_score=True,
        has_infoclimat=True,
        has_wind=True,
        has_soil=True,
    )


@router.get("/unified", response_model=UnifiedTimelineData)
async def get_unified_timeline(
    start: Optional[datetime] = Query(None, description="Start datetime (default: 48h ago)"),
    end: Optional[datetime] = Query(None, description="End datetime (default: now)"),
    river_stations: Optional[str] = Query(None, description="Comma-separated Hub'Eau station IDs"),
    rainfall_sources: Optional[str] = Query(None, description="Comma-separated rainfall sources (format: source:city)"),
    include_tide: bool = Query(False, description="Include tide data (Libourne)"),
    include_risk: bool = Query(False, description="Include risk score data"),
    include_soil: bool = Query(False, description="Include soil conditions data (moisture, temperature, ET0)"),
    include_infoclimat: bool = Query(True, description="Include Infoclimat rainfall data"),
    include_wind: bool = Query(False, description="Include wind speed/direction data"),
    include_forecast: bool = Query(False, description="Include 7-day risk forecast overlay"),
    resolution: str = Query("1h", description="Data resolution: 15m, 1h, 6h, 12h, 1d"),
):
    """
    Get unified timeline data combining river levels, rainfall, tides, and risk scores.

    This endpoint allows flexible comparison of any combination of:
    - River level stations (Hub'Eau)
    - Rainfall stations (Netatmo Lidoire)
    - Tide data (Libourne port)
    - Risk score history

    Example:
    - river_stations=P577001001,P555001001
    - rainfall_sources=netatmo-lidoire:Saint-Remy,netatmo-lidoire:Flaujagues
    - include_tide=true
    - include_risk=true
    """
    now = datetime.utcnow()

    if end is None:
        end = now
    if start is None:
        start = end - timedelta(hours=48)

    # Validate resolution
    valid_resolutions = ["15m", "1h", "6h", "12h", "1d"]
    if resolution not in valid_resolutions:
        resolution = "1h"

    series: dict[str, TimelineSeries] = {}

    # Query river levels if requested
    if river_stations:
        station_ids = [s.strip() for s in river_stations.split(",")]

        # Cache-through: backfill from Hub'Eau if InfluxDB is empty
        await _ensure_river_data(start, end, station_ids)

        river_data = await query_river_levels_multi_station(
            start=start,
            end=end,
            station_ids=station_ids,
            resolution=resolution,
        )

        for station_id, points in river_data.items():
            info = RIVER_STATIONS.get(station_id, {
                "name": station_id,
                "river": "Unknown",
                "color": "#6b7280",
            })
            series[f"river:{station_id}"] = TimelineSeries(
                type="river_level",
                name=info["name"],
                unit="m",
                color=info["color"],
                data=[
                    TimelineDataPoint(timestamp=p["timestamp"], value=p["value"])
                    for p in points
                ],
            )

    # Query rainfall if requested
    if rainfall_sources:
        source_cities = [s.strip() for s in rainfall_sources.split(",")]

        # Extract sources and cities
        sources_set = set()
        netatmo_cities = []
        cities_list = []
        for sc in source_cities:
            if ":" in sc:
                source, city = sc.split(":", 1)
                sources_set.add(source)
                cities_list.append(city)
                if source.startswith("netatmo"):
                    netatmo_cities.append(city)

        # Source-specific cache-through
        if "open-meteo" in sources_set:
            await _ensure_open_meteo_rainfall(start, end)
        if netatmo_cities:
            await _ensure_netatmo_rainfall(start, end, netatmo_cities)

        # For coarse resolutions (>=6h), use sum to get total mm per period
        # instead of mean which averages the rate and loses magnitude
        use_sum = resolution in ("6h", "12h", "1d")
        rain_agg_fn = "sum" if use_sum else "mean"
        rain_unit = "mm" if use_sum else "mm/h"

        rainfall_data = await query_rainfall_by_station(
            start=start,
            end=end,
            sources=list(sources_set) if sources_set else None,
            cities=cities_list if cities_list else None,
            resolution=resolution,
            agg_fn=rain_agg_fn,
        )

        # Check Netatmo data density and auto-add Open-Meteo fallback if sparse
        period_hours = ((end - start).total_seconds()) / 3600
        expected_min_points = max(4, int(period_hours / 3))
        open_meteo_key = "open-meteo:Moulin-Lidoire"
        need_open_meteo_fallback = False

        if netatmo_cities and open_meteo_key not in source_cities:
            for sc in source_cities:
                if sc.startswith("netatmo"):
                    netatmo_points = rainfall_data.get(sc, [])
                    if len(netatmo_points) < expected_min_points * 0.5:
                        print(f"Netatmo '{sc}' has only {len(netatmo_points)} points "
                              f"(need ~{expected_min_points}), adding Open-Meteo fallback")
                        need_open_meteo_fallback = True
                        break

        if need_open_meteo_fallback:
            # Ensure Open-Meteo data exists for the full period
            await _ensure_open_meteo_rainfall(start, end)
            # Query Open-Meteo data
            om_data = await query_rainfall_by_station(
                start=start, end=end,
                sources=["open-meteo"],
                cities=["Moulin-Lidoire"],
                resolution=resolution,
                agg_fn=rain_agg_fn,
            )
            # Merge into rainfall_data
            for key, points in om_data.items():
                if key not in rainfall_data:
                    rainfall_data[key] = points
            # Add to source_cities so it gets included in the response
            if open_meteo_key not in source_cities:
                source_cities.append(open_meteo_key)

        for source_city, points in rainfall_data.items():
            # Only include requested sources (+ auto-added fallback)
            if source_city in source_cities:
                info = RAINFALL_STATIONS.get(source_city, {
                    "name": source_city,
                    "position": "unknown",
                    "color": "#6b7280",
                })
                series[f"rainfall:{source_city}"] = TimelineSeries(
                    type="rainfall",
                    name=info["name"],
                    unit=rain_unit,
                    color=info["color"],
                    data=[
                        TimelineDataPoint(timestamp=p["timestamp"], value=p["value"])
                        for p in points
                    ],
                )

        # Cumulative rainfall series: running total using max across sources per timestamp
        # (avoids double-counting when multiple sources measure the same area)
        cumul_data = await query_rainfall_by_station(
            start=start, end=end,
            sources=list(sources_set) if sources_set else None,
            cities=cities_list if cities_list else None,
            resolution="1h",
            agg_fn="sum",
        )
        # Take max value across sources at each timestamp (not sum)
        all_hourly: dict[str, float] = {}
        for _sc, pts in cumul_data.items():
            for p in pts:
                ts_key = p["timestamp"].isoformat() if hasattr(p["timestamp"], "isoformat") else str(p["timestamp"])
                all_hourly[ts_key] = max(all_hourly.get(ts_key, 0), p["value"] or 0)

        if all_hourly:
            sorted_ts = sorted(all_hourly.keys())
            running = 0.0
            cumul_points = []
            for ts_key in sorted_ts:
                running += all_hourly[ts_key]
                cumul_points.append(
                    TimelineDataPoint(timestamp=ts_key, value=round(running, 1))
                )
            series["rainfall_cumul"] = TimelineSeries(
                type="rainfall_cumul",
                name="Cumul pluie",
                unit="mm",
                color="#1e40af",
                data=cumul_points,
            )

    # Query tide data if requested
    if include_tide:
        # Cache-through: fetch tides from APIs if InfluxDB is empty
        await _ensure_tide_data(start, end)

        tide_data = await query_tides_timeline(
            start=start,
            end=end,
            port="LIBOURNE",
            resolution=resolution,
        )

        # Also try BORDEAUX port (Open-Meteo writes with port=BORDEAUX)
        if not tide_data:
            tide_data = await query_tides_timeline(
                start=start,
                end=end,
                port="BORDEAUX",
                resolution=resolution,
            )

        if tide_data:
            series["tide:LIBOURNE"] = TimelineSeries(
                type="tide",
                name="Maree Libourne",
                unit="m",
                color="#3b82f6",
                data=[
                    TimelineDataPoint(timestamp=p["timestamp"], value=p["value"])
                    for p in tide_data
                ],
            )

    # Query soil data if requested
    if include_soil:
        await _ensure_soil_data(start, end)

        soil_data = await query_soil_timeline(
            start=start,
            end=end,
            resolution=resolution,
        )

        soil_series_config = {
            "moisture_shallow": {
                "type": "soil_moisture",
                "name": "Humidite sol (0-7cm)",
                "unit": "m\u00b3/m\u00b3",
                "color": "#7c3aed",
            },
            "moisture_deep": {
                "type": "soil_moisture",
                "name": "Humidite sol (7-100cm)",
                "unit": "m\u00b3/m\u00b3",
                "color": "#6366f1",
            },
            "soil_temp": {
                "type": "soil_temperature",
                "name": "Temperature sol",
                "unit": "\u00b0C",
                "color": "#f97316",
            },
            "et0_mm": {
                "type": "evapotranspiration",
                "name": "Evapotranspiration (ET0)",
                "unit": "mm/j",
                "color": "#22c55e",
            },
        }

        for field_name, config in soil_series_config.items():
            points = soil_data.get(field_name, [])
            if points:
                series[f"soil:{field_name}"] = TimelineSeries(
                    type=config["type"],
                    name=config["name"],
                    unit=config["unit"],
                    color=config["color"],
                    data=[
                        TimelineDataPoint(timestamp=p["timestamp"], value=p["value"])
                        for p in points
                    ],
                )

    # Query Infoclimat rainfall if requested
    if include_infoclimat:
        await _ensure_infoclimat_rainfall(start, end)

        infoclimat_data = await query_rainfall_by_station(
            start=start,
            end=end,
            sources=["infoclimat"],
            resolution=resolution,
        )

        for source_city, points in infoclimat_data.items():
            if points:
                station_name = source_city.replace("infoclimat:", "")
                series[f"rainfall:{source_city}"] = TimelineSeries(
                    type="rainfall",
                    name=f"Infoclimat ({station_name})",
                    unit="mm/h",
                    color="#dc2626",
                    data=[
                        TimelineDataPoint(timestamp=p["timestamp"], value=p["value"])
                        for p in points
                    ],
                )

    # Query wind data if requested
    if include_wind:
        await _ensure_wind_data(start, end)

        wind_data = await query_wind_timeline(
            start=start,
            end=end,
            resolution=resolution,
        )

        wind_series_config = {
            "speed_kmh": {
                "type": "wind_speed",
                "name": "Vent moyen",
                "unit": "km/h",
                "color": "#64748b",
            },
            "westerly_kmh": {
                "type": "wind_westerly",
                "name": "Composante Ouest (maree)",
                "unit": "km/h",
                "color": "#0ea5e9",
            },
        }

        for field_name, config in wind_series_config.items():
            points = wind_data.get(field_name, [])
            if points:
                series[f"wind:{field_name}"] = TimelineSeries(
                    type=config["type"],
                    name=config["name"],
                    unit=config["unit"],
                    color=config["color"],
                    data=[
                        TimelineDataPoint(timestamp=p["timestamp"], value=p["value"])
                        for p in points
                    ],
                )

    # Query risk scores if requested
    if include_risk:
        # Cache-through: backfill risk scores from historical inputs if sparse
        await _ensure_risk_data(start, end)

        risk_data = await query_risk_scores_timeline(
            start=start,
            end=end,
            resolution=resolution,
        )

        if risk_data:
            series["risk:score"] = TimelineSeries(
                type="risk_score",
                name="Score de risque",
                unit="/100",
                color="#f59e0b",
                data=[
                    TimelineDataPoint(timestamp=p["timestamp"], value=p["value"])
                    for p in risk_data
                ],
            )

    # Include 7-day risk forecast overlay
    if include_forecast:
        try:
            from app.services.forecast import get_risk_forecast_v2
            forecast_data = await get_risk_forecast_v2(hours=168)

            forecast_points = []
            forecast_rain_points = []
            forecast_level_points = []

            for fp in forecast_data.get("forecasts", []):
                ts = fp["timestamp"]
                forecast_points.append(
                    TimelineDataPoint(timestamp=ts, value=fp["score"])
                )
                forecast_rain_points.append(
                    TimelineDataPoint(timestamp=ts, value=fp["rain_1h_mm"])
                )
                forecast_level_points.append(
                    TimelineDataPoint(timestamp=ts, value=fp["projected_level_m"])
                )

            if forecast_points:
                series["forecast:risk_score"] = TimelineSeries(
                    type="forecast_risk_score",
                    name="Prevision risque (7j)",
                    unit="/100",
                    color="#8b5cf6",
                    data=forecast_points,
                )
                series["forecast:rain_1h"] = TimelineSeries(
                    type="forecast_rainfall",
                    name="Prevision pluie (1h)",
                    unit="mm/h",
                    color="#a78bfa",
                    data=forecast_rain_points,
                )
                series["forecast:projected_level"] = TimelineSeries(
                    type="forecast_level",
                    name="Niveau projete",
                    unit="m",
                    color="#c4b5fd",
                    data=forecast_level_points,
                )
        except Exception as e_fc:
            print(f"Forecast overlay failed: {e_fc}")

    return UnifiedTimelineData(
        start=start,
        end=end,
        resolution=resolution,
        series=series,
    )
