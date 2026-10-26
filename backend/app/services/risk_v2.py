"""Risk scoring service (model v3.4) - adapters around risk_core.

The score itself is computed ONLY by :func:`app.services.risk_core.compute_risk`.
This module:

- gathers and fuses realtime inputs (Hub'Eau stations, tides, rain from
  Open-Meteo / Netatmo / Infoclimat, soil, wind) -> :class:`RealtimeInputs`;
- builds :class:`RiskFeatures` from them (realtime, simulation) and converts
  a :class:`RiskBreakdown` into the API :class:`RiskScore` schema
  (description, recommendations);
- persists scores to InfluxDB (``store_risk_score``) - computing and storing
  are separate: nothing is written unless ``store=True`` is requested;
- recomputes historical scores (``backfill_risk_scores``) from stored series
  with no future data.

Model history (weights and mechanisms unchanged since v3.2, calibrated on
14 annotated episodes, 5 confirmed floods):

v3.3 (Oct 2026): bug-fix release, see risk_core docstring (single
implementation, orange-threshold fix, same denominator, no future leakage
in backfill, tide unavailable -> 0, Libourne never primary).

v3.2 (Jan 2026): multi-station above orange trigger (>= 3 stations), dampener
bypass, drainage impairment low-rain trigger.

v3.0 (Jan 2026): momentum factor, stability dampener, conjunction redesign.
Key insight: the moulin is on the Lidoire (tributary), NOT on the Dordogne.
High Dordogne level alone does NOT mean flood at the moulin.

v2.2 (Jan 2026): Libourne (P577001001, tidal) excluded (weight 0); primary
station for tendency: Pessac-sur-Dordogne (P555001001).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.config import get_settings
from app.models.schemas import (
    MultiStationData,
    RainfallSummary,
    RiskFactors,
    RiskLevel,
    RiskScore,
    RiskWeights,
    StationLevel,
    StationRiskScore,
    Tendency,
    TideForecast,
    TideType,
)
from app.services import risk_core
from app.services.risk_core import (
    MODEL_VERSION,
    RISING_RATE_CM_H,
    RiskBreakdown,
    RiskFeatures,
    StationConfig,
    StationFeature,
    compute_risk,
    to_utc_naive,
)

logger = logging.getLogger(__name__)

# Window around a target instant used to load tide records (backfill,
# TimeMachine) and to keep past tides in realtime (collectors/tide.py): the
# nearest high tide is at most ~6h12 away, 13h leaves a full cycle of margin.
TIDE_WINDOW_H = 13.0


def _utcnow() -> datetime:
    """Naive UTC now (module-level so tests can freeze time)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Backward-compatible helpers (used by validate_model.py / forecast callers)
# ---------------------------------------------------------------------------

def _normalize_score(
    value: float,
    thresholds: tuple[float, float, float],
    exponential: bool = True,
) -> float:
    """Normalize a value to 0-1 from (yellow, orange, red) thresholds."""
    return risk_core.normalize_score(value, thresholds, exponential)


def _score_to_level(score: float) -> RiskLevel:
    """Convert numerical score (0-100) to risk level."""
    return risk_core.score_to_level(score)


def _is_above_orange(s: Any) -> bool:
    """Above-orange test for a StationRiskScore (height >= orange threshold)."""
    above = getattr(s, "above_orange", None)
    if above is not None:
        return bool(above)
    linear = getattr(s, "linear_score", None)
    if linear is not None:
        return linear >= risk_core.LINEAR_ORANGE
    # Legacy objects only carry the post-curve score
    return s.score >= risk_core.apply_curve(risk_core.LINEAR_ORANGE)


def _count_rising(station_scores: list) -> int:
    return sum(
        1 for s in station_scores
        if s.weight > 0 and s.rate_cm_per_hour > RISING_RATE_CM_H
    )


def _calculate_tendency_score(stations_data: MultiStationData) -> float:
    """Tendency score from the primary station rate (never a zero-weight one)."""
    feats = [_station_feature(s) for s in stations_data.stations]
    primary = risk_core.select_primary_station(feats, stations_data.primary_station_id)
    return risk_core.tendency_score_from_rate(primary.rate_cm_h) if primary else 0.0


def _calculate_momentum_score(
    station_scores: list[StationRiskScore] | None = None,
    n_rising: int = 0,
    n_total: int = 6,
    river_score: float = 0.5,
) -> float:
    """Multi-station momentum score (see risk_core.momentum_score)."""
    if station_scores:
        active = [s for s in station_scores if s.weight > 0]
        n_total = len(active) if active else 6
        n_rising = _count_rising(station_scores)
    return risk_core.momentum_score(n_rising, river_score, n_total)


def _calculate_stability_dampener(
    river_score: float,
    tendency: Tendency,
    station_scores: list[StationRiskScore] | None = None,
    n_rising: int = 0,
    rain_72h_score: float = 0.0,
) -> float:
    """Stability dampener (see risk_core.stability_dampener)."""
    n_above_orange = 0
    if station_scores:
        n_rising = _count_rising(station_scores)
        n_above_orange = sum(1 for s in station_scores if s.weight > 0 and _is_above_orange(s))
    return risk_core.stability_dampener(
        river_score, tendency, n_rising, rain_72h_score,
        n_above_orange=n_above_orange, station_detail=bool(station_scores),
    )


def _calculate_conjunction_multiplier(
    river_score: float,
    tide_phase_score: float,
    tendency: Tendency,
    station_scores: list[StationRiskScore] | None = None,
    rain_72h_score: float = 0.0,
    rain_24h_score: float = 0.0,
    n_rising: int = 0,
    soil_saturation_factor: float = 1.0,
) -> float:
    """Conjunction multiplier (see risk_core.conjunction_multiplier)."""
    n_above_orange = 0
    if station_scores:
        n_rising = _count_rising(station_scores)
        n_above_orange = sum(1 for s in station_scores if s.weight > 0 and _is_above_orange(s))
    return risk_core.conjunction_multiplier(
        river_score=river_score,
        tide_phase=tide_phase_score,
        tendency=tendency,
        n_rising=n_rising,
        n_above_orange=n_above_orange,
        rain_24h_score=rain_24h_score,
        rain_72h_score=rain_72h_score,
        soil_saturation_factor=soil_saturation_factor,
        station_detail=bool(station_scores),
    )


# ---------------------------------------------------------------------------
# Breakdown -> API schema
# ---------------------------------------------------------------------------

def _generate_description(
    level: RiskLevel,
    factors: RiskFactors,
    primary_rate_cm_h: Optional[float],
) -> str:
    """Generate human-readable description of risk."""
    descriptions = {
        RiskLevel.GREEN: "Risque faible - conditions normales",
        RiskLevel.YELLOW: "Vigilance - conditions à surveiller",
        RiskLevel.ORANGE: "Risque modéré - conditions défavorables",
        RiskLevel.RED: "Risque élevé - conditions critiques",
    }

    desc = descriptions[level]

    high_factors = []
    if factors.rain_24h_score > 0.5:
        high_factors.append("pluie 24h")
    if factors.rain_72h_score > 0.5:
        high_factors.append("pluie 72h")
    if factors.rain_7d_score > 0.5:
        high_factors.append("pluie 7j")
    if factors.river_level_score > 0.5:
        high_factors.append("niveau rivières")
    if factors.tide_score > 0.5:
        high_factors.append("coefficient marée")
    if factors.tide_phase_score > 0.7:
        high_factors.append("pleine mer")
    if factors.tendency_score > 0.5:
        high_factors.append("montée rapide")
    if factors.momentum_score > 0.5:
        high_factors.append("hausse multi-stations")
    if factors.wind_west_score > 0.5:
        high_factors.append("vent d'ouest")
    if factors.soil_saturation_factor > 1.3:
        high_factors.append("sol saturé")
    if factors.conjunction_multiplier > 1.2:
        conj_parts = []
        if factors.tide_phase_score > 0.6:
            conj_parts.append("marée haute")
        if factors.river_level_score > 0.5:
            conj_parts.append("niveaux élevés")
        if factors.tendency_score > 0.3:
            conj_parts.append("montée")
        if factors.stations_detail:
            active = [s for s in factors.stations_detail if s.weight > 0]
            above_yellow = sum(
                1 for s in active
                if (s.linear_score if s.linear_score is not None else 0.0) >= risk_core.LINEAR_YELLOW
            )
            if active and above_yellow / len(active) >= 0.6:
                conj_parts.append(f"{above_yellow} stations élevées")
        if conj_parts:
            high_factors.append(f"conjonction: {' + '.join(conj_parts)}")
        else:
            high_factors.append("conjonction défavorable")

    if high_factors:
        desc += f" ({', '.join(high_factors)})"

    if primary_rate_cm_h is not None and primary_rate_cm_h > 5:
        desc += f" - Montée: {primary_rate_cm_h:.0f} cm/h"

    if not factors.tide_available:
        desc += " - marée indisponible/estimée (non prise en compte)"

    return desc


def _generate_recommendations(level: RiskLevel, factors: RiskFactors) -> list[str]:
    """Generate recommendations based on risk level and factors."""
    recommendations = []

    if level == RiskLevel.GREEN:
        recommendations.append("Aucune action nécessaire")
    elif level == RiskLevel.YELLOW:
        recommendations.append("Surveiller l'évolution des conditions")
        if factors.rain_24h_score > 0.4:
            recommendations.append("Vérifier les prévisions météo")
        if factors.tendency_score > 0.3:
            recommendations.append("Le niveau monte - surveiller régulièrement")
        if factors.soil_saturation_factor > 1.3:
            recommendations.append("Sol saturé - ruissellement accru")
    elif level == RiskLevel.ORANGE:
        recommendations.append("Préparer les mesures de protection")
        recommendations.append("Surveiller régulièrement le niveau d'eau")
        if factors.tide_phase_score > 0.6:
            recommendations.append("Attention à la prochaine pleine mer")
        if factors.conjunction_multiplier > 1.2:
            recommendations.append("Conjonction marée/niveau défavorable")
    else:  # RED
        recommendations.append("Mettre en place les protections")
        recommendations.append("Surveiller en continu")
        recommendations.append("Préparer l'évacuation des zones à risque")

    # Upstream warnings (above orange threshold, rising, zero-weight skipped)
    settings = get_settings()
    delays = {s["id"]: s.get("delay_hours", 0) for s in settings.hubeau_stations}
    for st in factors.stations_detail:
        if st.weight > 0 and st.above_orange and st.tendency == Tendency.RISING:
            delay = delays.get(st.station_id, 0)
            if delay > 0:
                recommendations.append(
                    f"Niveau élevé à {st.station_name} - impact prévu dans ~{delay:.0f}h"
                )

    return recommendations


def breakdown_to_factors(bd: RiskBreakdown) -> RiskFactors:
    """Convert a core breakdown to the API RiskFactors schema."""
    stations_detail = [
        StationRiskScore(
            station_id=d.station.station_id,
            station_name=d.station.station_name,
            river=d.station.river,
            height_m=d.station.height_m,
            score=d.score,
            weight=d.station.weight,
            weighted_score=d.weighted_score,
            tendency=d.station.display_tendency or risk_core.tendency_from_rate(d.station.rate_cm_h),
            rate_cm_per_hour=d.station.rate_cm_h,
            linear_score=d.linear_score,
            above_orange=d.above_orange,
        )
        for d in bd.stations
    ]
    return RiskFactors(
        rain_24h_score=bd.rain_24h_score,
        rain_72h_score=bd.rain_72h_score,
        rain_7d_score=bd.rain_7d_score,
        river_level_score=bd.river_score,
        tide_score=bd.tide_score,
        tide_phase_score=bd.tide_phase_score,
        wind_west_score=bd.wind_west_score,
        soil_saturation_factor=bd.soil_saturation_factor,
        video_level_score=bd.video_score,
        tendency_score=bd.tendency_score,
        momentum_score=bd.momentum_score,
        conjunction_multiplier=bd.conjunction_multiplier,
        stations_detail=stations_detail,
        weights=RiskWeights(**bd.weights),
        rain_24h_raw_mm=bd.rain_24h_effective_mm,
        rain_72h_raw_mm=bd.rain_72h_effective_mm,
        tide_coefficient_raw=bd.tide_coefficient,
        tide_hours_to_high=bd.hours_to_high_tide,
        tendency_rate_cm_h=bd.primary_rate_cm_h,
        stability_dampener=bd.stability_dampener,
        n_rising_stations=bd.n_rising,
        n_above_orange=bd.n_above_orange,
        tide_available=bd.tide_available,
        unavailable_factors=list(bd.unavailable),
        notes=list(bd.notes),
        tide_gate=bd.tide_gate,
        backwater_level_m=bd.backwater_level_m,
    )


def breakdown_to_risk_score(bd: RiskBreakdown) -> RiskScore:
    """Convert a core breakdown to the API RiskScore schema."""
    factors = breakdown_to_factors(bd)
    return RiskScore(
        timestamp=bd.timestamp,
        level=bd.level,
        score=bd.score,
        factors=factors,
        description=_generate_description(
            bd.level, factors, bd.primary_rate_cm_h if bd.primary_station_id else None,
        ),
        recommendations=_generate_recommendations(bd.level, factors),
        model_version=bd.model_version,
    )


# ---------------------------------------------------------------------------
# Realtime features
# ---------------------------------------------------------------------------

_ESTIMATED_TIDE_SOURCES = {"mock", "calculated", "estimated", "fallback", "synthetic"}


def tide_forecast_is_reliable(tide: Optional[TideForecast]) -> bool:
    """False when tide data is missing, flagged unavailable or estimated.

    Reads optional flags a collector may set on TideForecast (``available``,
    ``is_estimated`` / ``estimated``, ``source`` / ``status``) without
    requiring them.
    """
    if tide is None:
        return False
    if getattr(tide, "available", True) is False:
        return False
    if getattr(tide, "is_estimated", False) or getattr(tide, "estimated", False):
        return False
    for attr in ("source", "status"):
        val = getattr(tide, attr, None)
        if isinstance(val, str) and val.lower() in _ESTIMATED_TIDE_SOURCES:
            return False
    if not tide.tides and tide.next_high_tide is None:
        return False
    return True


def tide_high_list(tide: Optional[TideForecast]) -> list[tuple[datetime, Optional[int]]]:
    """High tides (timestamp naive UTC, coefficient) from a TideForecast.

    Includes the recent PAST tides (``recent_tides``): the tide phase is the
    distance to the nearest high tide, which is often the one that just
    passed (same rule as the backfill and the TimeMachine).
    """
    if tide is None:
        return []
    highs: dict[datetime, Optional[int]] = {}
    items = list(getattr(tide, "recent_tides", None) or []) + list(tide.tides)
    if tide.next_high_tide is not None:
        items.append(tide.next_high_tide)
    for t in items:
        if t.tide_type == TideType.HIGH:
            ts = to_utc_naive(t.timestamp)
            highs[ts] = t.coefficient or highs.get(ts)
    return sorted(highs.items())


def tide_state_from_forecast(tide: Optional[TideForecast], t: datetime) -> risk_core.TideState:
    """Tide state at ``t`` from a TideForecast (same rule for realtime and forecast)."""
    state = risk_core.tide_at(
        t,
        tide_high_list(tide),
        fallback_coefficient=tide.current_coefficient if tide else None,
    )
    if not tide_forecast_is_reliable(tide):
        return risk_core.TideState(False, state.coefficient, state.hours_to_high_tide)
    return state


def _station_feature(s: StationLevel) -> StationFeature:
    return StationFeature(
        station_id=s.station_id,
        station_name=s.station_name,
        river=s.river,
        height_m=s.current_level.height_m,
        rate_cm_h=s.rate_cm_per_hour,
        weight=s.weight,
        thresholds=(s.threshold_yellow, s.threshold_orange, s.threshold_red),
        display_tendency=s.tendency,
    )


def station_features_from_multi(
    stations_data: Optional[MultiStationData],
    t: datetime,
    max_age_hours: Optional[float] = None,
) -> tuple[StationFeature, ...]:
    """Station features from realtime Hub'Eau data, dropping stale levels."""
    if stations_data is None:
        return ()
    if max_age_hours is None:
        max_age_hours = get_settings().risk_level_max_age_hours
    out = []
    for s in stations_data.stations:
        ts = to_utc_naive(s.current_level.timestamp)
        if (t - ts).total_seconds() > max_age_hours * 3600:
            logger.info("risk: station %s ignored, last level at %s is stale", s.station_id, ts)
            continue
        out.append(_station_feature(s))
    return tuple(out)


def primary_level_points(
    stations_data: Optional[MultiStationData],
    primary_levels=(),
) -> list[risk_core.LevelPoint]:
    """Primary-station series: stored points plus the latest Hub'Eau level (sorted)."""
    points = risk_core.as_level_points(primary_levels)
    primary_id = stations_data.primary_station_id if stations_data else get_settings().hubeau_primary_station
    current = next(
        (s for s in (stations_data.stations if stations_data else []) if s.station_id == primary_id), None,
    )
    if current is not None:
        latest = (to_utc_naive(current.current_level.timestamp), float(current.current_level.height_m))
        if latest not in points:
            points = sorted(points + [latest])
    return points


def build_realtime_features(
    stations_data: Optional[MultiStationData],
    tide: Optional[TideForecast],
    rainfall: RainfallSummary,
    timestamp: datetime,
    westerly_wind_kmh: Optional[float] = 0.0,
    soil_saturation_factor: Optional[float] = 1.0,
    video_level: Optional[float] = None,
    primary_levels=(),
) -> RiskFeatures:
    """Features for 'now' from realtime collector outputs (pure).

    ``primary_levels``: stored primary-station (Pessac) points of the last
    BACKWATER_WINDOW_H, merged with the latest Hub'Eau level for the
    backwater level that gates the tide factors (v3.4).
    """
    t = to_utc_naive(timestamp)
    tide_state = tide_state_from_forecast(tide, t)
    primary_id = (
        stations_data.primary_station_id if stations_data
        else get_settings().hubeau_primary_station
    )
    return RiskFeatures(
        timestamp=t,
        stations=station_features_from_multi(stations_data, t),
        primary_station_id=primary_id,
        rain_24h_mm=rainfall.last_24h_mm,
        rain_72h_mm=rainfall.last_72h_mm,
        rain_7d_mm=rainfall.last_7d_mm,
        rain_forecast_24h_mm=rainfall.forecast_24h_mm,
        rain_forecast_72h_mm=rainfall.forecast_72h_mm,
        tide_available=tide_state.available,
        tide_coefficient=tide_state.coefficient,
        hours_to_high_tide=tide_state.hours_to_high_tide,
        backwater_level_m=risk_core.backwater_level(primary_level_points(stations_data, primary_levels), t),
        westerly_wind_kmh=westerly_wind_kmh,
        soil_saturation_factor=soil_saturation_factor,
        video_level=video_level,
    )


def fuse_rainfall(
    rainfall: RainfallSummary,
    netatmo: Optional[dict] = None,
    infoclimat: Optional[dict] = None,
) -> RainfallSummary:
    """Fuse Open-Meteo rain with Netatmo / Infoclimat gauges (max policy).

    Gauges only report 1h/24h; cumulative windows are kept monotonic
    (72h >= 24h, 7d >= 72h) so a gauge max on 24h is not lost on 72h.
    """
    last_1h = rainfall.last_hour_mm
    last_24h = rainfall.last_24h_mm
    for src in (netatmo, infoclimat):
        if src and src.get("available"):
            last_1h = max(last_1h, src.get("rain_1h_avg_mm") or 0.0)
            last_24h = max(last_24h, src.get("rain_24h_avg_mm") or 0.0)
    last_72h = max(rainfall.last_72h_mm, last_24h)
    last_7d = max(rainfall.last_7d_mm, last_72h)
    return rainfall.model_copy(update={
        "last_hour_mm": last_1h,
        "last_24h_mm": last_24h,
        "last_72h_mm": last_72h,
        "last_7d_mm": last_7d,
    })


def compute_soil_saturation_factor(
    base_factor: float,
    soil_wind: Optional[dict],
    meteo_agricole: Any = None,
) -> float:
    """Soil saturation factor (1.0-1.5) from all soil sources."""
    factor = base_factor
    soil_wind = soil_wind or {}

    moisture_deep = soil_wind.get("moisture_deep", 0)
    if moisture_deep and moisture_deep > 0.30:
        factor = min(1.5, factor + 0.15)
    elif moisture_deep and moisture_deep > 0.25:
        factor = min(1.5, factor + 0.05)

    et0 = soil_wind.get("et0_mm")
    if et0 is not None and et0 < 1.0:
        factor = min(1.5, factor + 0.05)

    if meteo_agricole is not None:
        surplus = getattr(meteo_agricole, "surplus_deficit_30d_pct", None)
        if surplus is not None:
            if surplus > 20:
                factor = min(1.5, factor + 0.1)
            elif surplus < -20:
                factor = max(1.0, factor - 0.1)

    return min(1.5, max(1.0, factor))


def compute_westerly_wind(soil_wind: Optional[dict], meteo_agricole: Any = None) -> float:
    """Westerly wind (km/h): max of Open-Meteo westerly and 0.7 x Meteo Agricole gust."""
    westerly = (soil_wind or {}).get("westerly_wind_kmh", 0.0) or 0.0
    gust = getattr(meteo_agricole, "wind_gust_kmh", 0) if meteo_agricole is not None else 0
    if gust and gust > 0:
        westerly = max(westerly, gust * 0.7)
    return westerly


@dataclass
class RealtimeInputs:
    """Raw and fused realtime inputs shared by /realtime, /risk, scheduler and forecast."""

    timestamp: datetime
    tide: Optional[TideForecast]
    rainfall: RainfallSummary  # fused
    rainfall_raw: RainfallSummary
    stations: Optional[MultiStationData]
    soil_wind: dict = field(default_factory=dict)
    meteo_agricole: Any = None
    netatmo: dict = field(default_factory=dict)
    infoclimat: dict = field(default_factory=dict)
    soil_saturation_factor: float = 1.0
    westerly_wind_kmh: float = 0.0
    video_level: Optional[float] = None
    # Primary station (Pessac) points of the last BACKWATER_WINDOW_H, stored
    # + latest Hub'Eau (tide gate input, shared with the forecast)
    primary_levels: tuple = ()

    def features(self) -> RiskFeatures:
        return build_realtime_features(
            self.stations, self.tide, self.rainfall, self.timestamp,
            westerly_wind_kmh=self.westerly_wind_kmh,
            soil_saturation_factor=self.soil_saturation_factor,
            video_level=self.video_level,
            primary_levels=self.primary_levels,
        )


async def _safe(coro, default, name: str):
    try:
        return await coro
    except Exception as e:  # optional source: degrade gracefully
        logger.warning("risk inputs: %s unavailable: %s", name, e)
        return default


async def load_primary_levels(now: datetime) -> list[risk_core.LevelPoint]:
    """Stored primary-station points over the backwater window ending at ``now``."""
    from app.services import influx_client

    rows = await influx_client.query_river_levels(
        get_settings().hubeau_primary_station,
        now - timedelta(hours=risk_core.BACKWATER_WINDOW_H),
        now + timedelta(minutes=1),
    )
    return _level_points(rows)


async def gather_realtime_inputs(now: Optional[datetime] = None) -> RealtimeInputs:
    """Fetch every realtime source in parallel and fuse them (I/O)."""
    from app.services.collectors.hubeau import get_multi_station_data
    from app.services.collectors.infoclimat import get_basin_rainfall_infoclimat
    from app.services.collectors.meteo_agricole import get_meteo_agricole_cached
    from app.services.collectors.netatmo import get_basin_rainfall
    from app.services.collectors.tide import get_current_tide_data
    from app.services.collectors.weather import get_rainfall_summary, get_soil_wind_cached

    now = now or _utcnow()

    (
        tide, rainfall, stations, soil_wind, meteo_agricole, netatmo, infoclimat, stored_primary,
    ) = await asyncio.gather(
        get_current_tide_data(),
        get_rainfall_summary(),
        get_multi_station_data(),
        _safe(get_soil_wind_cached(), {}, "soil/wind"),
        _safe(get_meteo_agricole_cached(), None, "meteo agricole"),
        _safe(get_basin_rainfall(), {}, "netatmo"),
        _safe(get_basin_rainfall_infoclimat(), {}, "infoclimat"),
        _safe(load_primary_levels(now), [], "stored primary levels"),
    )
    soil_wind = soil_wind or {}
    netatmo = netatmo or {}
    infoclimat = infoclimat or {}

    fused = fuse_rainfall(rainfall, netatmo, infoclimat)
    return RealtimeInputs(
        timestamp=now,
        tide=tide,
        rainfall=fused,
        rainfall_raw=rainfall,
        stations=stations,
        soil_wind=soil_wind,
        meteo_agricole=meteo_agricole,
        netatmo=netatmo,
        infoclimat=infoclimat,
        soil_saturation_factor=compute_soil_saturation_factor(
            rainfall.soil_saturation_factor, soil_wind, meteo_agricole,
        ),
        westerly_wind_kmh=compute_westerly_wind(soil_wind, meteo_agricole),
        primary_levels=tuple(primary_level_points(stations, stored_primary or [])),
    )


def risk_from_inputs(inputs: RealtimeInputs) -> RiskScore:
    """Score realtime inputs (pure)."""
    return breakdown_to_risk_score(compute_risk(inputs.features()))


async def compute_current_risk(store: bool = False) -> tuple[RiskScore, RealtimeInputs]:
    """Gather realtime inputs, compute the risk and optionally persist it."""
    inputs = await gather_realtime_inputs()
    risk = risk_from_inputs(inputs)
    if store:
        await store_risk_score(risk)
    return risk, inputs


async def calculate_risk_score_v2(
    stations_data: MultiStationData,
    tide: TideForecast,
    rainfall: RainfallSummary,
    video_level: Optional[float] = None,
    westerly_wind_kmh: float = 0.0,
    soil_saturation_factor: float = 1.0,
    *,
    timestamp: Optional[datetime] = None,
    store: bool = False,
) -> RiskScore:
    """Compute the risk score from realtime-shaped inputs.

    Nothing is written unless ``store=True``.
    """
    features = build_realtime_features(
        stations_data, tide, rainfall, timestamp or _utcnow(),
        westerly_wind_kmh=westerly_wind_kmh,
        soil_saturation_factor=soil_saturation_factor,
        video_level=video_level,
    )
    risk = breakdown_to_risk_score(compute_risk(features))
    if store:
        await store_risk_score(risk)
    return risk


def build_simulation_features(
    rain_24h_mm: float,
    rain_72h_mm: float,
    dordogne_level_m: float,
    tide_coefficient: Optional[int],
    hours_to_high_tide: Optional[float],
    rate_cm_per_hour: float = 0.0,
    video_level: Optional[float] = None,
    timestamp: Optional[datetime] = None,
    n_rising_stations: int = 0,
    rain_7d_mm: Optional[float] = None,
    westerly_wind_kmh: Optional[float] = None,
    soil_saturation_factor: Optional[float] = None,
) -> RiskFeatures:
    """Features from explicit values: one aggregate Dordogne (Pessac) level."""
    settings = get_settings()
    params = risk_core.get_model_params()
    primary_id = settings.hubeau_primary_station
    station = StationFeature(
        station_id=primary_id,
        station_name="Dordogne (simulation)",
        river="Dordogne",
        height_m=dordogne_level_m,
        rate_cm_h=rate_cm_per_hour,
        weight=1.0,
        thresholds=params.dordogne_thresholds,
    )
    return RiskFeatures(
        timestamp=to_utc_naive(timestamp) if timestamp else _utcnow(),
        stations=(station,),
        primary_station_id=primary_id,
        rain_24h_mm=rain_24h_mm,
        rain_72h_mm=rain_72h_mm,
        rain_7d_mm=rain_7d_mm,
        tide_available=True,
        tide_coefficient=tide_coefficient,
        hours_to_high_tide=hours_to_high_tide,
        westerly_wind_kmh=westerly_wind_kmh,
        soil_saturation_factor=soil_saturation_factor,
        video_level=video_level,
        n_rising_override=n_rising_stations,
        aggregate_only=True,
    )


async def calculate_risk_from_values(
    rain_24h_mm: float,
    rain_72h_mm: float,
    dordogne_level_m: float,
    tide_coefficient: int,
    hours_to_high_tide: float,
    rate_cm_per_hour: float = 0.0,
    video_level: Optional[float] = None,
    override_timestamp: Optional[datetime] = None,
    n_rising_stations: int = 0,
    n_total_stations: int = 6,
    rain_7d_mm: Optional[float] = None,
    westerly_wind_kmh: Optional[float] = None,
    soil_saturation_factor: Optional[float] = None,
) -> RiskScore:
    """Risk from explicit values (simulation). Never stores.

    ``n_total_stations`` is kept for backward compatibility (unused).
    """
    features = build_simulation_features(
        rain_24h_mm=rain_24h_mm,
        rain_72h_mm=rain_72h_mm,
        dordogne_level_m=dordogne_level_m,
        tide_coefficient=tide_coefficient,
        hours_to_high_tide=hours_to_high_tide,
        rate_cm_per_hour=rate_cm_per_hour,
        video_level=video_level,
        timestamp=override_timestamp,
        n_rising_stations=n_rising_stations,
        rain_7d_mm=rain_7d_mm,
        westerly_wind_kmh=westerly_wind_kmh,
        soil_saturation_factor=soil_saturation_factor,
    )
    return breakdown_to_risk_score(compute_risk(features))


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def risk_factors_for_storage(risk: RiskScore) -> dict[str, float]:
    """Numeric factor fields stored with each risk point (auditability)."""
    f = risk.factors
    out = {
        "rain_24h": f.rain_24h_score,
        "rain_72h": f.rain_72h_score,
        "rain_7d": f.rain_7d_score,
        "river_level": f.river_level_score,
        "tide": f.tide_score,
        "tide_phase": f.tide_phase_score,
        "tendency": f.tendency_score or 0.0,
        "momentum": f.momentum_score,
        "wind_west": f.wind_west_score,
        "soil_sat": risk_core.soil_sat_score(f.soil_saturation_factor),
        "conjunction": f.conjunction_multiplier,
        "dampener": f.stability_dampener,
        "tide_available": 1.0 if f.tide_available else 0.0,
        "rain_24h_mm": f.rain_24h_raw_mm or 0.0,
        "rain_72h_mm": f.rain_72h_raw_mm or 0.0,
        "rate_cm_h": f.tendency_rate_cm_h or 0.0,
    }
    if f.n_rising_stations is not None:
        out["n_rising"] = float(f.n_rising_stations)
    if f.n_above_orange is not None:
        out["n_above_orange"] = float(f.n_above_orange)
    if f.video_level_score is not None:
        out["video"] = f.video_level_score
    return out


def risk_point(risk: RiskScore, source: str):
    """InfluxDB point of a risk score (tags model_version + source, level field)."""
    from app.services import influx_client

    return influx_client.make_risk_point(
        score=risk.score,
        level=risk.level.value,
        timestamp=risk.timestamp,
        factors=risk_factors_for_storage(risk),
        model_version=risk.model_version or MODEL_VERSION,
        source=source,
    )


async def store_risk_score(risk: RiskScore, source: Optional[str] = None) -> bool:
    """Persist a risk score to InfluxDB (tags model_version + source, level as field).

    ``source`` defaults to "realtime": the hourly scheduler job is the only
    caller that stores live scores; the backfill writes "backfill" points in
    one batch (see :func:`backfill_risk_scores`).
    """
    try:
        from app.services import influx_client

        return await influx_client.write_risk_score(
            score=risk.score,
            level=risk.level.value,
            timestamp=risk.timestamp,
            factors=risk_factors_for_storage(risk),
            model_version=risk.model_version or MODEL_VERSION,
            source=source or influx_client.RISK_SOURCE_REALTIME,
        )
    except Exception as e:
        logger.error("Error storing risk score to InfluxDB: %s", e)
        return False


# Kept for callers of the old private name
_store_risk_to_influx = store_risk_score


# ---------------------------------------------------------------------------
# Historical backfill
# ---------------------------------------------------------------------------

def _level_points(rows: list[dict]) -> list[risk_core.LevelPoint]:
    pts = [
        (to_utc_naive(r["timestamp"]), float(r["height_m"]))
        for r in rows
        if r.get("height_m") is not None and r.get("timestamp") is not None
    ]
    pts.sort()
    return pts


def rain_hourly_from_records(rows: list[dict]) -> dict[datetime, float]:
    """Hourly rain from influx_client.query_rainfall() rows."""
    return risk_core.aggregate_hourly_rain(
        (r["timestamp"], r.get("precipitation_mm_h"), f"{r.get('source')}:{r.get('city')}")
        for r in rows
        if r.get("precipitation_mm_h") is not None and r.get("timestamp") is not None
    )


def tide_inputs_from_records(rows: list[dict]) -> tuple[list[tuple[datetime, Optional[int]]], list[tuple[datetime, int]]]:
    """(high tides, coefficient records) from influx_client.query_tides() rows.

    High tides come from PM-tagged heights when present, else from local
    maxima of the height series. Each high tide carries the coefficient
    stored for it (a coefficient record within 1h), so ``tide_at`` picks the
    coefficient of the latest past high tide, exactly like the realtime
    TideForecast (where coefficients are attached to the PM events).
    """
    heights = sorted(
        (to_utc_naive(r["timestamp"]), float(r["height_m"]))
        for r in rows if r.get("height_m") is not None
    )
    coefs = sorted(
        (to_utc_naive(r["timestamp"]), int(r["coefficient"]))
        for r in rows if r.get("coefficient") is not None
    )
    tagged = sorted({
        to_utc_naive(r["timestamp"]) for r in rows
        if r.get("height_m") is not None and r.get("tide_type") == TideType.HIGH.value
    })
    high_times = tagged or risk_core.detect_high_tides(heights)
    return [(ts, _coefficient_near(ts, coefs)) for ts in high_times], coefs


def _coefficient_near(ts: datetime, coefs: list[tuple[datetime, int]], max_gap_h: float = 1.0) -> Optional[int]:
    if not coefs:
        return None
    c_ts, c = min(coefs, key=lambda x: abs((x[0] - ts).total_seconds()))
    return c if abs((c_ts - ts).total_seconds()) <= max_gap_h * 3600 else None


async def load_tide_rows(start: datetime, end: datetime) -> list[dict]:
    """Stored tide rows for [start, end] at the Lidoire confluence.

    Shared by the backfill and the TimeMachine so both see the same tides.
    Only the confluence port is read: the legacy BORDEAUX / LIBOURNE points
    carry the source times without the phase lag and must not be mixed in.
    """
    from app.services import influx_client

    rows = await influx_client.query_tides(get_settings().tide_port_confluence, start, end)
    return rows if any(r.get("height_m") is not None for r in rows) else []


def _ceil_hour(dt: datetime) -> datetime:
    floor = dt.replace(minute=0, second=0, microsecond=0)
    return floor if floor == dt else floor + timedelta(hours=1)


async def _hours_with_live_scores(start: datetime, end: datetime) -> set[datetime]:
    """Hours of [start, end] that already hold a non-backfill score of this model."""
    from app.services import influx_client

    rows = await influx_client.query_risk_scores(start, end + timedelta(hours=1), MODEL_VERSION)
    return {
        to_utc_naive(r["timestamp"]).replace(minute=0, second=0, microsecond=0)
        for r in rows
        if r.get("source") != influx_client.RISK_SOURCE_BACKFILL
        and r.get("model_version") == MODEL_VERSION
    }


async def backfill_risk_scores(
    start: datetime,
    end: datetime,
    store: bool = True,
    skip_live_hours: bool = False,
) -> int:
    """Recompute (and store) risk scores from historical data in InfluxDB.

    Each step only uses observed points at or before the step time (level
    max age and rate window from settings). Resolution adapts to the period:
    hourly up to 7 days, 3-hourly up to 30 days, 6-hourly beyond. Steps start
    on a full hour so re-runs overwrite the same points.

    Stored points are tagged ``source=backfill`` (one batched write) and
    never replace the realtime series: readers prefer the realtime point for
    an hour that has both (``influx_client.prefer_realtime_risk_rows``). With
    ``skip_live_hours`` (anonymous timeline cache-through), hours that
    already hold a realtime score of the current model are not recomputed.

    Returns the number of scores computed (stored when ``store=True``).
    """
    from app.services import influx_client

    settings = get_settings()
    s = to_utc_naive(start)
    e = to_utc_naive(end)

    period_days = (e - s).days
    if period_days <= 7:
        step = timedelta(hours=1)
    elif period_days <= 30:
        step = timedelta(hours=3)
    else:
        step = timedelta(hours=6)

    max_age = settings.risk_level_max_age_hours
    rate_window = settings.risk_rate_window_hours
    lookback = timedelta(hours=max(max_age, rate_window, risk_core.BACKWATER_WINDOW_H) + 1)

    logger.info("Risk backfill v%s: loading data for %s -> %s (step=%s)", MODEL_VERSION, s, e, step)

    configs = [
        StationConfig.from_settings_dict(c) for c in settings.hubeau_stations
        if c.get("weight", 0) > 0
    ]
    station_points: dict[str, list[risk_core.LevelPoint]] = {}
    for cfg in configs:
        rows = await influx_client.query_river_levels(cfg.station_id, s - lookback, e + timedelta(hours=1))
        pts = _level_points(rows)
        if pts:
            station_points[cfg.station_id] = pts

    if not station_points:
        logger.warning("Risk backfill: no river data for any station, cannot compute scores")
        return 0

    rain_rows = await influx_client.query_rainfall(s - timedelta(hours=169), e + timedelta(hours=1))
    rain_hourly = rain_hourly_from_records(rain_rows)

    tide_window = timedelta(hours=TIDE_WINDOW_H)
    tide_rows = await load_tide_rows(s - tide_window, e + tide_window)
    high_tides, coef_records = tide_inputs_from_records(tide_rows)

    live_hours: set[datetime] = set()
    if skip_live_hours:
        try:
            live_hours = await _hours_with_live_scores(s, e)
        except Exception as exc:  # degrade to a plain backfill
            logger.warning("Risk backfill: cannot read existing scores: %s", exc)

    logger.info(
        "Risk backfill: %d stations, %d rain hours, %d high tides",
        len(station_points), len(rain_hourly), len(high_tides),
    )

    computed = 0
    skipped = 0
    points = []
    current = _ceil_hour(s)
    while current <= e:
        if current in live_hours:
            skipped += 1
            current += step
            continue
        tide_state = risk_core.tide_at(current, high_tides, coefficient_records=coef_records)
        features = risk_core.build_historical_features(
            current, configs, station_points, settings.hubeau_primary_station,
            rain_hourly, tide_state, max_age_hours=max_age, rate_window_h=rate_window,
        )
        if features.stations:
            risk = breakdown_to_risk_score(compute_risk(features))
            computed += 1
            if store:
                points.append(risk_point(risk, influx_client.RISK_SOURCE_BACKFILL))
        current += step

    stored = await influx_client.write_points_batch(points) if points else 0
    logger.info(
        "Risk backfill v%s: computed %d, stored %d scores (%d hours kept from realtime)",
        MODEL_VERSION, computed, stored, skipped,
    )
    return stored if store else computed
