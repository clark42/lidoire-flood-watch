"""Pydantic schemas for API data models."""

from datetime import datetime, timezone
from typing import Annotated, Optional
from pydantic import BaseModel, Field, PlainSerializer
from enum import Enum


def utc_isoformat(dt: datetime) -> str:
    """ISO 8601 UTC with an explicit "Z" (naive values are UTC by convention)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


# Datetimes are handled as naive UTC internally; the JSON API always states
# the offset so clients never read them as local time. Python-mode dumps
# (model_dump()) keep the datetime object unchanged.
UtcDatetime = Annotated[datetime, PlainSerializer(utc_isoformat, return_type=str, when_used="json")]


class TideType(str, Enum):
    """Type of tide."""
    HIGH = "PM"  # Pleine Mer
    LOW = "BM"   # Basse Mer


class RiskLevel(str, Enum):
    """Risk level classification."""
    GREEN = "green"
    YELLOW = "yellow"
    ORANGE = "orange"
    RED = "red"


class Tendency(str, Enum):
    """Water level tendency."""
    RISING = "hausse"
    STABLE = "stable"
    FALLING = "baisse"


# Tide data
class TideData(BaseModel):
    """Tide data point."""
    timestamp: UtcDatetime
    port: str
    tide_type: TideType
    height_m: float
    coefficient: Optional[int] = None
    # Data origin: "shom", "open-meteo-marine" (model sea level, coefficient
    # estimated from height) or "estimated" (computed placeholder, no real source)
    source: Optional[str] = None


class TideForecast(BaseModel):
    """Tide forecast for upcoming hours."""
    port: str
    next_high_tide: Optional[TideData] = None
    next_low_tide: Optional[TideData] = None
    current_coefficient: Optional[int] = None
    tides: list[TideData] = []  # upcoming tides (t > now), for display
    # Tides of the last 13h (t <= now). The risk model needs the high tide
    # that just passed (tide phase is the signed distance to the NEAREST high
    # tide, past or future), exactly like the backfill / TimeMachine.
    recent_tides: list[TideData] = []
    # False when no real tide source answered: tides are computed estimates
    # and must not drive the risk model as if they were real.
    available: bool = True
    source: Optional[str] = None
    # Hours added to the source times to estimate the tide at the Lidoire
    # confluence (settings.tide_phase_lag_hours); all times above include it.
    phase_lag_hours: Optional[float] = None


# River level data
class RiverLevel(BaseModel):
    """River level measurement."""
    timestamp: UtcDatetime
    station_id: str
    station_name: str
    river: str
    height_m: float
    flow_m3s: Optional[float] = None
    tendency: Optional[Tendency] = None


class RiverLevelForecast(BaseModel):
    """River level with forecast scenarios."""
    current: RiverLevel
    tendency: Tendency
    forecast_scenarios: list[dict] = []
    # False when no real data is available: current.height_m is then a 0.0
    # placeholder. source: "vigicrues", "vigicrues-stale" or "unavailable".
    available: bool = True
    source: Optional[str] = None


class StationLevel(BaseModel):
    """Current level data for a single monitoring station."""
    station_id: str
    station_name: str
    river: str
    current_level: RiverLevel
    tendency: Tendency
    rate_cm_per_hour: float = 0.0  # Positive = rising
    distance_km: float = 0.0  # Distance from moulin
    delay_hours: float = 0.0  # Estimated propagation delay
    weight: float = 0.1  # Weight in risk calculation
    threshold_yellow: float = 3.0
    threshold_orange: float = 4.0
    threshold_red: float = 5.0
    lat: Optional[float] = None
    lon: Optional[float] = None


class MultiStationData(BaseModel):
    """Aggregated data from all monitoring stations."""
    timestamp: UtcDatetime
    stations: list[StationLevel] = []
    primary_station_id: str = "P555001001"  # Pessac-sur-Dordogne (non-tidal)


# Weather data
class WeatherForecast(BaseModel):
    """Weather forecast data point."""
    timestamp: UtcDatetime
    point_id: str
    point_name: str
    precipitation_mm: float
    temperature_c: Optional[float] = None
    humidity_percent: Optional[float] = None
    wind_speed_kmh: Optional[float] = None
    wind_direction_deg: Optional[float] = None  # 270° = West
    storm_probability: Optional[float] = None


class RainfallObservation(BaseModel):
    """Observed rainfall data."""
    timestamp: UtcDatetime
    source: str  # radar, station, comephore
    precipitation_mm_h: float
    precipitation_mm_24h: Optional[float] = None
    precipitation_mm_72h: Optional[float] = None
    basin_avg: bool = True  # Is this averaged over the basin?


class RainfallSummary(BaseModel):
    """Rainfall summary for the basin."""
    last_hour_mm: float = 0.0
    last_6h_mm: float = 0.0
    last_24h_mm: float = 0.0
    last_72h_mm: float = 0.0
    last_7d_mm: float = 0.0  # 7 days cumulative
    forecast_24h_mm: float = 0.0
    forecast_72h_mm: float = 0.0
    # Soil saturation from Open-Meteo (m³/m³)
    soil_moisture: Optional[float] = None
    soil_saturation_factor: float = 1.0  # 1.0 (normal) to 1.5 (saturated)


class BasinRainfallSummary(BaseModel):
    """Aggregated rainfall data for the Lidoire basin (Netatmo stations).

    ``available`` is False when no basin station reported (Netatmo not
    authorised or down): the 0.0 averages are then placeholders, not
    measurements.
    """
    timestamp: UtcDatetime
    available: bool = False
    stations_count: int = 0
    basin_average_1h_mm: float = 0.0
    basin_average_24h_mm: float = 0.0
    weighted_average_24h_mm: float = 0.0
    by_position: dict = {}  # amont/milieu/aval with avg + station count
    sparkline_24h: list[dict] = []  # [{timestamp, value}]


# Video analysis
class VideoAnalysis(BaseModel):
    """Video analysis result from cascade camera."""
    timestamp: UtcDatetime
    water_level_index: float = Field(..., ge=0, le=100, description="Water level index 0-100")
    delta_from_baseline: Optional[float] = None
    anomaly_detected: bool = False
    snapshot_url: Optional[str] = None


# Risk scoring
class RiskWeights(BaseModel):
    """Weights used for each risk factor in the calculation (v3.0).

    Calibrated from 14 episodes (5 confirmed floods, 9 false positives).
    Key insight: the moulin is on the Lidoire (tributary), not on the Dordogne.
    High Dordogne level alone does NOT predict flooding — the signal is
    multi-station rising momentum + recent rainfall.
    """
    river: float = 0.28
    rain_24h: float = 0.10
    rain_72h: float = 0.13
    rain_7d: float = 0.05
    tide: float = 0.05
    tide_phase: float = 0.05
    tendency: float = 0.13
    momentum: float = 0.08  # Multi-station rising simultaneously
    wind_west: float = 0.03
    soil_sat: float = 0.05
    video: float = 0.05


class StationRiskScore(BaseModel):
    """Risk score contribution from a single station."""
    station_id: str
    station_name: str
    river: str
    height_m: float
    score: float = Field(..., ge=0, le=1)
    weight: float
    weighted_score: float
    tendency: Tendency
    rate_cm_per_hour: float = 0.0
    # v3.3: linear (pre-curve) score and height >= orange threshold
    linear_score: Optional[float] = None
    above_orange: Optional[bool] = None


class RiskFactors(BaseModel):
    """Individual risk factors."""
    rain_24h_score: float = Field(..., ge=0, le=1)
    rain_72h_score: float = Field(..., ge=0, le=1)
    rain_7d_score: float = Field(0.0, ge=0, le=1)  # 7 days cumulative
    river_level_score: float = Field(..., ge=0, le=1)
    tide_score: float = Field(..., ge=0, le=1)
    tide_phase_score: float = Field(..., ge=0, le=1)
    wind_west_score: float = Field(0.0, ge=0, le=1)  # Westerly wind causing surge
    soil_saturation_factor: float = Field(1.0, ge=1.0, le=1.5)  # Multiplier on rain
    video_level_score: Optional[float] = Field(None, ge=0, le=1)
    # Multi-station factors (v3.0)
    tendency_score: float = Field(0.0, ge=0, le=1)  # Rate of rise
    momentum_score: float = Field(0.0, ge=0, le=1)  # Multi-station rising simultaneously
    conjunction_multiplier: float = Field(1.0, ge=1.0, le=2.5)  # Active flood combo
    stations_detail: list[StationRiskScore] = []  # Per-station breakdown
    # Weights and raw values for detailed breakdown
    weights: Optional[RiskWeights] = None
    rain_24h_raw_mm: Optional[float] = None
    rain_72h_raw_mm: Optional[float] = None
    tide_coefficient_raw: Optional[int] = None
    tide_hours_to_high: Optional[float] = None
    tendency_rate_cm_h: Optional[float] = None
    # v3.3 auditability
    stability_dampener: float = Field(1.0, ge=0, le=1)
    n_rising_stations: Optional[int] = None
    n_above_orange: Optional[int] = None
    tide_available: bool = True
    unavailable_factors: list[str] = []
    notes: list[str] = []
    # v3.4: gate (0-1) applied to tide_score / tide_phase_score, and its input
    tide_gate: float = Field(1.0, ge=0, le=1)
    backwater_level_m: Optional[float] = None


class RiskScore(BaseModel):
    """Overall risk score and breakdown."""
    timestamp: UtcDatetime
    level: RiskLevel
    score: float = Field(..., ge=0, le=100, description="Risk score 0-100")
    factors: RiskFactors
    description: str
    recommendations: list[str] = []
    model_version: Optional[str] = None

    model_config = {"protected_namespaces": ()}


# Flood episodes
class EpisodeOutcome(str, Enum):
    """Ground-truth outcome of an episode at the mill.

    - flooded: water entered the mill (confirmed flood)
    - false_positive: alert/detection but nothing notable happened at the mill
    - near_miss: water rose markedly at the mill but did not flood it
    - unknown: labelled, but the outcome could not be established

    A null outcome on an episode means "not labelled yet" (pending review).
    """
    FLOODED = "flooded"
    FALSE_POSITIVE = "false_positive"
    NEAR_MISS = "near_miss"
    UNKNOWN = "unknown"


class FloodEpisode(BaseModel):
    """Historical flood episode record."""
    id: Optional[int] = None
    start_time: UtcDatetime
    peak_time: Optional[UtcDatetime] = None
    end_time: Optional[UtcDatetime] = None
    severity: int = Field(..., ge=1, le=5, description="Severity 1-5")
    description: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[UtcDatetime] = None
    # Structured ground truth (null outcome = not labelled yet)
    outcome: Optional[EpisodeOutcome] = None
    observed_at: Optional[UtcDatetime] = Field(
        None, description="Observed peak time at the mill"
    )
    water_height_cm: Optional[float] = Field(
        None, ge=0, description="Observed water height inside the mill (cm)"
    )
    outcome_source: Optional[str] = Field(
        None, max_length=100, description="Who/what set the outcome"
    )


class FloodEpisodeCreate(BaseModel):
    """Schema for creating/updating a flood episode.

    On update, ground-truth fields (outcome, observed_at, water_height_cm,
    outcome_source) are only changed when explicitly present in the payload,
    so legacy clients that omit them do not wipe existing labels.
    """
    start_time: UtcDatetime
    peak_time: Optional[UtcDatetime] = None
    end_time: Optional[UtcDatetime] = None
    severity: int = Field(..., ge=1, le=5)
    description: Optional[str] = None
    notes: Optional[str] = None
    outcome: Optional[EpisodeOutcome] = None
    observed_at: Optional[UtcDatetime] = None
    water_height_cm: Optional[float] = Field(None, ge=0)
    outcome_source: Optional[str] = Field(None, max_length=100)


class EpisodeLabel(BaseModel):
    """One annotated reference episode (from app/data/episodes_labels.json)."""
    key: str = Field(..., description="Stable key: peak date YYYY-MM-DD")
    peak_time: UtcDatetime = Field(..., description="Approximate peak time (naive, UTC assumed)")
    outcome: EpisodeOutcome
    severity: int = Field(..., ge=1, le=5)
    water_height_cm: Optional[float] = Field(None, ge=0)
    source: str = Field(..., description="Who/what labelled the episode")
    notes: Optional[str] = None
    flags: list[str] = Field(default_factory=list, description="Data quality caveats")
    legacy_db_id: Optional[int] = Field(
        None, description="Id used in validate_model.py / analyze_episodes.py"
    )
    legacy_refs: list[str] = Field(
        default_factory=list, description="Other names used in code/docstrings"
    )
    legacy_stored_score: Optional[float] = Field(
        None, description="Stored risk score at peak per validate_model.py (not ground truth)"
    )


class EpisodeLabelSet(BaseModel):
    """Versioned set of reference episode labels."""
    version: int
    updated_at: str
    description: str
    exhaustive: bool = Field(
        False, description="True only if every flood in the period is labelled"
    )
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    caveats: list[str] = []
    inconsistencies: list[str] = []
    episodes: list[EpisodeLabel]


class EpisodeContext(BaseModel):
    """Reconstructed context for an episode."""
    rain_24h_before_mm: float
    rain_48h_before_mm: float
    rain_72h_before_mm: float
    max_dordogne_level_m: float
    max_tide_coefficient: int
    tide_phase_at_peak: Optional[str] = None
    hours_since_high_tide: Optional[float] = None


class EpisodeAnalysis(BaseModel):
    """Full analysis of a flood episode."""
    episode: FloodEpisode
    context: EpisodeContext
    tides: list[TideData] = []
    river_levels: list[RiverLevel] = []
    rainfall: list[RainfallObservation] = []
    video_data: list[VideoAnalysis] = []
    matched_criteria: list[str] = []


# Soil conditions
class SoilConditions(BaseModel):
    """Soil and agricultural conditions."""
    # Open-Meteo forecast API, layers served by DWD ICON in "best match".
    # The timeline series (soil_timeline) use the ECMWF IFS / ERA5 layers
    # 0-7 / 7-28 / 28-100 cm instead: another model, values differ.
    soil_moisture: Optional[float] = None  # m³/m³ (0-9cm average, ICON)
    moisture_deep: Optional[float] = None  # m³/m³ (9-81cm average, ICON)
    soil_temperature_0cm: Optional[float] = None  # °C (surface)
    et0_mm: Optional[float] = None  # Evapotranspiration mm/day
    soil_saturation_factor: float = 1.0  # 1.0 (dry) to 1.5 (saturated)
    precip_probability: Optional[float] = None  # %
    # Meteo Agricole bilan hydrique
    water_balance_30d_mm: Optional[float] = None
    surplus_deficit_30d_pct: Optional[float] = None
    deficit_12m_mm: Optional[float] = None


# Wind data
class WindData(BaseModel):
    """Current wind conditions."""
    wind_speed_kmh: float = 0.0
    wind_direction_deg: float = 0.0
    westerly_wind_kmh: float = 0.0


# Meteo Agricole data
class NowcastRainData(BaseModel):
    """Nowcast rain data from La Meteo Agricole minute-by-minute radar."""
    timestamp: UtcDatetime
    commune: str = "Belves-de-Castillon"
    total_60min_mm: float = 0.0
    max_intensity_mm: float = 0.0
    rain_minutes: int = 0
    minute_data: list[dict] = []  # [{"minute": "21:36", "mm": 0.01}, ...]


class MeteoAgricoleData(BaseModel):
    """Data scraped from La Meteo Agricole."""
    timestamp: UtcDatetime
    commune: str = "Castillon-la-Bataille"
    rain_min_mm: float = 0.0
    rain_max_mm: float = 0.0
    rain_probability_pct: float = 0.0
    wind_avg_kmh: float = 0.0
    wind_gust_kmh: float = 0.0
    wind_direction: str = ""
    precip_30d_mm: Optional[float] = None
    surplus_deficit_30d_pct: Optional[float] = None
    deficit_12m_mm: Optional[float] = None
    rain_forecast_3d_mm: float = 0.0
    rain_forecast_7d_mm: float = 0.0


# Realtime dashboard data
class RealtimeData(BaseModel):
    """Real-time dashboard data."""
    timestamp: UtcDatetime
    risk: RiskScore
    tide: TideForecast
    river: RiverLevelForecast
    rainfall: RainfallSummary
    video: Optional[VideoAnalysis] = None
    alerts: list[str] = []
    # V2: Multi-station data
    stations: Optional[MultiStationData] = None
    netatmo_available: bool = False
    # V3: Agricultural / soil / wind data
    soil: Optional[SoilConditions] = None
    wind: Optional[WindData] = None
    meteo_agricole: Optional[MeteoAgricoleData] = None
    nowcast_rain: Optional[NowcastRainData] = None


# Timeline data
class TimelinePoint(BaseModel):
    """Single point on the timeline."""
    timestamp: UtcDatetime
    tide_height_m: Optional[float] = None
    river_level_m: Optional[float] = None
    rainfall_mm_h: Optional[float] = None
    risk_score: Optional[float] = None
    video_level: Optional[float] = None


class TimelineData(BaseModel):
    """Timeline data for charts."""
    start: UtcDatetime
    end: UtcDatetime
    resolution: str  # "1h", "15m", "1d"
    points: list[TimelinePoint]
    episodes: list[FloodEpisode] = []


# Unified Timeline Data (Multi-Source Comparison)
class TimelineDataPoint(BaseModel):
    """Single data point in a timeline series."""
    timestamp: UtcDatetime
    value: float


class TimelineSeries(BaseModel):
    """A single data series for timeline display."""
    type: str  # "river_level", "rainfall", "tide", or "risk_score"
    name: str  # Display name
    unit: str  # "m", "mm/h", or "/100"
    color: str  # Hex color
    data: list[TimelineDataPoint] = []


class UnifiedTimelineData(BaseModel):
    """Unified timeline data combining river levels and rainfall."""
    start: UtcDatetime
    end: UtcDatetime
    resolution: str
    series: dict[str, TimelineSeries] = {}  # key: source_id


class RiverStationInfo(BaseModel):
    """Information about a river monitoring station."""
    id: str
    name: str
    river: str
    color: str
    distance_km: float = 0.0
    delay_hours: float = 0.0


class RainfallStationInfo(BaseModel):
    """Information about a rainfall monitoring station."""
    id: str  # Format: "source:city" e.g. "netatmo-lidoire:Saint-Remy"
    name: str  # City name
    position: str  # "amont", "milieu", "aval"
    color: str
    # False when the source is not collecting (Netatmo not authorised): its
    # series only holds what was stored before, usually nothing.
    available: bool = True


class AvailableSources(BaseModel):
    """Available data sources for timeline comparison."""
    river_stations: list[RiverStationInfo] = []
    rainfall_stations: list[RainfallStationInfo] = []
    has_tide: bool = True
    has_risk_score: bool = True
    has_infoclimat: bool = True
    has_wind: bool = True
    has_soil: bool = True


# ---------------------------------------------------------------------------
# Official vigilance (Vigicrues, Meteo-France): informative, not a model input
# ---------------------------------------------------------------------------


class VigilanceWindow(BaseModel):
    """Validity window of one Meteo-France colour (level 1-4)."""
    begin: UtcDatetime
    end: UtcDatetime
    level: int = Field(..., ge=1, le=4)


class VigilanceTroncon(BaseModel):
    """Vigicrues flood vigilance of one troncon (level 1 vert .. 4 rouge)."""
    code: str
    name: str
    level: Optional[int] = Field(None, ge=1, le=4, description="None when missing from the feed")
    color: Optional[str] = None  # vert | jaune | orange | rouge
    url: str
    station_url: Optional[str] = None


class VigicruesVigilance(BaseModel):
    available: bool = False
    issued_at: Optional[UtcDatetime] = None
    fetched_at: Optional[UtcDatetime] = None
    troncons: list[VigilanceTroncon] = []
    url: str


class VigilancePhenomenon(BaseModel):
    """Meteo-France colour of one phenomenon on the current (J) map."""
    id: str
    key: str  # pluie-inondation | crues | orages | vent ...
    label: str
    level: Optional[int] = Field(None, ge=1, le=4)
    color: Optional[str] = None
    level_tomorrow: Optional[int] = Field(None, ge=1, le=4)
    # Windows above green (J and J1), in time order
    windows: list[VigilanceWindow] = []


class VigilanceDepartment(BaseModel):
    code: str
    name: str
    max_level: Optional[int] = Field(None, ge=1, le=4, description="Max over every phenomenon (J)")
    phenomena: list[VigilancePhenomenon] = []
    url: str


class MeteoFranceVigilance(BaseModel):
    available: bool = False
    issued_at: Optional[UtcDatetime] = None
    fetched_at: Optional[UtcDatetime] = None
    valid_from: Optional[UtcDatetime] = None
    valid_to: Optional[UtcDatetime] = None
    departments: list[VigilanceDepartment] = []
    url: str


class VigilanceStatus(BaseModel):
    """GET /api/vigilance: official colours, read from the collector cache."""
    timestamp: UtcDatetime
    vigicrues: VigicruesVigilance
    meteofrance: MeteoFranceVigilance
    note: str = "Vigilance officielle, informative : elle n'entre pas dans le score de risque."

