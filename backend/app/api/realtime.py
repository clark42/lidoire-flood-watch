"""Real-time data endpoints."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from dateutil.parser import isoparse

from app.models.schemas import (
    RealtimeData,
    RiskLevel,
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
from app.services import risk_core
from app.services.risk_v2 import (
    TIDE_WINDOW_H,
    breakdown_to_risk_score,
    gather_realtime_inputs,
    load_primary_levels,
    load_tide_rows,
    rain_hourly_from_records,
    risk_from_inputs,
    tide_inputs_from_records,
)
from app.services.collectors.tide import get_current_tide_data
from app.services.collectors.vigicrues import get_current_river_level
from app.services.collectors.weather import get_rainfall_summary
from app.services.collectors.hubeau import get_multi_station_data, fetch_episode_context
# Optional sources are read from the caches filled by the scheduler (peek_*):
# no request awaits Netatmo, Infoclimat or Meteo Agricole.
from app.services.collectors.netatmo import peek_basin_rainfall
from app.services.collectors.infoclimat import find_stations_near, peek_basin_rainfall_infoclimat
from app.services.collectors.meteo_agricole import peek_nowcast_rain
from app.services.influx_client import (
    make_rainfall_point,
    query_basin_rainfall_sparkline,
    query_historical_snapshot,
    query_rainfall,
    write_points_batch,
    write_river_levels_batch,
)
from app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter()


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def primary_station_alert(stations_data: Optional[MultiStationData]) -> Optional[str]:
    """'Niveau Dordogne élevé' alert from the primary NON-tidal station.

    Based on the primary station (Pessac) against its own orange threshold.
    Libourne (Vigicrues/Hub'Eau P577001001) is never used: it oscillates
    around 4 m with the tide, so a fixed 4.0 m test fired at most spring
    high tides with no flood signal (the model gives it weight 0).
    """
    if not stations_data:
        return None
    primary = next(
        (s for s in stations_data.stations
         if s.station_id == stations_data.primary_station_id and s.weight > 0),
        None,
    )
    if primary is None or primary.current_level.height_m < primary.threshold_orange:
        return None
    return (
        f"Niveau Dordogne élevé à {primary.station_name}: "
        f"{primary.current_level.height_m:.2f}m (seuil orange {primary.threshold_orange:.2f}m)"
    )


@router.get("", response_model=RealtimeData)
async def get_realtime_data():
    """Get current real-time data for the dashboard.

    The risk is computed from the same fused inputs as /api/risk and the
    hourly scheduler job; this read endpoint never stores the score.
    """
    inputs, river_data = await asyncio.gather(
        gather_realtime_inputs(),
        get_current_river_level(),
    )
    nowcast_rain_data = peek_nowcast_rain()
    now = inputs.timestamp
    risk = risk_from_inputs(inputs)

    rainfall_data = inputs.rainfall
    stations_data = inputs.stations
    netatmo_data = inputs.netatmo
    soil_wind_data = inputs.soil_wind
    meteo_agricole_data = inputs.meteo_agricole

    # Build alerts list
    alerts = []
    if risk.level in [RiskLevel.ORANGE, RiskLevel.RED]:
        alerts.append(f"Risque {risk.level.value}: {risk.description}")
    if rainfall_data.last_24h_mm > 40:
        alerts.append(f"Fortes pluies: {rainfall_data.last_24h_mm:.1f}mm en 24h")
    dordogne_alert = primary_station_alert(stations_data)
    if dordogne_alert:
        alerts.append(dordogne_alert)

    # Add upstream alerts from multi-station data (zero-weight stations skipped)
    for station in stations_data.stations:
        if station.station_id != stations_data.primary_station_id and station.weight > 0:
            if station.current_level.height_m >= station.threshold_orange:
                alerts.append(
                    f"Niveau élevé à {station.station_name} ({station.river}): "
                    f"{station.current_level.height_m:.2f}m - "
                    f"impact dans ~{station.delay_hours:.0f}h"
                )

    if not risk.factors.tide_available:
        alerts.append("Marée indisponible ou estimée : non prise en compte dans le score")

    # Add Netatmo info if available
    if netatmo_data.get("available"):
        stations_count = netatmo_data.get("stations_count", 0)
        if stations_count > 0:
            alerts.append(f"Données Netatmo: {stations_count} stations actives")

    soil_conditions = SoilConditions(
        soil_moisture=soil_wind_data.get("soil_moisture"),
        moisture_deep=soil_wind_data.get("moisture_deep"),
        soil_temperature_0cm=soil_wind_data.get("soil_temperature_0cm"),
        et0_mm=soil_wind_data.get("et0_mm"),
        soil_saturation_factor=inputs.soil_saturation_factor,
        precip_probability=soil_wind_data.get("precip_probability"),
        water_balance_30d_mm=meteo_agricole_data.precip_30d_mm if meteo_agricole_data else None,
        surplus_deficit_30d_pct=meteo_agricole_data.surplus_deficit_30d_pct if meteo_agricole_data else None,
        deficit_12m_mm=meteo_agricole_data.deficit_12m_mm if meteo_agricole_data else None,
    )

    wind_data = WindData(
        wind_speed_kmh=soil_wind_data.get("wind_speed_kmh", 0.0),
        wind_direction_deg=soil_wind_data.get("wind_direction_deg", 0.0),
        westerly_wind_kmh=inputs.westerly_wind_kmh,
    )

    return RealtimeData(
        timestamp=now,
        risk=risk,
        tide=inputs.tide,
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
    """Get rainfall data from Netatmo stations (last scheduler collection)."""
    return peek_basin_rainfall()


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

    # Nearby stations from the already loaded list (never fetched here)
    stations = await find_stations_near(
        lat=settings.moulin_lat,
        lon=settings.moulin_lon,
        radius_km=50,
        fetch=False,
    )

    # Last scheduler collection (may be unavailable: IP restriction, outage)
    rainfall = peek_basin_rainfall_infoclimat()

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
        netatmo_data = peek_basin_rainfall()
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
            settings.moulin_lat, settings.moulin_lon, 50, fetch=False,
        )
        infoclimat_rainfall = peek_basin_rainfall_infoclimat()
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
        "timestamp": _utcnow().isoformat(),
        "sources": sources,
    }


@router.get("/basin-rainfall")
async def get_basin_rainfall_summary():
    """Get aggregated rainfall data for the Lidoire basin."""
    settings = get_settings()
    netatmo_data = peek_basin_rainfall()

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

    # Weighted average: amont 30%, milieu 40%, aval 30%, renormalised over
    # the positions that have stations (an empty position is not 0 mm)
    position_weights = {"amont": 0.3, "milieu": 0.4, "aval": 0.3}
    covered = {p: w for p, w in position_weights.items() if by_position[p]["count"] > 0}
    total_weight = sum(covered.values())
    weighted = (
        sum(by_position[p]["avg"] * w for p, w in covered.items()) / total_weight
        if total_weight > 0 else 0.0
    )

    # Get sparkline data
    sparkline = await query_basin_rainfall_sparkline(24)

    return BasinRainfallSummary(
        timestamp=_utcnow(),
        available=station_count > 0,
        stations_count=station_count,
        basin_average_1h_mm=round(basin_1h, 1),
        basin_average_24h_mm=round(basin_24h, 1),
        weighted_average_24h_mm=round(weighted, 1),
        by_position=by_position,
        sparkline_24h=sparkline,
    )


def _to_naive(dt: datetime) -> datetime:
    """Convert to naive UTC for safe comparison with naive datetimes."""
    return risk_core.to_utc_naive(dt)


async def _cache_through_river_levels(target_time: datetime, snapshot: dict) -> None:
    """Fetch river levels from Hub'Eau when InfluxDB has none (cache-through)."""
    logger.info("TimeMachine: no InfluxDB river data for %s, fetching from Hub'Eau", target_time)
    river_data = await fetch_episode_context(target_time, hours_before=6, hours_after=6)

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
        logger.info("TimeMachine: stored %d river level points to InfluxDB", stored)


async def _cache_through_rainfall(target_time: datetime) -> list[dict]:
    """Fetch hourly rain from Open-Meteo Archive (cache-through); returns rows."""
    settings = get_settings()
    start = target_time - timedelta(hours=170)
    end = target_time + timedelta(hours=1)
    rows: list[dict] = []
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
            hourly = response.json().get("hourly", {})

        for time_str, mm in zip(hourly.get("time", []), hourly.get("precipitation", [])):
            if mm is None:
                continue
            rows.append({
                "timestamp": datetime.fromisoformat(time_str),
                "precipitation_mm_h": mm,
                "source": "open-meteo-archive",
                "city": "Moulin",
            })
        stored = await write_points_batch([
            make_rainfall_point(
                source=r["source"], precipitation_mm_h=r["precipitation_mm_h"],
                timestamp=r["timestamp"], station_city=r["city"],
            )
            for r in rows
        ]) if rows else 0
        logger.info("TimeMachine: stored %d rainfall points to InfluxDB", stored)
    except Exception as e:
        logger.warning("TimeMachine: error fetching historical rainfall: %s", e)
    return rows


def _snapshot_station_points(snapshot: dict) -> dict[str, list[risk_core.LevelPoint]]:
    """Sorted (ts, height) points per station from a historical snapshot."""
    out: dict[str, list[risk_core.LevelPoint]] = {}
    for station_id, levels in snapshot["river_levels"].items():
        pts = []
        for lv in levels:
            try:
                pts.append((_to_naive(isoparse(lv["timestamp"])), float(lv["height_m"])))
            except (ValueError, TypeError, KeyError):
                continue
        pts.sort()
        out[station_id] = pts
    return out


def _snapshot_tide_rows(snapshot: dict) -> list[dict]:
    """Convert snapshot tide records to query_tides()-like rows.

    Confluence port only, as ``load_tide_rows`` (legacy ports have no phase lag).
    """
    port = get_settings().tide_port_confluence
    rows = []
    for t in snapshot["tides"]:
        if t.get("port") not in (None, port):
            continue
        try:
            ts = _to_naive(isoparse(t["timestamp"]))
        except (ValueError, TypeError, KeyError):
            continue
        rows.append({
            "timestamp": ts,
            "height_m": t["value"] if t.get("field") == "height_m" else None,
            "coefficient": t["value"] if t.get("field") == "coefficient" else None,
            "tide_type": t.get("tide_type"),
            "port": t.get("port"),
        })
    return rows


@router.get("/at")
async def get_realtime_data_at(timestamp: str):
    """Get reconstructed dashboard data at a specific historical timestamp (TimeMachine).

    The risk is recomputed with the same features as the historical backfill
    (observed points at or before the target only) and is NEVER stored.
    Missing raw data is fetched from Hub'Eau / Open-Meteo Archive and cached
    in InfluxDB (cache-through of inputs only).
    """
    try:
        target_time = _to_naive(isoparse(timestamp))
    except (ValueError, TypeError, OverflowError):
        raise HTTPException(status_code=400, detail=f"Invalid timestamp: {timestamp}")

    settings = get_settings()
    max_age = settings.risk_level_max_age_hours
    rate_window = settings.risk_rate_window_hours

    # Tides over +/-13h (same window and port rule as backfill_risk_scores):
    # the nearest high tide can be ~6h12 away, outside the +/-6h snapshot.
    tide_window = timedelta(hours=TIDE_WINDOW_H)
    # Primary levels over the trailing 12.5h (tide gate, v3.4): longer than
    # the -6h of the snapshot.
    snapshot, rain_rows, tide_rows, primary_rows = await asyncio.gather(
        query_historical_snapshot(target_time, window_hours=6),
        query_rainfall(target_time - timedelta(hours=169), target_time + timedelta(minutes=1)),
        load_tide_rows(target_time - tide_window, target_time + tide_window),
        load_primary_levels(target_time),
    )

    # === CACHE-THROUGH (inputs only) ===
    if not any(len(levels) > 0 for levels in snapshot["river_levels"].values()):
        await _cache_through_river_levels(target_time, snapshot)

    rain_hourly = rain_hourly_from_records(rain_rows)
    if risk_core.rain_window_count(rain_hourly, target_time, 24) < 12:
        logger.info("TimeMachine: insufficient rainfall data in 24h window, fetching archive")
        rain_rows = rain_rows + await _cache_through_rainfall(target_time)
        rain_hourly = rain_hourly_from_records(rain_rows)

    # === FEATURES (same builder as backfill) ===
    station_points = _snapshot_station_points(snapshot)
    primary_id = settings.hubeau_primary_station
    station_points[primary_id] = sorted(set(station_points.get(primary_id, [])) | set(primary_rows))
    configs = [risk_core.StationConfig.from_settings_dict(c) for c in settings.hubeau_stations]
    # query_tides rows carry aware timestamps (display code compares naive)
    tide_rows = [
        {**r, "timestamp": _to_naive(r["timestamp"])} for r in tide_rows if r.get("timestamp") is not None
    ] or _snapshot_tide_rows(snapshot)
    high_tides, coef_records = tide_inputs_from_records(tide_rows)
    tide_state = risk_core.tide_at(target_time, high_tides, coefficient_records=coef_records)

    features = risk_core.build_historical_features(
        target_time, configs, station_points, settings.hubeau_primary_station,
        rain_hourly, tide_state, max_age_hours=max_age, rate_window_h=rate_window,
    )
    risk = breakdown_to_risk_score(risk_core.compute_risk(features))
    risk.description = f"Donnees historiques du {target_time.strftime('%d/%m/%Y %H:%M')}"
    risk.recommendations = ["Donnees reconstituees depuis l'historique"]

    # === DISPLAY DATA ===
    feats_by_id = {f.station_id: f for f in features.stations}
    stations_list = []
    primary_level: Optional[RiverLevel] = None
    primary_tendency = Tendency.STABLE
    for station_cfg in settings.hubeau_stations:
        station_id = station_cfg["id"]
        points = station_points.get(station_id, [])
        feat = feats_by_id.get(station_id)
        last = risk_core.latest_level(points, target_time, max_age)
        if last is not None:
            height = last[1]
            level_ts = last[0]
            rate = feat.rate_cm_h if feat else risk_core.rate_cm_per_hour(points, target_time, rate_window)
            tendency = risk_core.tendency_from_rate(rate)
        else:
            height, level_ts, rate, tendency = 0.0, target_time, 0.0, Tendency.STABLE

        river_level = RiverLevel(
            timestamp=level_ts,
            station_id=station_id,
            station_name=station_cfg["name"],
            river=station_cfg["river"],
            height_m=height,
        )
        stations_list.append(StationLevel(
            station_id=station_id,
            station_name=station_cfg["name"],
            river=station_cfg["river"],
            current_level=river_level,
            tendency=tendency,
            # Unrounded (same value as the model); the UI rounds for display
            rate_cm_per_hour=rate,
            distance_km=station_cfg["distance_km"],
            delay_hours=station_cfg["delay_hours"],
            weight=station_cfg["weight"],
            threshold_yellow=station_cfg["thresholds"]["yellow"],
            threshold_orange=station_cfg["thresholds"]["orange"],
            threshold_red=station_cfg["thresholds"]["red"],
            lat=station_cfg.get("lat"),
            lon=station_cfg.get("lon"),
        ))
        if station_id == settings.hubeau_primary_station and last is not None:
            primary_level = river_level
            primary_tendency = tendency

    stations_data = MultiStationData(
        timestamp=target_time,
        stations=stations_list,
        primary_station_id=settings.hubeau_primary_station,
    )

    # Tide display (PM/BM events of the snapshot)
    tide_forecast = TideForecast(
        port=settings.tide_port_confluence, tides=[], current_coefficient=tide_state.coefficient,
    )
    for row in sorted(tide_rows, key=lambda r: r["timestamp"]):
        if row["height_m"] is None or row.get("tide_type") not in ("PM", "BM"):
            continue
        td = TideData(
            timestamp=row["timestamp"],
            port=row.get("port") or settings.tide_port_confluence,
            tide_type=TideType(row["tide_type"]),
            height_m=row["height_m"],
            coefficient=tide_state.coefficient,
        )
        tide_forecast.tides.append(td)
        if row["timestamp"] >= target_time:
            if td.tide_type == TideType.HIGH and not tide_forecast.next_high_tide:
                tide_forecast.next_high_tide = td
            elif td.tide_type == TideType.LOW and not tide_forecast.next_low_tide:
                tide_forecast.next_low_tide = td

    rainfall = RainfallSummary(
        last_hour_mm=round(risk_core.rain_window_sum(rain_hourly, target_time, 1), 1),
        last_6h_mm=round(risk_core.rain_window_sum(rain_hourly, target_time, 6), 1),
        last_24h_mm=round(features.rain_24h_mm, 1),
        last_72h_mm=round(features.rain_72h_mm, 1),
        last_7d_mm=round(features.rain_7d_mm or 0.0, 1),
    )

    if not primary_level:
        primary_cfg = next(
            (s for s in settings.hubeau_stations if s["id"] == settings.hubeau_primary_station),
            None,
        )
        primary_level = RiverLevel(
            timestamp=target_time,
            station_id=settings.hubeau_primary_station,
            station_name=primary_cfg["name"] if primary_cfg else settings.hubeau_primary_station,
            river=primary_cfg["river"] if primary_cfg else "Dordogne",
            height_m=0.0,
        )

    alerts = ["Mode TimeMachine - donnees historiques"]
    if not risk.factors.tide_available:
        alerts.append("Marée indisponible pour cette date : non prise en compte dans le score")

    return RealtimeData(
        timestamp=target_time,
        risk=risk,
        tide=tide_forecast,
        river=RiverLevelForecast(current=primary_level, tendency=primary_tendency),
        rainfall=rainfall,
        video=None,
        alerts=alerts,
        stations=stations_data,
        netatmo_available=False,
    )


@router.get("/radar")
async def get_radar_basin_rain():
    """Observed basin rain from the OPERA radar composite (EUMETNET, CC BY 4.0).

    Mean ACRR over the Lidoire polygon (pixels weighted by their share inside
    the basin) for the last 1 / 3 / 6 / 24 h, summed from the 1-hour
    accumulations ending at the last image time, plus the max pixel and the
    time of the last image. Read-only (the scheduler stores the images);
    informative, never read by the risk model.
    """
    from app.models.probabilistic import api_json
    from app.services.collectors.opera_radar import summarize_radar
    from app.services.probabilistic_store import query_radar_points

    settings = get_settings()
    now = _utcnow()
    if not settings.radar_enabled:
        return api_json({**summarize_radar([], now), "note": "desactive (RADAR_ENABLED=false)"})
    try:
        points = await query_radar_points(now - timedelta(hours=25), now + timedelta(minutes=1))
    except Exception as e:
        logger.warning("Radar basin rain query failed: %r", e)
        return api_json({**summarize_radar([], now), "note": "lecture InfluxDB impossible"})
    return api_json(summarize_radar(points, now))
