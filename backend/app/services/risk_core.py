"""Pure flood risk scoring core (model v3.4).

This module is the ONE implementation of the risk score. It has no I/O:
no HTTP, no database, no ``utcnow()``. Every caller (realtime dashboard,
historical backfill, TimeMachine replay, 7-day forecast, /api/risk/simulate)
only BUILDS a :class:`RiskFeatures` and calls :func:`compute_risk`.

v3.3 is a bug-fix release of the v3.2 calibration (same weights, same
mechanisms, see CLAUDE.md):

- "Above orange" station counts use the station height against its own
  orange threshold (linear score >= 0.66), not the post-curve score > 0.6
  (which was ~3.24 m at Pessac instead of 3.8 m).
- Same normalisation denominator everywhere: sum of the weights of the
  factors whose input is available (video, rain 7d, wind, soil may be
  missing and are then excluded). Tide stays in the denominator: when tide
  data is unavailable or estimated, both tide factors contribute 0 and the
  breakdown says so.
- Same tendency definition everywhere: rate (cm/h) of the primary station
  (Pessac). The primary-station fallback never picks a zero-weight station
  (Libourne, tidal).
- Soil saturation enters only through ``soil_saturation_factor`` (rain score
  multiplier + soil weight + conjunction branch, exactly as realtime v3.2);
  builders must NOT pre-multiply rainfall millimetres.
- Single tide-phase table.

v3.4 = v3.3 + correct tide phase + tide factors gated by the Dordogne level:

- Tide times are estimated at the Lidoire confluence (source time + phase
  lag, applied by the tide collector; no change here).
- The ``tide`` and ``tide_phase`` contributions are multiplied by
  :func:`tide_gate` of the backwater level H (max of the primary station,
  Pessac, over the trailing 12.5 h): 0 at or below its yellow threshold,
  1 from its orange threshold, linear in between. The tide only hinders
  the drainage when the Dordogne is already high. The conjunction branch
  "high water + river > 0.5" keeps the ungated phase (as evaluated).

The pure feature-building helpers at the bottom (rate regression, latest
level, hourly rain buckets, tide lookup, historical features) only use
points at or before ``t`` for observed series (no future leakage).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Iterable, Mapping, Optional, Sequence

from app.models.schemas import RiskLevel, Tendency

MODEL_VERSION = "3.4"

# Trailing window (h) of the backwater level H that gates the tide factors
BACKWATER_WINDOW_H = 12.5

# A station is "rising" above this rate (cm/h). Shared by momentum,
# conjunction, dampener and the tendency enum.
RISING_RATE_CM_H = 1.0

# Exponent of the power curve applied to linear threshold scores.
SCORE_CURVE_EXPONENT = 0.6

# Linear score at which a value reaches its orange / yellow threshold.
LINEAR_YELLOW = 0.33
LINEAR_ORANGE = 0.66

WEIGHT_KEYS = (
    "river", "rain_24h", "rain_72h", "rain_7d", "tide", "tide_phase",
    "tendency", "momentum", "wind_west", "soil_sat", "video",
)


# ---------------------------------------------------------------------------
# Parameters (read from config once)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelParams:
    """All tunable parameters of the model, read once from settings."""

    weights: Mapping[str, float]
    rain_24h_thresholds: tuple[float, float, float]
    rain_72h_thresholds: tuple[float, float, float]
    rain_7d_thresholds: tuple[float, float, float]
    tide_coef_thresholds: tuple[float, float, float]
    default_tide_coefficient: int
    level_thresholds: tuple[float, float, float]  # yellow, orange, red (0-100)
    dordogne_thresholds: tuple[float, float, float]
    # Share of the forecast rain added to observed rain (anticipation)
    forecast_rain_24h_blend: float = 0.5
    forecast_rain_72h_blend: float = 0.3
    # Backwater level (m) at which the tide gate opens / is fully open:
    # yellow / orange thresholds of the primary station (Pessac)
    tide_gate_thresholds: tuple[float, float] = (3.0, 3.8)


def params_from_settings(settings) -> ModelParams:
    """Build model parameters from an app Settings object."""
    weights = dict(settings.risk_weights)
    missing = [k for k in WEIGHT_KEYS if k not in weights]
    if missing:
        raise ValueError(f"risk_weights is missing keys: {missing}")
    primary = next(
        (c for c in settings.hubeau_stations if c.get("id") == settings.hubeau_primary_station), None,
    )
    if primary is None:
        raise ValueError(f"primary station {settings.hubeau_primary_station!r} is not configured")
    primary_th = primary.get("thresholds", {})
    return ModelParams(
        weights=weights,
        rain_24h_thresholds=(
            settings.risk_rain_24h_yellow,
            settings.risk_rain_24h_orange,
            settings.risk_rain_24h_red,
        ),
        rain_72h_thresholds=(
            settings.risk_rain_24h_yellow * 2,
            settings.risk_rain_24h_orange * 2,
            settings.risk_rain_24h_red * 2,
        ),
        rain_7d_thresholds=(
            settings.risk_rain_7d_yellow,
            settings.risk_rain_7d_orange,
            settings.risk_rain_7d_red,
        ),
        tide_coef_thresholds=(
            settings.risk_tide_coef_low,
            settings.risk_tide_coef_high,
            settings.risk_tide_coef_max,
        ),
        default_tide_coefficient=settings.risk_tide_default_coef,
        level_thresholds=(
            settings.risk_level_yellow,
            settings.risk_level_orange,
            settings.risk_level_red,
        ),
        dordogne_thresholds=(
            settings.risk_dordogne_level_yellow,
            settings.risk_dordogne_level_orange,
            settings.risk_dordogne_level_red,
        ),
        tide_gate_thresholds=(float(primary_th["yellow"]), float(primary_th["orange"])),
    )


@lru_cache
def get_model_params() -> ModelParams:
    """Model parameters from the cached app settings (read once)."""
    from app.config import get_settings

    return params_from_settings(get_settings())


# ---------------------------------------------------------------------------
# Elementary scoring functions
# ---------------------------------------------------------------------------

def normalize_linear(value: float, thresholds: Sequence[float]) -> float:
    """Piecewise-linear 0-1 score from (yellow, orange, red) thresholds.

    0 -> 0, yellow -> 0.33, orange -> 0.66, red and above -> 1.0.
    """
    yellow, orange, red = thresholds
    if value is None or value <= 0:
        return 0.0
    if value < yellow:
        return (value / yellow) * LINEAR_YELLOW
    if value < orange:
        return LINEAR_YELLOW + ((value - yellow) / (orange - yellow)) * 0.33
    if value < red:
        return LINEAR_ORANGE + ((value - orange) / (red - orange)) * 0.34
    return 1.0


def apply_curve(linear: float) -> float:
    """Power curve (x^0.6): steeper increase near critical thresholds."""
    if linear <= 0:
        return 0.0
    return linear ** SCORE_CURVE_EXPONENT


def normalize_score(
    value: float,
    thresholds: Sequence[float],
    exponential: bool = True,
) -> float:
    """Normalize a value to 0-1 (linear score, optionally with power curve).

    e.g. linear 0.33 -> 0.51, 0.5 -> 0.66, 0.8 -> 0.87 with the curve.
    """
    linear = normalize_linear(value, thresholds)
    return apply_curve(linear) if exponential else linear


def tendency_from_rate(rate_cm_h: float) -> Tendency:
    """Tendency enum from a rate of change (cm/h)."""
    if rate_cm_h > RISING_RATE_CM_H:
        return Tendency.RISING
    if rate_cm_h < -RISING_RATE_CM_H:
        return Tendency.FALLING
    return Tendency.STABLE


def tendency_score_from_rate(rate_cm_h: float) -> float:
    """Risk score from the primary-station rate of rise (v2.5 table)."""
    if rate_cm_h <= 0:
        return 0.0
    if rate_cm_h < 2:
        return 0.3
    if rate_cm_h < 5:
        return 0.5
    if rate_cm_h < 10:
        return 0.7
    if rate_cm_h < 20:
        return 0.85
    return 1.0


def tide_gate(level_m: Optional[float], thresholds: Sequence[float]) -> float:
    """Tide gate (0-1) from the backwater level: 0 at <= yellow, 1 at >= orange.

    ``None`` (no primary station level at all) leaves the tide factors
    ungated (1.0), as in v3.3.
    """
    if level_m is None:
        return 1.0
    low, full = thresholds[0], thresholds[1]
    if full <= low:
        return 1.0 if level_m >= full else 0.0
    return min(1.0, max(0.0, (level_m - low) / (full - low)))


def tide_phase_score(hours_to_nearest_high: Optional[float]) -> float:
    """Risk from the proximity of high tide (single tide-phase table).

    ``hours_to_nearest_high`` is signed (negative = high tide already passed);
    only its absolute value matters. None (no tide timing) -> 0.
    """
    if hours_to_nearest_high is None:
        return 0.0
    h = abs(hours_to_nearest_high)
    if h <= 1:
        return 1.0
    if h <= 2:
        return 0.8
    if h <= 3:
        return 0.5
    if h <= 4:
        return 0.3
    return 0.1


def wind_west_score(westerly_wind_kmh: float) -> float:
    """0 below 30 km/h, linear 30-100 km/h -> 0-1."""
    if westerly_wind_kmh < 30:
        return 0.0
    if westerly_wind_kmh >= 100:
        return 1.0
    return (westerly_wind_kmh - 30) / 70.0


def soil_sat_score(soil_saturation_factor: float) -> float:
    """(factor - 1.0) / 0.5 clamped to 0-1 (1.0 dry, 1.5 saturated)."""
    return min(1.0, max(0.0, (soil_saturation_factor - 1.0) / 0.5))


def momentum_score(n_rising: int, river_score: float, n_total: int = 6) -> float:
    """Multi-station momentum (v3.0), gated by the river level score.

    Flooded episodes had 4.4 rising stations on average vs 2.1 otherwise.
    Rising from a LOW level (EP4: 5 rising, river=0.514; EP6: 6 rising,
    river=0.353) is an early warning, not an emergency.
    """
    if n_total == 0:
        return 0.0

    if n_rising == 0:
        base = 0.0
    elif n_rising <= 2:
        base = 0.2
    elif n_rising == 3:
        base = 0.5
    elif n_rising == 4:
        base = 0.8
    else:
        base = 1.0

    # Gates on the (post-curve) weighted river score, as calibrated in v3.0
    if river_score >= 0.55:
        gate = 1.0
    elif river_score >= 0.40:
        gate = 0.7
    elif river_score >= 0.30:
        gate = 0.4
    else:
        gate = 0.2

    return base * gate


def stability_dampener(
    river_score: float,
    tendency: Tendency,
    n_rising: int,
    rain_72h_score: float = 0.0,
    n_above_orange: int = 0,
    station_detail: bool = True,
) -> float:
    """Dampener for high-but-stable river conditions (v3.1/v3.2).

    Returns a multiplier 0.65-1.0. Catches EP5 (Pessac RED, 0 rising,
    0 rain = no flood).
    """
    if river_score < 0.4:
        return 1.0

    # Lidoire still fed by rain even if the Dordogne recedes
    if rain_72h_score > 0.4:
        return 1.0

    # Basin-wide high levels are an ongoing flood (EP14 April 23)
    if station_detail and n_above_orange >= 3:
        return 1.0

    if tendency in (Tendency.STABLE, Tendency.FALLING):
        if n_rising == 0:
            return 0.65
        if n_rising <= 1:
            return 0.80
    elif tendency == Tendency.RISING and n_rising <= 1:
        return 0.85

    return 1.0


def conjunction_multiplier(
    river_score: float,
    tide_phase: float,
    tendency: Tendency,
    n_rising: int,
    n_above_orange: int,
    rain_24h_score: float,
    rain_72h_score: float,
    soil_saturation_factor: float = 1.0,
    station_detail: bool = True,
) -> float:
    """Multiplier (1.0-2.5) for dangerous combinations (v3.1/v3.2).

    ``station_detail`` is False when only an aggregate river value is known
    (simulation): the high weighted average is then used as a proxy for
    "several stations above orange".
    """
    multiplier = 1.0

    # PRIMARY: active basin-wide flood
    if n_rising >= 4 and river_score > 0.35 and rain_24h_score > 0.2:
        multiplier += 0.45
    elif n_rising >= 4 and river_score > 0.35:
        multiplier += 0.12
    elif n_rising >= 5 and rain_24h_score > 0.2:
        multiplier += 0.25
    elif n_rising >= 3 and river_score > 0.55:
        multiplier += 0.25

    # MULTI-STATION ABOVE ORANGE (v3.2): ongoing flood conditions
    if station_detail and n_above_orange >= 3 and n_rising >= 1:
        multiplier += 0.45
    elif station_detail and n_above_orange >= 3:
        multiplier += 0.30
    elif station_detail and n_above_orange >= 2 and river_score > 0.55 and n_rising >= 1:
        multiplier += 0.20
    elif not station_detail and river_score > 0.60:
        multiplier += 0.25

    # DRAINAGE IMPAIRMENT (v3.1): Dordogne high + cumulative rain
    if river_score > 0.60 and rain_72h_score > 0.5:
        multiplier += 0.45
    elif river_score > 0.50 and rain_72h_score > 0.4:
        multiplier += 0.30
    elif river_score > 0.40 and rain_72h_score > 0.5:
        multiplier += 0.20
    elif river_score > 0.60 and rain_72h_score > 0.2:
        multiplier += 0.15

    # Soil saturation amplifies drainage impairment
    if soil_saturation_factor > 1.2 and river_score > 0.45 and rain_72h_score > 0.3:
        multiplier += 0.15

    # SECONDARY: rising + rain + elevated river
    if tendency == Tendency.RISING and rain_24h_score > 0.3 and river_score > 0.45:
        multiplier += 0.20

    # TERTIARY: sustained rain + elevated river
    if rain_72h_score > 0.5 and river_score > 0.45:
        multiplier += 0.15

    # TIDE: high tide + elevated river
    if tide_phase > 0.5 and river_score > 0.5:
        multiplier += 0.15

    return min(multiplier, 2.5)


def score_to_level(score: float, params: Optional[ModelParams] = None) -> RiskLevel:
    """Convert a 0-100 score to a risk level (15 / 35 / 55 boundaries)."""
    yellow, orange, red = (params or get_model_params()).level_thresholds
    if score < yellow:
        return RiskLevel.GREEN
    if score < orange:
        return RiskLevel.YELLOW
    if score < red:
        return RiskLevel.ORANGE
    return RiskLevel.RED


# ---------------------------------------------------------------------------
# Features and breakdown
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StationFeature:
    """One station with a valid level at the scoring time."""

    station_id: str
    station_name: str
    river: str
    height_m: float
    rate_cm_h: float
    weight: float
    thresholds: tuple[float, float, float]
    # Display-only tendency (e.g. Hub'Eau 3h tendency); scoring uses rate.
    display_tendency: Optional[Tendency] = None


@dataclass(frozen=True)
class RiskFeatures:
    """Everything the score depends on, at one instant (naive UTC)."""

    timestamp: datetime
    stations: tuple[StationFeature, ...]
    primary_station_id: str
    # Observed rain over the windows ending at ``timestamp`` (mm)
    rain_24h_mm: float = 0.0
    rain_72h_mm: float = 0.0
    rain_7d_mm: Optional[float] = None  # None = unavailable
    # Forecast rain over the windows starting at ``timestamp`` (mm)
    rain_forecast_24h_mm: float = 0.0
    rain_forecast_72h_mm: float = 0.0
    # Tide (astronomical predictions)
    tide_available: bool = True
    tide_coefficient: Optional[int] = None
    hours_to_high_tide: Optional[float] = None  # signed, nearest high tide
    # Backwater level H (m): max of the primary station over the trailing
    # BACKWATER_WINDOW_H (points <= t), gates the tide factors. None = not
    # computed: compute_risk falls back to the current primary level.
    backwater_level_m: Optional[float] = None
    # Optional inputs (None = unavailable, excluded from the denominator)
    westerly_wind_kmh: Optional[float] = None
    soil_saturation_factor: Optional[float] = None
    video_level: Optional[float] = None  # 0-100 camera index
    # Forecast only: projected number of rising stations
    n_rising_override: Optional[int] = None
    # True when ``stations`` is a single aggregate proxy (simulation)
    aggregate_only: bool = False
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class StationBreakdown:
    """Per-station score detail."""

    station: StationFeature
    linear_score: float
    score: float
    active: bool
    rising: bool
    above_yellow: bool
    above_orange: bool

    @property
    def weighted_score(self) -> float:
        return self.score * self.station.weight


@dataclass(frozen=True)
class RiskBreakdown:
    """Full, auditable result of :func:`compute_risk`."""

    model_version: str
    timestamp: datetime
    score: float  # 0-100, rounded to 0.1
    level: RiskLevel
    river_score: float
    rain_24h_score: float
    rain_72h_score: float
    rain_7d_score: float
    tide_score: float
    tide_phase_score: float
    tendency_score: float
    momentum_score: float
    wind_west_score: float
    soil_sat_score: float
    video_score: Optional[float]
    conjunction_multiplier: float
    stability_dampener: float
    weighted_sum: float
    total_weight: float
    tendency: Tendency
    primary_station_id: Optional[str]
    primary_rate_cm_h: float
    n_active: int
    n_rising: int
    n_above_orange: int
    rain_24h_effective_mm: float
    rain_72h_effective_mm: float
    tide_available: bool
    tide_coefficient: Optional[int]
    hours_to_high_tide: Optional[float]
    soil_saturation_factor: float
    stations: tuple[StationBreakdown, ...]
    weights: Mapping[str, float]
    unavailable: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    # v3.4: tide gate applied to tide_score / tide_phase_score above, and its input
    tide_gate: float = 1.0
    backwater_level_m: Optional[float] = None


def select_primary_station(
    stations: Sequence[StationFeature],
    primary_station_id: Optional[str],
) -> Optional[StationFeature]:
    """Primary station for the tendency; never a zero-weight station.

    Uses the configured primary (Pessac) when it has data, otherwise the
    highest-weight station with weight > 0.
    """
    active = [s for s in stations if s.weight > 0]
    primary = next((s for s in active if s.station_id == primary_station_id), None)
    if primary is None and active:
        primary = max(active, key=lambda s: s.weight)
    return primary


def compute_risk(
    features: RiskFeatures,
    params: Optional[ModelParams] = None,
) -> RiskBreakdown:
    """Compute the flood risk score from features. Pure function."""
    p = params or get_model_params()
    w = p.weights
    notes = list(features.notes)
    unavailable: list[str] = []

    # --- River levels (multi-station, zero-weight stations excluded) ---
    station_details: list[StationBreakdown] = []
    total_ws = 0.0
    total_w = 0.0
    for s in features.stations:
        linear = normalize_linear(s.height_m, s.thresholds)
        score = apply_curve(linear)
        active = s.weight > 0
        station_details.append(StationBreakdown(
            station=s,
            linear_score=linear,
            score=score,
            active=active,
            rising=s.rate_cm_h > RISING_RATE_CM_H,
            above_yellow=s.height_m >= s.thresholds[0],
            above_orange=s.height_m >= s.thresholds[1],
        ))
        if active:
            total_ws += score * s.weight
            total_w += s.weight
    river_score = total_ws / total_w if total_w > 0 else 0.0

    active_details = [d for d in station_details if d.active]
    n_active = len(active_details)
    n_above_orange = sum(1 for d in active_details if d.above_orange)
    if features.n_rising_override is not None:
        n_rising = max(0, int(features.n_rising_override))
    else:
        n_rising = sum(1 for d in active_details if d.rising)
    station_detail = (not features.aggregate_only) and n_active > 0
    if n_active == 0:
        notes.append("no river station with recent data")

    # --- Tendency (primary station rate) ---
    primary = select_primary_station(features.stations, features.primary_station_id)
    primary_rate = primary.rate_cm_h if primary else 0.0
    tendency = tendency_from_rate(primary_rate)
    tendency_score = tendency_score_from_rate(primary_rate)

    # --- Soil (single channel) ---
    if features.soil_saturation_factor is None:
        soil_factor = 1.0
        soil_score = 0.0
        unavailable.append("soil_sat")
    else:
        soil_factor = min(1.5, max(1.0, features.soil_saturation_factor))
        soil_score = soil_sat_score(soil_factor)

    # --- Rain ---
    rain_24h_eff = features.rain_24h_mm + features.rain_forecast_24h_mm * p.forecast_rain_24h_blend
    rain_72h_eff = features.rain_72h_mm + features.rain_forecast_72h_mm * p.forecast_rain_72h_blend
    rain_24h_score = min(1.0, normalize_score(rain_24h_eff, p.rain_24h_thresholds) * soil_factor)
    rain_72h_score = min(1.0, normalize_score(rain_72h_eff, p.rain_72h_thresholds) * soil_factor)
    if features.rain_7d_mm is None:
        rain_7d_score = 0.0
        unavailable.append("rain_7d")
    else:
        rain_7d_score = normalize_score(features.rain_7d_mm, p.rain_7d_thresholds)

    # --- Wind ---
    if features.westerly_wind_kmh is None:
        wind_score = 0.0
        unavailable.append("wind_west")
    else:
        wind_score = wind_west_score(features.westerly_wind_kmh)

    # --- Tide (contributes 0 when unavailable / estimated) ---
    if features.tide_available:
        coef = features.tide_coefficient or p.default_tide_coefficient
        tide_score = normalize_score(coef, p.tide_coef_thresholds)
        phase_score = tide_phase_score(features.hours_to_high_tide)
    else:
        coef = features.tide_coefficient
        tide_score = 0.0
        phase_score = 0.0
        notes.append("tide data unavailable or estimated: tide factors set to 0")

    # --- Tide gate (v3.4): backwater level H, else the current primary level ---
    gate_level = features.backwater_level_m
    if gate_level is None:
        current = next((s for s in features.stations if s.station_id == features.primary_station_id), None)
        if current is not None:
            gate_level = current.height_m
            notes.append("tide gate: backwater level unavailable, current primary level used")
    gate = tide_gate(gate_level, p.tide_gate_thresholds)
    # The conjunction keeps the ungated phase; contributions use the gated scores
    conjunction_phase = phase_score
    tide_score *= gate
    phase_score *= gate

    # --- Video ---
    if features.video_level is None:
        video_score = None
        unavailable.append("video")
    else:
        video_score = min(1.0, max(0.0, features.video_level / 100))

    # --- Momentum / conjunction / dampener ---
    mom_score = momentum_score(n_rising, river_score, n_total=max(n_active, 1))
    conjunction = conjunction_multiplier(
        river_score=river_score,
        tide_phase=conjunction_phase,
        tendency=tendency,
        n_rising=n_rising,
        n_above_orange=n_above_orange,
        rain_24h_score=rain_24h_score,
        rain_72h_score=rain_72h_score,
        soil_saturation_factor=soil_factor,
        station_detail=station_detail,
    )
    dampener = stability_dampener(
        river_score=river_score,
        tendency=tendency,
        n_rising=n_rising,
        rain_72h_score=rain_72h_score,
        n_above_orange=n_above_orange,
        station_detail=station_detail,
    )

    # --- Weighted sum, same denominator rule everywhere ---
    contributions = {
        "river": river_score,
        "rain_24h": rain_24h_score,
        "rain_72h": rain_72h_score,
        "rain_7d": rain_7d_score,
        "tide": tide_score,
        "tide_phase": phase_score,
        "tendency": tendency_score,
        "momentum": mom_score,
        "wind_west": wind_score,
        "soil_sat": soil_score,
        "video": video_score or 0.0,
    }
    weighted = sum(contributions[k] * w[k] for k in WEIGHT_KEYS if k not in unavailable)
    total_weight = sum(w[k] for k in WEIGHT_KEYS if k not in unavailable)

    weighted *= conjunction
    weighted *= dampener
    final = min((weighted / total_weight) * 100, 100.0) if total_weight > 0 else 0.0
    final = round(final, 1)

    return RiskBreakdown(
        model_version=MODEL_VERSION,
        timestamp=features.timestamp,
        score=final,
        level=score_to_level(final, p),
        river_score=river_score,
        rain_24h_score=rain_24h_score,
        rain_72h_score=rain_72h_score,
        rain_7d_score=rain_7d_score,
        tide_score=tide_score,
        tide_phase_score=phase_score,
        tendency_score=tendency_score,
        momentum_score=mom_score,
        wind_west_score=wind_score,
        soil_sat_score=soil_score,
        video_score=video_score,
        conjunction_multiplier=conjunction,
        stability_dampener=dampener,
        weighted_sum=weighted,
        total_weight=total_weight,
        tendency=tendency,
        primary_station_id=primary.station_id if primary else None,
        primary_rate_cm_h=primary_rate,
        n_active=n_active,
        n_rising=n_rising,
        n_above_orange=n_above_orange,
        rain_24h_effective_mm=rain_24h_eff,
        rain_72h_effective_mm=rain_72h_eff,
        tide_available=features.tide_available,
        tide_coefficient=coef,
        hours_to_high_tide=features.hours_to_high_tide,
        soil_saturation_factor=soil_factor,
        stations=tuple(station_details),
        weights=dict(w),
        unavailable=tuple(unavailable),
        notes=tuple(notes),
        tide_gate=gate,
        backwater_level_m=gate_level,
    )


# ---------------------------------------------------------------------------
# Pure feature-building helpers
# ---------------------------------------------------------------------------

def to_utc_naive(dt: datetime) -> datetime:
    """Convert to naive UTC (aware datetimes are converted, naive kept)."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def ceil_hour(dt: datetime) -> datetime:
    floored = floor_hour(dt)
    return floored if floored == dt else floored + timedelta(hours=1)


# Points are (timestamp naive UTC, height_m) sorted by timestamp.
LevelPoint = tuple[datetime, float]


def _index_after(points: Sequence[LevelPoint], t: datetime) -> int:
    """Index of the first point with timestamp > t (O(log n), no copy)."""
    return bisect.bisect_right(points, (t, float("inf")))


def _index_from(points: Sequence[LevelPoint], t: datetime) -> int:
    """Index of the first point with timestamp >= t."""
    return bisect.bisect_left(points, (t, float("-inf")))


def latest_level(
    points: Sequence[LevelPoint],
    t: datetime,
    max_age_hours: float = 3.0,
) -> Optional[LevelPoint]:
    """Latest point at or before ``t`` and not older than ``max_age_hours``."""
    idx = _index_after(points, t)
    if idx == 0:
        return None
    last = points[idx - 1]
    if (t - last[0]).total_seconds() > max_age_hours * 3600:
        return None
    return last


# Rate regression defaults (shared by realtime Hub'Eau, backfill, replay, forecast).
RATE_WINDOW_H = 3.0
RATE_MIN_POINTS = 2
RATE_MIN_SPAN_H = 0.5


def _level_point(point) -> LevelPoint:
    """(naive UTC timestamp, height_m) from a tuple, a dict or a RiverLevel-like object."""
    if isinstance(point, (tuple, list)):
        ts, h = point[0], point[1]
    elif isinstance(point, Mapping):
        ts, h = point["timestamp"], point["height_m"]
    else:
        ts, h = point.timestamp, point.height_m
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return to_utc_naive(ts), float(h)


def as_level_points(points: Iterable) -> list[LevelPoint]:
    """Normalise any level series to sorted ``(naive UTC, height_m)`` tuples.

    Accepts ``(timestamp, height_m)`` tuples, dicts with ``timestamp`` /
    ``height_m`` keys or objects with ``timestamp`` / ``height_m``
    attributes (e.g. ``RiverLevel``); timestamps may be naive UTC, tz-aware
    or ISO strings. Malformed points are skipped.
    """
    out: list[LevelPoint] = []
    for p in points:
        try:
            out.append(_level_point(p))
        except (KeyError, TypeError, ValueError, AttributeError, IndexError):
            continue
    out.sort(key=lambda lp: lp[0])
    return out


def _is_level_point_sequence(points) -> bool:
    """True for a non-empty list/tuple of naive ``(datetime, float)`` tuples.

    Such sequences follow the ``LevelPoint`` convention (sorted by time) and
    are searched with bisect, without a copy (backfill hot path).
    """
    if not isinstance(points, (list, tuple)) or not points:
        return False
    first = points[0]
    return (
        isinstance(first, tuple)
        and len(first) == 2
        and isinstance(first[0], datetime)
        and first[0].tzinfo is None
    )


def rate_cm_per_hour(
    points: Iterable,
    t: datetime,
    window_h: float = RATE_WINDOW_H,
    min_points: int = RATE_MIN_POINTS,
    min_span_h: float = RATE_MIN_SPAN_H,
) -> float:
    """Rate of water level change at ``t`` in cm/h (positive = rising).

    THE single implementation (collectors/hubeau.py imports it): ordinary
    least-squares slope of height vs time over the points in
    ``[t - window_h, t]``. Points after ``t`` are ignored, so the same helper
    serves live data (t = now) and replays/backfills (t = past instant)
    without look-ahead.

    Args:
        points: sorted ``(naive UTC, height_m)`` tuples (fast path), or any
            series accepted by :func:`as_level_points` (RiverLevel objects,
            dicts, tz-aware or ISO timestamps, any order).
        t: evaluation instant (naive UTC or tz-aware).
        window_h: trailing window length in hours.
        min_points: minimum number of points in the window (at least 2).
        min_span_h: minimum time span covered by those points (hours).

    Returns:
        Unrounded slope in cm/h; 0.0 when there is not enough data.
    """
    if not _is_level_point_sequence(points):
        points = as_level_points(points)
    t = to_utc_naive(t)
    end_idx = _index_after(points, t)
    start_idx = _index_from(points, t - timedelta(hours=window_h))
    window = points[start_idx:end_idx]
    if len(window) < max(2, min_points):
        return 0.0
    span_h = (window[-1][0] - window[0][0]).total_seconds() / 3600
    if span_h < min_span_h:
        return 0.0
    xs = [(ts - window[0][0]).total_seconds() / 3600 for ts, _ in window]
    ys = [h for _, h in window]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return 0.0
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return (sxy / sxx) * 100.0


def backwater_level(
    points: Iterable,
    t: datetime,
    window_h: float = BACKWATER_WINDOW_H,
) -> Optional[float]:
    """Max level over ``[t - window_h, t]`` (points <= t), None without points.

    Applied to the primary station (Pessac): the 12.5 h max removes its own
    tidal oscillation. Also the H_aval of the shadow model (``risk_shadow``).
    """
    if not _is_level_point_sequence(points):
        points = as_level_points(points)
    t = to_utc_naive(t)
    window = points[_index_from(points, t - timedelta(hours=window_h)):_index_after(points, t)]
    return max(h for _, h in window) if window else None


def aggregate_hourly_rain(
    records: Iterable[tuple[datetime, float, str]],
) -> dict[datetime, float]:
    """Hourly rain (mm) keyed by hour END, from (timestamp, mm_h, series_key).

    A record at ``ts`` belongs to the hour ending at ceil(ts) (Open-Meteo
    convention: the value at T is the precipitation of [T-1h, T]). Each
    series (source/city) is averaged within the hour first, then the hour
    value is the mean across series: sources are NOT summed together and
    sub-hourly samples are not summed either.
    """
    per_series: dict[datetime, dict[str, list[float]]] = {}
    for ts, mm, key in records:
        if mm is None:
            continue
        bucket = ceil_hour(to_utc_naive(ts))
        per_series.setdefault(bucket, {}).setdefault(key or "", []).append(float(mm))
    hourly: dict[datetime, float] = {}
    for bucket, series in per_series.items():
        means = [sum(v) / len(v) for v in series.values() if v]
        if means:
            hourly[bucket] = sum(means) / len(means)
    return hourly


def rain_window_sum(
    hourly: Mapping[datetime, float],
    t: datetime,
    hours: int,
    forward: bool = False,
) -> float:
    """Sum of hourly buckets over a window anchored at floor_hour(t).

    Past window (default): hours ending in (t0 - hours, t0], strictly
    observed before t. Forward window: hours ending in (t0, t0 + hours].
    """
    t0 = floor_hour(t)
    if forward:
        buckets = (t0 + timedelta(hours=k) for k in range(1, hours + 1))
    else:
        buckets = (t0 - timedelta(hours=k) for k in range(0, hours))
    return sum(hourly.get(b, 0.0) for b in buckets)


def rain_window_count(hourly: Mapping[datetime, float], t: datetime, hours: int) -> int:
    """Number of past hourly buckets with data in the window."""
    t0 = floor_hour(t)
    return sum(1 for k in range(hours) if (t0 - timedelta(hours=k)) in hourly)


@dataclass(frozen=True)
class TideState:
    """Tide inputs at one instant."""

    available: bool
    coefficient: Optional[int]
    hours_to_high_tide: Optional[float]


def tide_at(
    t: datetime,
    high_tides: Sequence[tuple[datetime, Optional[int]]],
    fallback_coefficient: Optional[int] = None,
    coefficient_records: Sequence[tuple[datetime, int]] = (),
    max_distance_h: float = 13.0,
) -> TideState:
    """Tide coefficient and signed hours to the nearest high tide at ``t``.

    Tides are astronomical predictions, so using future high tides is not
    leakage. Coefficient: latest high tide at or before ``t`` with a
    coefficient, else ``fallback_coefficient``, else the nearest coefficient
    record within 24h, else the next high tide's coefficient.
    """
    highs = sorted((to_utc_naive(ts), c) for ts, c in high_tides)
    hours_to_high: Optional[float] = None
    if highs:
        nearest = min(highs, key=lambda x: abs((x[0] - t).total_seconds()))
        delta_h = (nearest[0] - t).total_seconds() / 3600
        if abs(delta_h) <= max_distance_h:
            hours_to_high = delta_h

    coef: Optional[int] = None
    past_with_coef = [c for ts, c in highs if ts <= t and c]
    if past_with_coef:
        coef = int(past_with_coef[-1])
    elif fallback_coefficient:
        coef = int(fallback_coefficient)
    elif coefficient_records:
        ts_c, c = min(
            ((to_utc_naive(ts), c) for ts, c in coefficient_records),
            key=lambda x: abs((x[0] - t).total_seconds()),
        )
        if abs((ts_c - t).total_seconds()) <= 24 * 3600:
            coef = int(c)
    if coef is None:
        future_with_coef = [c for ts, c in highs if ts > t and c]
        if future_with_coef:
            coef = int(future_with_coef[0])

    available = hours_to_high is not None
    return TideState(available=available, coefficient=coef, hours_to_high_tide=hours_to_high)


def detect_high_tides(heights: Sequence[tuple[datetime, float]]) -> list[datetime]:
    """High tide times as local maxima of a sorted height series."""
    out = []
    for i in range(1, len(heights) - 1):
        if heights[i][1] >= heights[i - 1][1] and heights[i][1] >= heights[i + 1][1]:
            # Skip plateau duplicates
            if out and (heights[i][0] - out[-1]).total_seconds() < 3 * 3600:
                continue
            out.append(heights[i][0])
    return out


@dataclass(frozen=True)
class StationConfig:
    """Static station configuration used by builders."""

    station_id: str
    station_name: str
    river: str
    weight: float
    thresholds: tuple[float, float, float]

    @classmethod
    def from_settings_dict(cls, cfg: Mapping) -> "StationConfig":
        th = cfg.get("thresholds", {})
        return cls(
            station_id=cfg["id"],
            station_name=cfg.get("name", cfg["id"]),
            river=cfg.get("river", ""),
            weight=float(cfg.get("weight", 0.0)),
            thresholds=(
                float(th.get("yellow", 3.0)),
                float(th.get("orange", 4.0)),
                float(th.get("red", 5.0)),
            ),
        )


def station_feature_at(
    cfg: StationConfig,
    points: Sequence[LevelPoint],
    t: datetime,
    max_age_hours: float = 3.0,
    rate_window_h: float = 3.0,
) -> Optional[StationFeature]:
    """Station feature at ``t`` from its sorted level series (points <= t)."""
    last = latest_level(points, t, max_age_hours)
    if last is None:
        return None
    return StationFeature(
        station_id=cfg.station_id,
        station_name=cfg.station_name,
        river=cfg.river,
        height_m=last[1],
        rate_cm_h=rate_cm_per_hour(points, t, rate_window_h),
        weight=cfg.weight,
        thresholds=cfg.thresholds,
    )


def build_historical_features(
    t: datetime,
    station_configs: Sequence[StationConfig],
    station_points: Mapping[str, Sequence[LevelPoint]],
    primary_station_id: str,
    rain_hourly: Mapping[datetime, float],
    tide: TideState,
    max_age_hours: float = 3.0,
    rate_window_h: float = 3.0,
    rain_7d_available: bool = True,
) -> RiskFeatures:
    """Features at a past instant ``t`` from stored series (backfill, TimeMachine).

    Observed series (levels, rain) only contribute points at or before ``t``.
    No forecast rain (historical), no wind/soil/video (not archived). The
    caller must load at least BACKWATER_WINDOW_H of primary-station history
    before ``t`` for the tide gate.
    """
    stations = []
    for cfg in station_configs:
        feat = station_feature_at(
            cfg, station_points.get(cfg.station_id, ()), t, max_age_hours, rate_window_h,
        )
        if feat is not None:
            stations.append(feat)

    rain_24h = rain_window_sum(rain_hourly, t, 24)
    rain_72h = rain_window_sum(rain_hourly, t, 72)
    rain_7d = rain_window_sum(rain_hourly, t, 168) if rain_7d_available else None

    return RiskFeatures(
        timestamp=t,
        stations=tuple(stations),
        primary_station_id=primary_station_id,
        rain_24h_mm=rain_24h,
        rain_72h_mm=rain_72h,
        rain_7d_mm=rain_7d,
        tide_available=tide.available,
        tide_coefficient=tide.coefficient,
        hours_to_high_tide=tide.hours_to_high_tide,
        backwater_level_m=backwater_level(station_points.get(primary_station_id, ()), t),
    )


def features_with(features: RiskFeatures, **changes) -> RiskFeatures:
    """Copy of features with some fields replaced."""
    from dataclasses import replace

    return replace(features, **changes)


__all__ = [
    "MODEL_VERSION",
    "ModelParams",
    "RiskBreakdown",
    "RiskFeatures",
    "StationBreakdown",
    "StationConfig",
    "StationFeature",
    "TideState",
    "aggregate_hourly_rain",
    "apply_curve",
    "as_level_points",
    "backwater_level",
    "build_historical_features",
    "compute_risk",
    "conjunction_multiplier",
    "detect_high_tides",
    "get_model_params",
    "latest_level",
    "momentum_score",
    "normalize_linear",
    "normalize_score",
    "params_from_settings",
    "rain_window_sum",
    "rate_cm_per_hour",
    "score_to_level",
    "stability_dampener",
    "tendency_from_rate",
    "tendency_score_from_rate",
    "tide_at",
    "tide_gate",
    "tide_phase_score",
    "to_utc_naive",
]
