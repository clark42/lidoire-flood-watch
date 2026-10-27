// API Types

export type RiskLevel = 'green' | 'yellow' | 'orange' | 'red'
export type TideType = 'PM' | 'BM'
export type Tendency = 'hausse' | 'stable' | 'baisse'

export interface TideData {
  timestamp: string
  port: string
  tide_type: TideType
  height_m: number
  coefficient: number | null
}

export interface TideForecast {
  port: string
  next_high_tide: TideData | null
  next_low_tide: TideData | null
  current_coefficient: number | null
  tides: TideData[]
}

export interface RiverLevel {
  timestamp: string
  station_id: string
  station_name: string
  river: string
  height_m: number
  flow_m3s: number | null
  tendency: Tendency | null
}

/** Official Vigicrues forecast at Libourne (next high waters, 80 % interval) */
export interface VigicruesScenario {
  timestamp: string
  min_m: number
  mean_m: number
  max_m: number
}

export interface RiverLevelForecast {
  current: RiverLevel
  tendency: Tendency
  /** Vigicrues forecast at Libourne when `source` is "vigicrues" */
  forecast_scenarios: VigicruesScenario[] | unknown[]
  available?: boolean
  source?: string
}

export interface RainfallSummary {
  last_hour_mm: number
  last_6h_mm: number
  last_24h_mm: number
  last_72h_mm: number
  last_7d_mm: number
  forecast_24h_mm: number
  forecast_72h_mm: number
  // Soil saturation from Open-Meteo (m3/m3)
  soil_moisture: number | null
  soil_saturation_factor: number  // 1.0 (normal) to 1.5 (saturated)
}

// Default values mirror backend RiskWeights (v3.2)
export interface RiskWeights {
  river: number     // 28% (level alone doesn't predict moulin flooding)
  rain_24h: number  // 10%
  rain_72h: number  // 13%
  rain_7d: number   // 5%
  tide: number      // 5%
  tide_phase: number // 5%
  tendency: number   // 13%
  momentum: number   // 8% (NEW: multi-station rising simultaneously)
  wind_west: number  // 3%
  soil_sat: number   // 5%
  video: number      // 5%
}

export interface StationRiskScore {
  station_id: string
  station_name: string
  river: string
  height_m: number
  score: number
  weight: number
  weighted_score: number
  tendency: Tendency
  rate_cm_per_hour: number
}

export interface RiskFactors {
  rain_24h_score: number
  rain_72h_score: number
  rain_7d_score?: number
  river_level_score: number
  tide_score: number
  tide_phase_score: number
  wind_west_score?: number
  soil_saturation_factor?: number
  video_level_score: number | null
  tendency_score?: number
  momentum_score?: number
  conjunction_multiplier?: number
  stations_detail?: StationRiskScore[]
  weights?: RiskWeights
  rain_24h_raw_mm?: number
  rain_72h_raw_mm?: number
  tide_coefficient_raw?: number
  tide_hours_to_high?: number
  tendency_rate_cm_h?: number
  // v3.3 auditability
  stability_dampener?: number
  n_rising_stations?: number | null
  n_above_orange?: number | null
  tide_available?: boolean
  unavailable_factors?: string[] // excluded from the denominator
  notes?: string[]
  // v3.4: gate (0-1) applied to tide_score / tide_phase_score, and its input
  tide_gate?: number
  backwater_level_m?: number | null // max of Pessac over the previous 12.5 h
}

export interface RiskScore {
  timestamp: string
  level: RiskLevel
  score: number
  factors: RiskFactors
  description: string
  recommendations: string[]
  model_version?: string | null
}

export interface VideoAnalysis {
  timestamp: string
  water_level_index: number
  delta_from_baseline: number | null
  anomaly_detected: boolean
  snapshot_url: string | null
}

export interface StationData {
  station_id: string
  station_name: string
  river: string
  current_level: {
    timestamp: string
    height_m: number
  }
  tendency: Tendency
  rate_cm_per_hour: number
  distance_km: number
  delay_hours: number
  weight: number
  threshold_yellow: number
  threshold_orange: number
  threshold_red: number
  lat?: number
  lon?: number
}

export interface MultiStationData {
  timestamp: string
  primary_station_id: string
  stations: StationData[]
}

export interface NetatmoStation {
  city: string
  distance_km: number
  rain_1h_mm: number | null
  rain_24h_mm: number | null
  rain_72h_mm?: number | null
  rain_7d_mm?: number | null
  lat?: number
  lon?: number
  is_lidoire?: boolean
  position?: 'amont' | 'milieu' | 'aval'
}

export interface NetatmoData {
  available: boolean
  timestamp?: string
  stations_count: number
  rain_1h_avg_mm: number
  rain_24h_avg_mm: number
  closest_station?: NetatmoStation
  lidoire_stations?: NetatmoStation[]
  stations?: NetatmoStation[]
  /** OAuth start path, returned when no account is linked */
  auth_url?: string
}

export interface InfoclimatStation {
  name: string
  distance_km: number
  source: string
  departement?: string
  lat?: number
  lon?: number
}

export interface InfoclimatRainfallStation {
  station_id: string
  station_name: string
  distance_km: number
  rain_24h_mm: number
}

export interface InfoclimatRainfall {
  available: boolean
  reason?: string
  stations_count?: number
  rain_24h_avg_mm?: number
  stations?: InfoclimatRainfallStation[]
}

export interface InfoclimatData {
  stations_found: number
  nearest_stations: InfoclimatStation[]
  rainfall: InfoclimatRainfall
}

export interface ForecastPoint {
  timestamp: string
  hours_ahead: number
  score: number
  level: RiskLevel
  rain_1h_mm: number
  rain_24h_mm: number
  rain_72h_mm: number
  tide_coefficient: number
  hours_to_high_tide: number
  projected_level_m: number
  tide_available?: boolean
  beyond_tide_horizon?: boolean
}

/** High tide estimated at the Lidoire confluence (forecast payload) */
export interface ForecastHighTide {
  timestamp: string
  coefficient: number | null
  estimated: boolean
}

export interface ForecastData {
  generated_at: string
  hours_ahead: number
  forecasts: ForecastPoint[]
  max_risk: number
  max_level: RiskLevel
  critical_periods: ForecastPoint[]
  summary: string
  data_sources: {
    rain: { status: string; source?: string; hours?: number; error?: string }
    tide: { status: string; port?: string; error?: string }
    river: { status: string; stations?: number; error?: string }
    soil_wind: { status: string; error?: string }
  }
  model_version?: string
  tide_horizon?: string | null
  high_tides?: ForecastHighTide[]
  inputs_summary?: Record<string, unknown>
}

export interface SoilConditions {
  soil_moisture: number | null
  moisture_deep: number | null
  soil_temperature_0cm: number | null
  et0_mm: number | null
  soil_saturation_factor: number
  precip_probability: number | null
  water_balance_30d_mm: number | null
  surplus_deficit_30d_pct: number | null
  deficit_12m_mm: number | null
}

export interface WindData {
  wind_speed_kmh: number
  wind_direction_deg: number
  westerly_wind_kmh: number
}

export interface NowcastRainData {
  timestamp: string
  commune: string
  total_60min_mm: number
  max_intensity_mm: number
  rain_minutes: number
  minute_data: { minute: string; mm: number }[]
}

export interface MeteoAgricoleData {
  timestamp: string
  commune: string
  rain_min_mm: number
  rain_max_mm: number
  rain_probability_pct: number
  wind_avg_kmh: number
  wind_gust_kmh: number
  wind_direction: string
  precip_30d_mm: number | null
  surplus_deficit_30d_pct: number | null
  deficit_12m_mm: number | null
  rain_forecast_3d_mm: number
  rain_forecast_7d_mm: number
}

export interface RealtimeData {
  timestamp: string
  risk: RiskScore
  tide: TideForecast
  river: RiverLevelForecast
  rainfall: RainfallSummary
  video: VideoAnalysis | null
  alerts: string[]
  stations?: MultiStationData
  netatmo_available?: boolean
  soil?: SoilConditions
  wind?: WindData
  meteo_agricole?: MeteoAgricoleData
  nowcast_rain?: NowcastRainData
}

// Ground-truth outcome at the mill (null on an episode = not labelled yet)
export type EpisodeOutcome = 'flooded' | 'false_positive' | 'near_miss' | 'unknown'

export interface FloodEpisode {
  id: number | null
  start_time: string
  peak_time: string | null
  end_time: string | null
  severity: number
  description: string | null
  notes: string | null
  created_at: string | null
  outcome?: EpisodeOutcome | null
  observed_at?: string | null // observed peak time at the mill
  water_height_cm?: number | null
  outcome_source?: string | null // who/what labelled the episode
  // Derived by the API: 'ongoing' only while recent and live data flows
  status?: 'ongoing' | 'closed' | null
}

export interface EpisodeLabel {
  key: string // peak date YYYY-MM-DD
  peak_time: string
  // Half-width of the uncertainty on peak_time (h): 0.5-2 = hour precise, 12 = day
  peak_time_precision_hours?: number | null
  outcome: EpisodeOutcome
  severity: number
  water_height_cm: number | null
  source: string
  notes: string | null
  flags: string[]
  legacy_db_id: number | null
  legacy_refs: string[]
  legacy_stored_score: number | null
}

export interface EpisodeLabelSet {
  version: number
  updated_at: string
  description: string
  exhaustive: boolean
  period_start: string | null
  period_end: string | null
  caveats: string[]
  inconsistencies: string[]
  episodes: EpisodeLabel[]
}

export interface EpisodeContext {
  rain_24h_before_mm: number
  rain_48h_before_mm: number
  rain_72h_before_mm: number
  max_dordogne_level_m: number
  // null when no tide data covers the episode (unknown)
  max_tide_coefficient: number | null
  tide_phase_at_peak: string | null
  hours_since_high_tide: number | null
}

export interface EpisodeAnalysis {
  episode: FloodEpisode
  context: EpisodeContext
  tides: TideData[]
  river_levels: RiverLevel[]
  rainfall: unknown[]
  video_data: VideoAnalysis[]
  matched_criteria: string[]
}

// Unified Timeline Types (Multi-Source Comparison)
export interface TimelineDataPoint {
  timestamp: string
  value: number
}

export interface TimelineSeries {
  type: 'river_level' | 'rainfall' | 'rainfall_cumul' | 'tide' | 'risk_score' | 'risk_score_shadow' | 'soil_moisture' | 'soil_temperature' | 'evapotranspiration' | 'wind_speed' | 'wind_westerly' | 'forecast_risk_score' | 'forecast_rainfall' | 'forecast_level' | IndicatorSeriesType
  name: string
  unit: string
  color: string
  data: TimelineDataPoint[]
}

export interface UnifiedTimelineData {
  start: string
  end: string
  resolution: string
  series: Record<string, TimelineSeries>
}

export interface RiverStationInfo {
  id: string
  name: string
  river: string
  color: string
  distance_km: number
  delay_hours: number
}

export interface RainfallStationInfo {
  id: string
  name: string
  position: 'amont' | 'milieu' | 'aval'
  color: string
  /** False while the source is not collecting (Netatmo not authorised) */
  available?: boolean
}

// Hydrological indicators (outside the risk model v3.4): flows, groundwater, observed tide
export type IndicatorSeriesType = 'flow_tributary' | 'flow_dordogne' | 'groundwater' | 'tide_observed'

export interface IndicatorSeriesInfo {
  id: string // "flow:EYRAUD", "gw:MOULIETS", "tide_obs:BORDEAUX"
  kind: 'flow' | 'groundwater' | 'tide_observed'
  type: IndicatorSeriesType
  name: string
  unit: string
  color: string
  role?: string | null
  distance_km: number
}

export interface AvailableSources {
  river_stations: RiverStationInfo[]
  rainfall_stations: RainfallStationInfo[]
  /** Absent on older backends */
  indicator_series?: IndicatorSeriesInfo[]
  has_tide: boolean
  has_risk_score: boolean
  has_infoclimat: boolean
  has_wind: boolean
  has_soil: boolean
}

// Basin Rainfall (aggregated Lidoire basin)
export interface BasinRainfallData {
  timestamp: string
  /** False when no Netatmo basin station reported: the averages are 0.0 placeholders */
  available?: boolean
  stations_count?: number
  basin_average_1h_mm: number
  basin_average_24h_mm: number
  weighted_average_24h_mm: number
  by_position: Record<string, { total: number; count: number; avg: number }>
  sparkline_24h: { timestamp: string; value: number }[]
}

// /api/status: real dependency checks (database/influxdb/redis)
export type DependencyState = 'connected' | 'error' | 'timeout'

export interface DependencyCheck {
  status: DependencyState
  latency_ms: number
  error?: string
}

export type DependencyName = 'database' | 'influxdb' | 'redis'

// /api/status `sources`: newest stored point of each data source
export type SourceFreshnessState = 'ok' | 'stale' | 'unavailable' | 'unknown'

export type SourceFreshnessName =
  | 'river_primary'
  | 'rain_openmeteo'
  | 'infoclimat'
  | 'netatmo'
  | 'tide'
  | 'soil'
  | 'risk_score'

export interface SourceFreshness {
  label: string
  status: SourceFreshnessState
  last_timestamp: string | null // naive UTC
  age_hours: number | null // negative = point ahead of now (tide forecasts)
  max_age_hours: number
  critical: boolean // stale/unavailable => status "degraded"
  note?: string
  error?: string
}

export interface SystemStatus {
  status: 'healthy' | 'degraded'
  timestamp: string
  version: string
  location: { lat: number; lon: number; name: string }
  services: {
    database: DependencyState
    influxdb: DependencyState
    redis: DependencyState
    checks: Partial<Record<DependencyName, DependencyCheck>>
    collectors: Record<string, string> // static declaration (compatibility)
  }
  sources?: Partial<Record<SourceFreshnessName, SourceFreshness>>
}

// POST /api/risk/simulate
export interface RiskSimulationResult {
  simulated_values: Record<string, number>
  risk: RiskScore
  model_version: string
}

// /api/sources-detail (exhaustive config, display only)
export interface SourcesDetailRiverStation {
  id: string
  name: string
  river: string
  distance_km: number
  delay_hours: number
  weight: number
  lat?: number | null
  lon?: number | null
  thresholds?: { yellow?: number; orange?: number; red?: number }
}

export interface SourcesDetailNetatmoStation {
  city: string
  lat: number
  lon: number
  position: string
}

export interface SourcesDetailPoint {
  id?: string
  name: string
  lat: number
  lon: number
}

export interface SourcesDetailLevels {
  yellow: number
  orange: number
  red: number
}

export interface SourcesDetail {
  location?: { name: string; lat: number; lon: number }
  primary_station?: string
  sources?: {
    river?: { name: string; description: string; api_url: string; stations?: SourcesDetailRiverStation[] }
    netatmo?: {
      name: string
      description: string
      api_url?: string
      configured: boolean
      search_radius_km: number
      priority_stations?: SourcesDetailNetatmoStation[]
    }
    infoclimat?: {
      name: string
      description: string
      configured: boolean
      search_radius_km: number
      max_stations: number
      note?: string
    }
    open_meteo?: {
      name: string
      description: string
      api_forecast: string
      api_archive: string
      fields?: string[]
      basin_points?: SourcesDetailPoint[]
    }
    tide?: {
      name: string
      description: string
      ports?: SourcesDetailPoint[]
      reference_point?: SourcesDetailPoint
    }
    meteo_agricole?: { name: string; description: string; url?: string; fields?: string[] }
    camera?: { name: string; description: string; configured: boolean }
  }
  risk_model?: {
    version?: string // risk_core.MODEL_VERSION
    weights: Record<string, string>
    conjunction_multiplier?: string
    stability_dampener?: string
    thresholds?: {
      rain_24h?: SourcesDetailLevels
      dordogne_level?: SourcesDetailLevels
      tide_coef_high?: number | null
    }
  }
}

// Probabilistic forecasts (GET /api/forecast/ensemble) - informative, not in the v3.4 score
export interface EnsembleWindowStats {
  p10: number
  p50: number
  p90: number
  mean: number
  max: number
  members: number
  /** Share of members above each threshold (keys: threshold in mm, e.g. "25") */
  prob: Record<string, number>
}

export interface EnsembleHorizon {
  start: string
  end: string
  lead_start_h: number
  lead_end_h: number
  models: Record<string, EnsembleWindowStats>
  blend: { prob: Record<string, number>; models: string[] }
}

export interface EnsembleModelInfo {
  id: string
  label: string
  available: boolean
  members?: number
  run_init?: string
  run_init_known?: boolean
  horizon_end?: string | null
}

export interface EnsembleRain {
  available: boolean
  note?: string
  thresholds?: Record<string, number[]>
  issued_at?: string
  models?: EnsembleModelInfo[]
  horizons?: Record<string, EnsembleHorizon[]>
  attribution?: string
}

export interface GlofasDay {
  date: string
  lead_days: number
  p10: number
  p50: number
  p90: number
  min: number
  max: number
  mean: number
  members: number
  prob_gt_p95: number | null
  prob_gt_p99: number | null
}

export interface GlofasForecast {
  available: boolean
  note?: string
  stale?: boolean
  run?: string
  run_known?: boolean
  members?: number
  climatology?: { p50: number; p90: number; p95: number; p99: number; max: number; first_day?: string; last_day?: string } | null
  days?: GlofasDay[]
  max_prob_gt_p95_10d?: { date: string; value: number } | null
  max_prob_gt_p99_10d?: { date: string; value: number } | null
  attribution?: string
}

export interface EnsembleForecast {
  rain: EnsembleRain
  glofas: GlofasForecast
  note?: string
}

// Observed basin rain, OPERA radar (GET /api/realtime/radar)
export interface RadarWindow {
  mm: number | null
  max_pixel_mm: number | null
  hours_found: number
  hours_expected: number
  complete: boolean
}

export interface RadarBasinRain {
  available: boolean
  note?: string
  last_image_time: string | null
  age_minutes: number | null
  windows: Record<string, RadarWindow>
  attribution: string
  licence_url?: string
}

// Lidoire basin polygon (GET /api/geo/basin)
export interface BasinProperties {
  name: string
  area_km2: number
  source: string
  source_url: string
  licence: string
  attribution: string
}

export interface BasinGeoJson {
  type: 'FeatureCollection'
  features: {
    type: 'Feature'
    properties: BasinProperties
    geometry: { type: 'Polygon'; coordinates: number[][][] }
  }[]
}

// Outlook (GET /api/risk/outlook): "do I need to act, and when?"
export interface OutlookDriver {
  factor: string
  label: string
  /** Points of the 0-100 score brought by this factor */
  points: number
  text: string
}

export interface OutlookHorizon {
  hours: number
  label: string
  max_score: number
  level: RiskLevel
  max_at: string
  max_hours_ahead: number
  /** First hour at or above `level` */
  first_at_level: string
  drivers: OutlookDriver[]
  modifiers: string[]
  unavailable: string[]
  summary: string
}

export interface OutlookHighTide {
  timestamp: string
  coefficient: number | null
  estimated: boolean
  hours_from_now: number
}

export interface OutlookConfidence {
  level: 'good' | 'reduced' | 'low'
  notes: string[]
  unavailable_factors: string[]
  tide_estimated: boolean
  stale_sources: string[]
}

export interface OutlookChecklist {
  level: RiskLevel
  title: string
  actions: string[]
  act_before: string | null
}

export interface RiskOutlook {
  generated_at: string
  forecast_generated_at: string
  model_version: string | null
  horizons: OutlookHorizon[]
  highest_level: RiskLevel
  next_high_tide: OutlookHighTide | null
  confidence: OutlookConfidence
  checklist: OutlookChecklist
  inputs_summary: Record<string, unknown>
}

// Forecast skill (GET /api/risk/forecast-skill): archived forecast vs stored realtime score
export interface ForecastSkillPair {
  run_at: string
  lead_hours: number
  valid_at: string
  forecast_score: number
  actual_score: number
  actual_at: string
}

export interface ForecastSkillLead {
  lead_hours: number
  n: number
  mae: number | null
  bias: number | null
  hits: number
  misses: number
  false_alarms: number
  correct_negatives: number
  pod: number | null
  far: number | null
  csi: number | null
}

export interface ForecastSkill {
  generated_at: string
  model_version: string
  threshold: number
  threshold_level: string
  days: number
  lead_hours: number[]
  tolerance_minutes: number
  n_runs: number
  first_run_at: string | null
  last_run_at: string | null
  by_lead: ForecastSkillLead[]
  pairs: ForecastSkillPair[]
  notes: string[]
}

// Official vigilance (GET /api/vigilance): informative, not a model input.
// Levels: 1 vert, 2 jaune, 3 orange, 4 rouge (null = unknown)
export type VigilanceLevel = 1 | 2 | 3 | 4
export type VigilanceColor = 'vert' | 'jaune' | 'orange' | 'rouge'

export interface VigilanceWindow {
  begin: string
  end: string
  level: VigilanceLevel
}

export interface VigilanceTroncon {
  code: string
  name: string
  level: VigilanceLevel | null
  color: VigilanceColor | null
  url: string
  station_url: string | null
}

export interface VigilancePhenomenon {
  id: string
  key: string
  label: string
  level: VigilanceLevel | null
  color: VigilanceColor | null
  level_tomorrow: VigilanceLevel | null
  windows: VigilanceWindow[]
}

export interface VigilanceDepartment {
  code: string
  name: string
  max_level: VigilanceLevel | null
  phenomena: VigilancePhenomenon[]
  url: string
}

export interface VigilanceStatus {
  timestamp: string
  vigicrues: {
    available: boolean
    issued_at: string | null
    fetched_at: string | null
    troncons: VigilanceTroncon[]
    url: string
  }
  meteofrance: {
    available: boolean
    issued_at: string | null
    fetched_at: string | null
    valid_from: string | null
    valid_to: string | null
    departments: VigilanceDepartment[]
    url: string
  }
  note: string
}

// Field observations at the mill (ground truth from the owner)
export type ObservationKind = 'flooded' | 'water_rising' | 'near_miss' | 'normal'

export interface ObservationInput {
  observed_at?: string | null
  kind: ObservationKind
  water_height_cm?: number | null
  reference?: string | null
  note?: string | null
  source?: string
}

export interface Observation {
  id: number
  observed_at: string
  kind: ObservationKind
  water_height_cm: number | null
  reference: string | null
  note: string | null
  has_photo: boolean
  photo_url: string | null
  created_at: string
  source: string
}

export interface OutcomeSuggestion {
  outcome: EpisodeOutcome
  observed_at: string
  water_height_cm: number | null
  observation_id: number
  reason: string
}

export interface EpisodeObservations {
  episode_id: number
  window_start: string
  window_end: string
  observations: Observation[]
  suggested_outcome: OutcomeSuggestion | null
}

// /api/indicators (stored series only, never a model input)
export interface IndicatorThresholdStatus {
  exceeded: string[]
  next: string | null
  next_value: number | null
  distance: number | null
  ratio_to_next: number | null
}

export type IndicatorTrend = 'rising' | 'falling' | 'stable'

interface IndicatorBase {
  id: string
  key: string
  name: string
  distance_km?: number | null
  available: boolean
  last_timestamp: string | null
  age_hours: number | null
  stale: boolean | null
  percentile: number | null
  percentile_label: string | null
  percentile_basis: string
  history_days: number
}

export interface FlowIndicator extends IndicatorBase {
  station_id: string
  river: string | null
  role: string | null
  basin_km2: number | null
  q_m3s: number | null
  q_mean_24h_m3s: number | null
  trend_m3s_per_h: number | null
  trend: IndicatorTrend | null
  thresholds_daily_m3s: Record<string, number>
  threshold: IndicatorThresholdStatus
}

export interface GroundwaterIndicator extends IndicatorBase {
  code_bss: string
  aquifer: string | null
  level_m_ngf: number | null
  change_24h_m: number | null
  trend: IndicatorTrend | null
  thresholds_m_ngf: Record<string, number>
  threshold: IndicatorThresholdStatus
}

export interface TideObservedIndicator extends IndicatorBase {
  source: string | null
  datum: string | null
  height_m: number | null
  max_24h_m: number | null
}

export interface IndicatorsData {
  timestamp: string
  model_input: false
  note: string
  runoff: FlowIndicator[]
  dordogne: FlowIndicator[]
  groundwater: GroundwaterIndicator[]
  tide_observed: TideObservedIndicator[]
}

// --- Lidoire long profile, episode drivers, analogs, radar frame (display only) ---

export interface LidoireProfilePoint {
  d_m: number // distance from the confluence, upstream
  z_m: number // low ground of a +/- 30 m cross-section (m NGF)
  z_line_m: number | null
  lon: number
  lat: number
}

export interface ConfluenceConfig {
  label: 'estime'
  upstream_station: string
  downstream_station: string
  ngf_zeros_m: Record<string, number>
  alpha_downstream_weight: number
  envelope_hours: number
  formula: string
}

export interface BackwaterReach {
  label: string
  status: 'unknown' | 'none' | 'partial' | 'whole_profile'
  distance_m: number | null
  connected_distance_m: number | null
  disconnected_low_points: boolean | null
  lon: number | null
  lat: number | null
  z_m: number | null
  distance_below_mill_m: number | null
  connected_distance_below_mill_m: number | null
  reaches_mill: boolean | null
}

export interface LidoireProfileData {
  at: string
  model_input: false
  profile: {
    metadata: Record<string, unknown>
    length_m: number | null
    z_range_m: [number, number] | null
    points: LidoireProfilePoint[] | null
  }
  mill: {
    lon: number
    lat: number
    d_m: number
    ground_z_m_ngf: number
    ground_z_source: string
    floor_z_m_ngf: number | null
  }
  confluence: ConfluenceConfig & {
    lon: number
    lat: number
    d_m: number
    ground_z_m: number
    z_c_m_ngf: number | null
    inputs: {
      pessac_max_m: number | null
      pessac_max_m_ngf: number | null
      libourne_max_m: number | null
      libourne_max_m_ngf: number | null
    }
    instant: {
      z_c_m_ngf: number | null
      pessac_m: number | null
      pessac_time: string | null
      libourne_m: number | null
      libourne_time: string | null
      notes: string[]
    }
    notes: string[]
  }
  backwater: BackwaterReach & { method: string; limits: string[]; instant: BackwaterReach }
  margin: { mill_ground_minus_z_c_m: number | null; label: string; note: string }
  labels: { z_c: string; backwater: string }
  errors: string[]
}

export type DriverAxis = 'pessac' | 'eyraud_q' | 'rain_72h' | 'piezo' | 'glofas'

export interface DriverValues {
  pessac_m: number | null
  libourne_m: number | null
  eyraud_q_m3s: number | null
  rain_72h_mm: number | null
  rain_72h_hours_found: number
  piezo_m_ngf: number | null
  piezo_time: string | null
  glofas_m3s: number | null
  z_c_m_ngf: number | null
  notes: string[]
}

export interface EpisodeDriversEntry {
  key: string
  peak_time: string
  outcome: EpisodeOutcome
  severity: number
  water_height_cm: number | null
  notes: string | null
  flags: string[]
  values: DriverValues
  percentiles: Record<DriverAxis, number | null>
  errors: string[]
}

export interface EpisodeDriversData {
  generated_at: string
  model_input: false
  labels_version: number
  labels_updated_at: string
  windows: Record<string, string>
  percentile_basis: Record<string, string>
  history_counts: Record<DriverAxis, number>
  confluence: ConfluenceConfig
  caveats: string[]
  partial: boolean
  errors: string[]
  episodes: EpisodeDriversEntry[]
}

export interface RiskAnalog {
  key: string
  peak_time: string
  outcome: EpisodeOutcome
  severity: number
  water_height_cm: number | null
  notes: string | null
  distance: number | null
  close: boolean
  axes_used: DriverAxis[]
  percentiles: Partial<Record<DriverAxis, number | null>>
  percentile_diff: Partial<Record<DriverAxis, number | null>>
  values: DriverValues
  observations: { count: number; with_photo: number } | null
}

export interface RiskAnalogsData {
  timestamp: string
  model_input: false
  k: number
  axes: DriverAxis[]
  method: string
  threshold: number
  nothing_close: boolean
  nearest_distance: number | null
  message: string
  situation: {
    timestamp: string
    values: DriverValues
    percentiles: Record<DriverAxis, number | null>
    errors: string[]
  }
  windows: Record<string, string>
  percentile_basis: Record<string, string>
  observation_window_hours: number
  observations_available: boolean
  labels_partial: boolean
  analogs: RiskAnalog[]
}

export interface RadarFrameMeta {
  available: boolean
  time: string
  accumulation_minutes: number
  accumulation_start: string
  product: string
  source: string
  attribution: string
  licence_url: string
  bbox: [number, number, number, number] // west, south, east, north
  bbox_order: string
  crs: string
  width: number
  height: number
  pixel_deg: number
  url: string
  legend: { min_mm: number; rgba: [number, number, number, number] }[]
  no_coverage_rgba: [number, number, number, number]
  unit: string
  model_input: false
  coverage: number
  max_mm: number | null
  rain_pixels: number
}
