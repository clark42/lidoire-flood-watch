"""Real-time data endpoints."""

import asyncio
from datetime import datetime
from fastapi import APIRouter
from dateutil.parser import isoparse

from app.models.schemas import (
    RealtimeData,
    RiskScore,
    RiskLevel,
    RiskFactors,
    RiskWeights,
    TideForecast,
    TideData,
    TideType,
    RiverLevel,
    RiverLevelForecast,
    RainfallSummary,
    BasinRainfallSummary,
    MultiStationData,
    StationLevel,
    Tendency,
    SoilConditions,
    WindData,
)
from app.services.risk_v2 import calculate_risk_score_v2
from app.services.collectors.tide import get_current_tide_data
from app.services.collectors.vigicrues import get_current_river_level
from app.services.collectors.weather import get_rainfall_summary, get_soil_wind_cached
from app.services.collectors.hubeau import get_multi_station_data, fetch_episode_context
from app.services.collectors.netatmo import get_basin_rainfall
from app.services.collectors.infoclimat import find_stations_near, get_basin_rainfall_infoclimat
from app.services.collectors.meteo_agricole import get_meteo_agricole_cached, get_nowcast_rain_cached
from app.services.influx_client import (
    query_basin_rainfall_sparkline,
    query_historical_snapshot,
    write_river_levels_batch,
    write_rainfall,
)
from app.config import get_settings

router = APIRouter()


@router.get("", response_model=RealtimeData)
async def get_realtime_data():
    """Get current real-time data for the dashboard."""
    now = datetime.utcnow()

    # Fetch all independent data sources in parallel
    async def _safe_infoclimat():
        try:
            return await get_basin_rainfall_infoclimat()
        except Exception as e:
            print(f"Infoclimat enrichment skipped: {e}")
            return {}

    (
        tide_data,
        river_data,
        rainfall_data,
        stations_data,
        soil_wind_data,
        meteo_agricole_data,
        nowcast_rain_data,
        netatmo_data,
        infoclimat_rain,
    ) = await asyncio.gather(
        get_current_tide_data(),
        get_current_river_level(),
        get_rainfall_summary(),
        get_multi_station_data(),
        get_soil_wind_cached(),
        get_meteo_agricole_cached(),
        get_nowcast_rain_cached(),
        get_basin_rainfall(),
        _safe_infoclimat(),
    )

    # Enrich rainfall with Netatmo data if available
    if netatmo_data.get("available"):
        netatmo_1h = netatmo_data.get("rain_1h_avg_mm", 0)
        netatmo_24h = netatmo_data.get("rain_24h_avg_mm", 0)

        # Take MAX of Open-Meteo and Netatmo — preserve all existing fields
        if netatmo_1h > 0 or netatmo_24h > 0:
            rainfall_data = rainfall_data.model_copy(update={
                "last_hour_mm": max(rainfall_data.last_hour_mm, netatmo_1h),
                "last_24h_mm": max(rainfall_data.last_24h_mm, netatmo_24h),
            })

    # Enrich rainfall with Infoclimat data if available
    if infoclimat_rain.get("available"):
        ic_24h = infoclimat_rain.get("rain_24h_avg_mm", 0)
        ic_1h = infoclimat_rain.get("rain_1h_avg_mm", 0)
        if ic_24h > 0 or ic_1h > 0:
            rainfall_data = rainfall_data.model_copy(update={
                "last_hour_mm": max(rainfall_data.last_hour_mm, ic_1h or 0),
                "last_24h_mm": max(rainfall_data.last_24h_mm, ic_24h or 0),
            })

    # Determine soil saturation factor using all available sources:
    # 1) Base factor from Open-Meteo shallow soil moisture
    soil_sat_factor = rainfall_data.soil_saturation_factor

    # 2) Deep soil moisture (Open-Meteo): saturated deep soil = higher risk
    moisture_deep = soil_wind_data.get("moisture_deep", 0)
    if moisture_deep and moisture_deep > 0.30:
        soil_sat_factor = min(1.5, soil_sat_factor + 0.15)
    elif moisture_deep and moisture_deep > 0.25:
        soil_sat_factor = min(1.5, soil_sat_factor + 0.05)

    # 3) Low evapotranspiration = slow drying = higher saturation risk
    et0 = soil_wind_data.get("et0_mm")
    if et0 is not None and et0 < 1.0:
        soil_sat_factor = min(1.5, soil_sat_factor + 0.05)

    # 4) Meteo Agricole 30-day water balance adjustment
    if meteo_agricole_data:
        surplus = meteo_agricole_data.surplus_deficit_30d_pct
        if surplus is not None:
            if surplus > 20:
                soil_sat_factor = min(1.5, soil_sat_factor + 0.1)
            elif surplus < -20:
                soil_sat_factor = max(1.0, soil_sat_factor - 0.1)

    # Determine westerly wind (max of Open-Meteo and Meteo Agricole gusts)
    westerly_wind = soil_wind_data.get("westerly_wind_kmh", 0.0)
    if meteo_agricole_data and meteo_agricole_data.wind_gust_kmh > 0:
        # Use gust if higher than sustained westerly
        westerly_wind = max(westerly_wind, meteo_agricole_data.wind_gust_kmh * 0.7)

    # Calculate risk score using v2 multi-station model
    risk = await calculate_risk_score_v2(
        stations_data=stations_data,
        tide=tide_data,
        rainfall=rainfall_data,
        westerly_wind_kmh=westerly_wind,
        soil_saturation_factor=soil_sat_factor,
    )

    # Build alerts list
    alerts = []
    if risk.level in [RiskLevel.ORANGE, RiskLevel.RED]:
        alerts.append(f"Risque {risk.level.value}: {risk.description}")
    if rainfall_data.last_24h_mm > 40:
        alerts.append(f"Fortes pluies: {rainfall_data.last_24h_mm:.1f}mm en 24h")
    if river_data.current.height_m > 4.0:
        alerts.append(f"Niveau Dordogne élevé: {river_data.current.height_m:.2f}m")

    # Add upstream alerts from multi-station data
    for station in stations_data.stations:
        if station.station_id != stations_data.primary_station_id:
            if station.current_level.height_m > station.threshold_orange:
                alerts.append(
                    f"Niveau élevé à {station.station_name} ({station.river}): "
                    f"{station.current_level.height_m:.2f}m - "
                    f"impact dans ~{station.delay_hours:.0f}h"
                )

    # Add Netatmo info if available
    if netatmo_data.get("available"):
        stations_count = netatmo_data.get("stations_count", 0)
        if stations_count > 0:
            alerts.append(f"Données Netatmo: {stations_count} stations actives")

    # Build soil conditions
    soil_conditions = SoilConditions(
        soil_moisture=soil_wind_data.get("soil_moisture"),
        moisture_deep=soil_wind_data.get("moisture_deep"),
        soil_temperature_0cm=soil_wind_data.get("soil_temperature_0cm"),
        et0_mm=soil_wind_data.get("et0_mm"),
        soil_saturation_factor=soil_sat_factor,
        precip_probability=soil_wind_data.get("precip_probability"),
        water_balance_30d_mm=meteo_agricole_data.precip_30d_mm if meteo_agricole_data else None,
        surplus_deficit_30d_pct=meteo_agricole_data.surplus_deficit_30d_pct if meteo_agricole_data else None,
        deficit_12m_mm=meteo_agricole_data.deficit_12m_mm if meteo_agricole_data else None,
    )

    # Build wind data
    wind_data = WindData(
        wind_speed_kmh=soil_wind_data.get("wind_speed_kmh", 0.0),
        wind_direction_deg=soil_wind_data.get("wind_direction_deg", 0.0),
        westerly_wind_kmh=westerly_wind,
    )

    return RealtimeData(
        timestamp=now,
        risk=risk,
        tide=tide_data,
        river=river_data,
        rainfall=rainfall_data,
        video=None,  # TODO: integrate video analysis
        alerts=alerts,
        stations=stations_data,
        netatmo_available=netatmo_data.get("available", False),
        soil=soil_conditions,
        wind=wind_data,
        meteo_agricole=meteo_agricole_data,
        nowcast_rain=nowcast_rain_data,
    )


@router.get("/stations", response_model=MultiStationData)
async def get_all_stations():
    """Get current data from all monitoring stations."""
    return await get_multi_station_data()


@router.get("/netatmo")
async def get_netatmo_rainfall():
    """Get rainfall data from Netatmo stations."""
    return await get_basin_rainfall()


@router.get("/tide", response_model=TideForecast)
async def get_tide():
    """Get current tide information."""
    return await get_current_tide_data()


@router.get("/river", response_model=RiverLevelForecast)
async def get_river():
    """Get current river level information."""
    return await get_current_river_level()


@router.get("/rainfall", response_model=RainfallSummary)
async def get_rainfall():
    """Get current rainfall summary."""
    return await get_rainfall_summary()


@router.get("/infoclimat")
async def get_infoclimat_data():
    """Get Infoclimat stations and rainfall data."""
    settings = get_settings()

    # Get nearby stations
    stations = await find_stations_near(
        lat=settings.moulin_lat,
        lon=settings.moulin_lon,
        radius_km=50
    )

    # Try to get rainfall data (may fail due to IP restrictions)
    rainfall = await get_basin_rainfall_infoclimat()

    return {
        "stations_found": len(stations),
        "nearest_stations": [
            {
                "name": s.get("name"),
                "distance_km": s.get("distance_km"),
                "source": s.get("source"),
                "departement": s.get("departement"),
                "lat": s.get("lat"),
                "lon": s.get("lon"),
            }
            for s in stations[:10]
        ],
        "rainfall": rainfall,
    }


@router.get("/sources")
async def get_data_sources_status():
    """Get status of all data sources."""
    settings = get_settings()
    sources = []

    # Hub'Eau
    try:
        stations_data = await get_multi_station_data()
        active_stations = len([s for s in stations_data.stations if s.current_level])
        sources.append({
            "name": "Hub'Eau Hydrometrie",
            "status": "ok" if active_stations > 0 else "error",
            "description": "Niveaux des cours d'eau (Vigicrues/DREAL)",
            "details": f"{active_stations} stations actives sur {len(stations_data.stations)}",
            "url": "https://hubeau.eaufrance.fr/page/api-hydrometrie",
        })
    except Exception as e:
        sources.append({
            "name": "Hub'Eau Hydrometrie",
            "status": "error",
            "description": "Niveaux des cours d'eau",
            "details": str(e),
        })

    # Netatmo
    try:
        netatmo_data = await get_basin_rainfall()
        sources.append({
            "name": "Netatmo Weather",
            "status": "ok" if netatmo_data.get("available") else "degraded",
            "description": "Pluviometrie citoyenne",
            "details": f"{netatmo_data.get('stations_count', 0)} stations avec pluviometre",
            "url": "https://dev.netatmo.com/",
        })
    except Exception as e:
        sources.append({
            "name": "Netatmo Weather",
            "status": "error",
            "description": "Pluviometrie citoyenne",
            "details": str(e),
        })

    # Infoclimat
    try:
        infoclimat_stations = await find_stations_near(
            settings.moulin_lat, settings.moulin_lon, 50
        )
        infoclimat_rainfall = await get_basin_rainfall_infoclimat()
        sources.append({
            "name": "Infoclimat",
            "status": "ok" if infoclimat_rainfall.get("available") else "degraded",
            "description": "Reseau meteo amateur (StatIC)",
            "details": f"{len(infoclimat_stations)} stations proches" + (
                " - API restreinte par IP" if not infoclimat_rainfall.get("available") else ""
            ),
            "url": "https://www.infoclimat.fr/opendata/",
        })
    except Exception as e:
        sources.append({
            "name": "Infoclimat",
            "status": "error",
            "description": "Reseau meteo amateur",
            "details": str(e),
        })

    # Tide (Open-Meteo Marine)
    try:
        tide_data = await get_current_tide_data()
        has_tide = tide_data.tides and len(tide_data.tides) > 0
        sources.append({
            "name": "Open-Meteo Marine",
            "status": "ok" if has_tide else "degraded",
            "description": "Marees et coefficients",
            "details": f"Coef actuel: {tide_data.current_coefficient or 'N/A'}",
            "url": "https://open-meteo.com/en/docs/marine-weather-api",
        })
    except Exception as e:
        sources.append({
            "name": "Open-Meteo Marine",
            "status": "error",
            "description": "Marees et coefficients",
            "details": str(e),
        })

    # Weather forecast (Open-Meteo)
    try:
        rainfall = await get_rainfall_summary()
        sources.append({
            "name": "Open-Meteo Weather",
            "status": "ok",
            "description": "Previsions meteorologiques",
            "details": f"Prevu 24h: {rainfall.forecast_24h_mm:.1f}mm, 72h: {rainfall.forecast_72h_mm:.1f}mm",
            "url": "https://open-meteo.com/en/docs",
        })
    except Exception as e:
        sources.append({
            "name": "Open-Meteo Weather",
            "status": "error",
            "description": "Previsions meteorologiques",
            "details": str(e),
        })

    return {
        "timestamp": datetime.utcnow().isoformat(),
        "sources": sources,
    }


@router.get("/basin-rainfall")
async def get_basin_rainfall_summary():
    """Get aggregated rainfall data for the Lidoire basin."""
    settings = get_settings()
    netatmo_data = await get_basin_rainfall()

    # Group lidoire stations by position
    by_position = {"amont": {"total": 0, "count": 0}, "milieu": {"total": 0, "count": 0}, "aval": {"total": 0, "count": 0}}
    basin_1h = 0.0
    basin_24h = 0.0
    station_count = 0

    if netatmo_data.get("available") and netatmo_data.get("lidoire_stations"):
        for station in netatmo_data["lidoire_stations"]:
            pos = station.get("position", "milieu")
            rain_24h = station.get("rain_24h_mm") or 0.0
            rain_1h = station.get("rain_1h_mm") or 0.0

            if pos in by_position:
                by_position[pos]["total"] += rain_24h
                by_position[pos]["count"] += 1

            basin_1h += rain_1h
            basin_24h += rain_24h
            station_count += 1

    # Compute averages
    if station_count > 0:
        basin_1h /= station_count
        basin_24h /= station_count

    for pos in by_position:
        cnt = by_position[pos]["count"]
        by_position[pos]["avg"] = round(by_position[pos]["total"] / cnt, 1) if cnt > 0 else 0.0

    # Weighted average: amont 30%, milieu 40%, aval 30%
    weighted = (
        by_position["amont"].get("avg", 0) * 0.3 +
        by_position["milieu"].get("avg", 0) * 0.4 +
        by_position["aval"].get("avg", 0) * 0.3
    )

    # Get sparkline data
    sparkline = await query_basin_rainfall_sparkline(24)

    return BasinRainfallSummary(
        timestamp=datetime.utcnow(),
        basin_average_1h_mm=round(basin_1h, 1),
        basin_average_24h_mm=round(basin_24h, 1),
        weighted_average_24h_mm=round(weighted, 1),
        by_position=by_position,
        sparkline_24h=sparkline,
    )


def _to_naive(dt: datetime) -> datetime:
    """Strip timezone info for safe comparison with naive datetimes."""
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


@router.get("/at")
async def get_realtime_data_at(timestamp: str):
    """Get reconstructed dashboard data at a specific historical timestamp (TimeMachine).

    Reconstructs the dashboard state from InfluxDB stored data with a ±6h window.
    If no data exists in InfluxDB, fetches from external APIs (Hub'Eau, Open-Meteo Archive)
    and stores for future queries (cache-through pattern).
    """
    from datetime import timedelta

    target_time = _to_naive(isoparse(timestamp))
    settings = get_settings()

    # Fetch snapshots in parallel for different time windows:
    # - snapshot: ±6h around target (river levels, current state)
    # - snapshot_24h: 24h before target (rain accumulation)
    # - snapshot_72h: 72h before target (sustained rain for flood risk)
    snapshot, snapshot_24h, snapshot_72h = await asyncio.gather(
        query_historical_snapshot(target_time, window_hours=6),
        query_historical_snapshot(target_time - timedelta(hours=12), window_hours=12),
        query_historical_snapshot(target_time - timedelta(hours=48), window_hours=36),
    )

    # === CACHE-THROUGH: Fetch from APIs if InfluxDB has no data ===
    # Check if we have river level data
    has_river_data = any(
        len(levels) > 0 for levels in snapshot["river_levels"].values()
    )

    if not has_river_data:
        print(f"TimeMachine: no InfluxDB data for {target_time}, fetching from Hub'Eau...")
        # Fetch from Hub'Eau (Hydroportail for sub-hourly historical data)
        river_data = await fetch_episode_context(
            target_time,
            hours_before=6,
            hours_after=6,
        )

        # Store to InfluxDB for future queries
        all_levels = []
        for station_id, levels in river_data.items():
            for lv in levels:
                all_levels.append({
                    "station_id": lv.station_id,
                    "station_name": lv.station_name,
                    "river": lv.river,
                    "height_m": lv.height_m,
                    "timestamp": lv.timestamp,
                    "flow_m3s": lv.flow_m3s,
                })
            # Update snapshot with fetched data
            snapshot["river_levels"][station_id] = [
                {
                    "timestamp": lv.timestamp.isoformat(),
                    "height_m": lv.height_m,
                    "station_name": lv.station_name,
                    "river": lv.river,
                }
                for lv in levels
            ]

        if all_levels:
            stored = await write_river_levels_batch(all_levels)
            print(f"TimeMachine: stored {stored} river level points to InfluxDB")

    # Check if we have rainfall data in the critical 24h window before target_time
    # Not just "any" data, but specifically for the time period we need
    rain_in_24h_window = 0
    for r in snapshot["rainfall"] + snapshot_24h["rainfall"]:
        if r.get("field") == "precipitation_mm_h" and r.get("value") is not None:
            try:
                ts = _to_naive(isoparse(r.get("timestamp", "")))
                hours_before = (target_time - ts).total_seconds() / 3600
                if 0 <= hours_before <= 24:
                    rain_in_24h_window += 1
            except (ValueError, TypeError):
                continue

    # If we have less than 12 hours of rain data in the 24h window, fetch more
    if rain_in_24h_window < 12:
        print(f"TimeMachine: insufficient rainfall data ({rain_in_24h_window} pts in 24h window), fetching from Open-Meteo Archive...")
        # Fetch from Open-Meteo Archive API for full 72h
        start = target_time - timedelta(hours=80)  # Extra buffer
        end = target_time + timedelta(hours=6)

        try:
            import httpx
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    "https://archive-api.open-meteo.com/v1/archive",
                    params={
                        "latitude": settings.moulin_lat,
                        "longitude": settings.moulin_lon,
                        "start_date": start.strftime("%Y-%m-%d"),
                        "end_date": end.strftime("%Y-%m-%d"),
                        "hourly": "precipitation",
                        "timezone": "UTC",
                    },
                    timeout=30.0,
                )
                response.raise_for_status()
                data = response.json()

                hourly = data.get("hourly", {})
                times = hourly.get("time", [])
                precip = hourly.get("precipitation", [])

                # Store to InfluxDB and update snapshots
                stored_count = 0
                for i, time_str in enumerate(times):
                    if i < len(precip) and precip[i] is not None:
                        ts = datetime.fromisoformat(time_str)
                        await write_rainfall(
                            source="open-meteo-archive",
                            precipitation_mm_h=precip[i],
                            timestamp=ts,
                            station_city="Moulin",
                        )
                        stored_count += 1
                        # Add to appropriate snapshot for rainfall calculation
                        hours_before = (target_time - ts).total_seconds() / 3600
                        rain_record = {
                            "timestamp": ts.isoformat(),
                            "value": precip[i],
                            "field": "precipitation_mm_h",
                            "source": "open-meteo-archive",
                            "city": "Moulin",
                        }
                        if hours_before <= 24:
                            snapshot_24h["rainfall"].append(rain_record)
                        elif hours_before <= 72:
                            snapshot_72h["rainfall"].append(rain_record)

                print(f"TimeMachine: stored {stored_count} rainfall points to InfluxDB")

        except Exception as e:
            print(f"TimeMachine: error fetching historical rainfall: {e}")

    # === END CACHE-THROUGH ===

    # Reconstruct stations data
    stations_list = []
    primary_level = None
    primary_tendency = Tendency.STABLE

    for station_cfg in settings.hubeau_stations:
        station_id = station_cfg["id"]
        levels = snapshot["river_levels"].get(station_id, [])

        # Find the closest measurement to target_time
        best = None
        best_diff = float("inf")
        for lv in levels:
            t = _to_naive(isoparse(lv["timestamp"]))
            diff = abs((t - target_time).total_seconds())
            if diff < best_diff:
                best_diff = diff
                best = lv

        if best:
            height = best["height_m"]
            river_level = RiverLevel(
                timestamp=best["timestamp"],
                station_id=station_id,
                station_name=station_cfg["name"],
                river=station_cfg["river"],
                height_m=height,
            )

            # Compute tendency using the 3h window BEFORE target time
            # (not first-to-last across full ±6h window which masks rising/falling phases)
            tendency = Tendency.STABLE
            rate = 0.0
            if len(levels) >= 2:
                # Parse all levels with timestamps
                parsed = []
                for lv in levels:
                    try:
                        t = _to_naive(isoparse(lv["timestamp"]))
                        parsed.append((t, lv["height_m"]))
                    except (ValueError, TypeError):
                        continue
                parsed.sort(key=lambda x: x[0])

                # Find the point closest to target_time (the "current" value)
                best_t = _to_naive(isoparse(best["timestamp"]))

                # Find a reference point 2-4h before target_time
                ref_point = None
                for t, h in parsed:
                    hours_before = (target_time - t).total_seconds() / 3600
                    if 1.5 <= hours_before <= 4.0:
                        # Prefer the one closest to 3h before
                        if ref_point is None or abs(hours_before - 3.0) < abs(ref_point[2] - 3.0):
                            ref_point = (t, h, hours_before)

                if ref_point is not None:
                    time_diff_h = (best_t - ref_point[0]).total_seconds() / 3600
                    if time_diff_h > 0.5:
                        rate = (height - ref_point[1]) / time_diff_h * 100  # cm/h
                else:
                    # Fallback: use only points AT or BEFORE target time
                    before_points = [(t, h) for t, h in parsed if t <= target_time]
                    if len(before_points) >= 2:
                        time_diff_h = (before_points[-1][0] - before_points[0][0]).total_seconds() / 3600
                        if time_diff_h > 0.5:
                            rate = (before_points[-1][1] - before_points[0][1]) / time_diff_h * 100

                if rate > 1:
                    tendency = Tendency.RISING
                elif rate < -1:
                    tendency = Tendency.FALLING

            sl = StationLevel(
                station_id=station_id,
                station_name=station_cfg["name"],
                river=station_cfg["river"],
                current_level=river_level,
                tendency=tendency,
                rate_cm_per_hour=round(rate, 1),
                distance_km=station_cfg["distance_km"],
                delay_hours=station_cfg["delay_hours"],
                weight=station_cfg["weight"],
                threshold_yellow=station_cfg["thresholds"]["yellow"],
                threshold_orange=station_cfg["thresholds"]["orange"],
                threshold_red=station_cfg["thresholds"]["red"],
                lat=station_cfg.get("lat"),
                lon=station_cfg.get("lon"),
            )
            stations_list.append(sl)

            if station_id == settings.hubeau_primary_station:
                primary_level = river_level
                primary_tendency = tendency
        else:
            # No data — still include the station with zeroed values
            empty_level = RiverLevel(
                timestamp=target_time.isoformat(),
                station_id=station_id,
                station_name=station_cfg["name"],
                river=station_cfg["river"],
                height_m=0.0,
            )
            sl = StationLevel(
                station_id=station_id,
                station_name=station_cfg["name"],
                river=station_cfg["river"],
                current_level=empty_level,
                tendency=Tendency.STABLE,
                distance_km=station_cfg["distance_km"],
                delay_hours=station_cfg["delay_hours"],
                weight=station_cfg["weight"],
                threshold_yellow=station_cfg["thresholds"]["yellow"],
                threshold_orange=station_cfg["thresholds"]["orange"],
                threshold_red=station_cfg["thresholds"]["red"],
                lat=station_cfg.get("lat"),
                lon=station_cfg.get("lon"),
            )
            stations_list.append(sl)

    stations_data = MultiStationData(
        timestamp=target_time,
        stations=stations_list,
        primary_station_id=settings.hubeau_primary_station,
    )

    # Reconstruct tide data
    tide_forecast = TideForecast(port="LIBOURNE", tides=[])
    coef = None
    if snapshot["tides"]:
        # Find best coefficient near target time
        best_coef_diff = float("inf")
        for t in snapshot["tides"]:
            if t.get("field") == "coefficient" and t.get("value"):
                ts = _to_naive(isoparse(t["timestamp"]))
                diff = abs((ts - target_time).total_seconds())
                if diff < best_coef_diff:
                    best_coef_diff = diff
                    coef = int(t["value"])

        # Reconstruct tide entries for next_high_tide / next_low_tide
        for t in snapshot["tides"]:
            if t.get("field") == "height_m" and t.get("value") is not None:
                tide_type_str = t.get("tide_type")
                if tide_type_str in ("PM", "BM"):
                    ts = _to_naive(isoparse(t["timestamp"]))
                    td = TideData(
                        timestamp=ts,
                        port=t.get("port", "LIBOURNE"),
                        tide_type=TideType(tide_type_str),
                        height_m=t["value"],
                        coefficient=coef,
                    )
                    tide_forecast.tides.append(td)
                    # Set next high/low relative to target_time
                    if ts >= target_time:
                        if tide_type_str == "PM" and not tide_forecast.next_high_tide:
                            tide_forecast.next_high_tide = td
                        elif tide_type_str == "BM" and not tide_forecast.next_low_tide:
                            tide_forecast.next_low_tide = td

    tide_forecast.current_coefficient = coef

    # Reconstruct rainfall: cumulative from all snapshots
    # Average across multiple sources per timestamp to avoid undercount
    rain_1h = 0.0
    rain_24h_sum = 0.0
    rain_72h_sum = 0.0
    rain_7d_sum = 0.0

    # Collect all rainfall records, group by timestamp (average across sources)
    rain_by_ts: dict[str, list[float]] = {}
    for r in snapshot_72h["rainfall"] + snapshot_24h["rainfall"] + snapshot["rainfall"]:
        if r.get("field") == "precipitation_mm_h" and r.get("value") is not None:
            ts = r.get("timestamp", "")
            if ts not in rain_by_ts:
                rain_by_ts[ts] = []
            rain_by_ts[ts].append(r["value"])

    # Sum averaged hourly precipitation for each time window
    for ts_str, values in rain_by_ts.items():
        try:
            ts = _to_naive(isoparse(ts_str))
            hours_before = (target_time - ts).total_seconds() / 3600
            avg_value = sum(values) / len(values)
            if 0 <= hours_before <= 1:
                rain_1h = max(rain_1h, avg_value)
            if 0 <= hours_before <= 24:
                rain_24h_sum += avg_value
            if 0 <= hours_before <= 72:
                rain_72h_sum += avg_value
            if 0 <= hours_before <= 168:
                rain_7d_sum += avg_value
        except (ValueError, TypeError):
            continue

    # Also check for pre-aggregated 24h field
    for r in snapshot["rainfall"]:
        if r.get("field") == "precipitation_mm_24h" and r.get("value") is not None:
            rain_24h_sum = max(rain_24h_sum, r["value"])

    rainfall = RainfallSummary(
        last_hour_mm=round(rain_1h, 1),
        last_24h_mm=round(rain_24h_sum, 1),
        last_72h_mm=round(rain_72h_sum, 1),
        last_7d_mm=round(rain_7d_sum, 1),
    )

    # Recalculate risk score using v2 model instead of reading stored score
    risk = await calculate_risk_score_v2(
        stations_data=stations_data,
        tide=tide_forecast,
        rainfall=rainfall,
    )
    # Override description for TimeMachine context
    risk.description = f"Donnees historiques du {target_time.strftime('%d/%m/%Y %H:%M')}"
    risk.recommendations = ["Donnees reconstituees depuis l'historique"]

    if not primary_level:
        # Fallback to primary station (Pessac-sur-Dordogne)
        primary_cfg = next(
            (s for s in settings.hubeau_stations if s["id"] == settings.hubeau_primary_station),
            settings.hubeau_stations[0],
        )
        primary_level = RiverLevel(
            timestamp=target_time.isoformat(),
            station_id=settings.hubeau_primary_station,
            station_name=primary_cfg["name"],
            river=primary_cfg["river"],
            height_m=0.0,
        )

    river_forecast = RiverLevelForecast(
        current=primary_level,
        tendency=primary_tendency,
    )

    return RealtimeData(
        timestamp=target_time,
        risk=risk,
        tide=tide_forecast,
        river=river_forecast,
        rainfall=rainfall,
        video=None,
        alerts=["Mode TimeMachine - donnees historiques"],
        stations=stations_data,
        netatmo_available=False,
    )
