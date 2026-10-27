// API client for Lidoire Flood Watch backend

import type {
  RealtimeData,
  FloodEpisode,
  EpisodeAnalysis,
  EpisodeLabelSet,
  EpisodeOutcome,
  RiskScore,
  MultiStationData,
  NetatmoData,
  InfoclimatData,
  ForecastData,
  AvailableSources,
  UnifiedTimelineData,
  BasinRainfallData,
  RiskSimulationResult,
  SystemStatus,
  SourcesDetail,
  RiskOutlook,
  ForecastSkill,
  VigilanceStatus,
  Observation,
  ObservationInput,
  EpisodeObservations,
  IndicatorsData,
  EnsembleForecast,
  RadarBasinRain,
  BasinGeoJson,
  LidoireProfileData,
  EpisodeDriversData,
  RiskAnalogsData,
  RadarFrameMeta,
} from './types'

const API_BASE = '/api'

// API key for mutating endpoints, stored per browser
export const API_KEY_STORAGE_KEY = 'lfw_api_key'

export function getApiKey(): string | null {
  try {
    return window.localStorage.getItem(API_KEY_STORAGE_KEY)
  } catch {
    // localStorage unavailable (private mode, blocked storage)
    return null
  }
}

export function setApiKey(key: string | null): void {
  try {
    if (key) window.localStorage.setItem(API_KEY_STORAGE_KEY, key)
    else window.localStorage.removeItem(API_KEY_STORAGE_KEY)
  } catch {
    // localStorage unavailable: nothing to persist
  }
}

/** Headers to add to a request: X-API-Key on every non-GET method. */
export function authHeaders(method: string = 'GET'): Record<string, string> {
  const upper = method.toUpperCase()
  if (upper === 'GET' || upper === 'HEAD') return {}
  const key = getApiKey()
  return key ? { 'X-API-Key': key } : {}
}

/** Error thrown by every API call: HTTP status plus the parsed FastAPI `detail`. */
export class ApiError extends Error {
  readonly status: number
  readonly detail: unknown

  constructor(status: number, message: string, detail: unknown = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }

  /** 401/403: API key missing or wrong (see Configuration > Cle API). */
  get isAuthError(): boolean {
    return this.status === 401 || this.status === 403
  }
}

export function isAuthError(err: unknown): boolean {
  return err instanceof ApiError && err.isAuthError
}

/** True when the error comes from an aborted request (superseded fetch). */
export function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError'
}

/** Human readable message for any error raised by an API call. */
export function errorMessage(err: unknown): string {
  if (err instanceof Error) return err.message
  return String(err)
}

/** Error of a user action, as shown by components/ActionError. */
export interface ActionErrorState {
  message: string
  /** 401/403: show the link to Configuration > Cle API */
  auth: boolean
}

export function toActionError(err: unknown, context?: string): ActionErrorState {
  const message = errorMessage(err)
  return {
    message: context ? `${context} : ${message}` : message,
    auth: isAuthError(err),
  }
}

/** Render a FastAPI `detail` (string, {error, ...} object or validation list). */
function describeDetail(detail: unknown): string | null {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const msgs = detail
      .map((d) => (d && typeof d === 'object' && 'msg' in d ? String((d as { msg: unknown }).msg) : null))
      .filter((m): m is string => !!m)
    return msgs.length > 0 ? msgs.join('; ') : null
  }
  if (detail && typeof detail === 'object') {
    const obj = detail as { error?: unknown; unknown?: unknown }
    const parts: string[] = []
    if (typeof obj.error === 'string') parts.push(obj.error)
    if (Array.isArray(obj.unknown) && obj.unknown.length > 0) {
      parts.push(`: ${obj.unknown.join(', ')}`)
    }
    return parts.length > 0 ? parts.join('') : null
  }
  return null
}

async function buildApiError(response: Response): Promise<ApiError> {
  let detail: unknown = null
  try {
    const body: unknown = await response.json()
    if (body && typeof body === 'object' && 'detail' in body) {
      detail = (body as { detail: unknown }).detail
    }
  } catch {
    // Non-JSON error body (proxy error page, empty body)
  }
  if (response.status === 401 || response.status === 403) {
    return new ApiError(
      response.status,
      `Clé API manquante ou invalide (HTTP ${response.status}) : renseignez-la dans Configuration > Clé API`,
      detail,
    )
  }
  const described = describeDetail(detail)
  return new ApiError(
    response.status,
    `Erreur API ${response.status}${described ? ` : ${described}` : ` ${response.statusText}`}`,
    detail,
  )
}

async function fetchResponse(url: string, options?: RequestInit): Promise<Response> {
  const response = await fetch(url, {
    ...options,
    cache: 'no-store',
    headers: {
      'Content-Type': 'application/json',
      ...authHeaders(options?.method),
      ...options?.headers,
    },
  })
  if (!response.ok) {
    throw await buildApiError(response)
  }
  return response
}

async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetchResponse(url, options)
  if (response.status === 204) return undefined as T
  return response.json()
}

const NETATMO_AUTH_PATH = `${API_BASE}/auth/netatmo`

/**
 * Netatmo OAuth start link. The start endpoint is a plain browser navigation
 * (no X-API-Key header possible), so the stored key goes in ?api_key=.
 * Only same-origin API paths are accepted (the key never leaves the backend
 * origin); anything else falls back to the default start path.
 */
export function netatmoAuthUrl(authUrl?: string | null): string {
  const base = authUrl && authUrl.startsWith(`${API_BASE}/`) ? authUrl : NETATMO_AUTH_PATH
  const key = getApiKey()
  if (!key) return base
  const sep = base.includes('?') ? '&' : '?'
  return `${base}${sep}api_key=${encodeURIComponent(key)}`
}

// Realtime data
export async function getRealtimeData(): Promise<RealtimeData> {
  return fetchJson<RealtimeData>(`${API_BASE}/realtime`)
}

// Episodes
export async function getEpisodes(
  limit: number = 50,
  offset: number = 0
): Promise<FloodEpisode[]> {
  return fetchJson<FloodEpisode[]>(
    `${API_BASE}/episodes?limit=${limit}&offset=${offset}`
  )
}

export async function getEpisode(id: number): Promise<FloodEpisode> {
  return fetchJson<FloodEpisode>(`${API_BASE}/episodes/${id}`)
}

export async function createEpisode(
  episode: Omit<FloodEpisode, 'id' | 'created_at'>
): Promise<FloodEpisode> {
  return fetchJson<FloodEpisode>(`${API_BASE}/episodes`, {
    method: 'POST',
    body: JSON.stringify(episode),
  })
}

export async function analyzeEpisode(
  date: string,
  windowBefore: number = 3,
  windowAfter: number = 2
): Promise<EpisodeAnalysis> {
  const params = new URLSearchParams({
    date,
    window_before_days: windowBefore.toString(),
    window_after_days: windowAfter.toString(),
  })

  return fetchJson<EpisodeAnalysis>(`${API_BASE}/episodes/analyze?${params}`, {
    method: 'POST',
  })
}

export async function analyzeExistingEpisode(id: number): Promise<EpisodeAnalysis> {
  return fetchJson<EpisodeAnalysis>(`${API_BASE}/episodes/analyze/${id}`)
}

// Risk
export async function getCurrentRisk(): Promise<RiskScore> {
  return fetchJson<RiskScore>(`${API_BASE}/risk`)
}

export async function simulateRisk(params: {
  rain_24h_mm: number
  rain_72h_mm: number
  dordogne_level_m: number
  tide_coefficient: number
  hours_to_high_tide: number
}): Promise<RiskSimulationResult> {
  const searchParams = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    searchParams.set(key, value.toString())
  })

  return fetchJson<RiskSimulationResult>(`${API_BASE}/risk/simulate?${searchParams}`, {
    method: 'POST',
  })
}

// Status (real dependency checks)
export async function getStatus(): Promise<SystemStatus> {
  return fetchJson<SystemStatus>(`${API_BASE}/status`)
}

// Sources detail (exhaustive config)
export async function getSourcesDetail(): Promise<SourcesDetail> {
  return fetchJson<SourcesDetail>(`${API_BASE}/sources-detail`)
}

// Stations
export async function getStations(): Promise<MultiStationData> {
  return fetchJson<MultiStationData>(`${API_BASE}/realtime/stations`)
}

// Netatmo
export async function getNetatmoData(): Promise<NetatmoData> {
  return fetchJson<NetatmoData>(`${API_BASE}/realtime/netatmo`)
}

// Infoclimat
export async function getInfoclimatData(): Promise<InfoclimatData> {
  return fetchJson<InfoclimatData>(`${API_BASE}/realtime/infoclimat`)
}

// Forecast
export async function getForecast(hours: number = 168): Promise<ForecastData> {
  return fetchJson<ForecastData>(`${API_BASE}/risk/forecast?hours=${hours}`)
}

// Outlook 6 / 24 / 72 h (max, why, checklist)
export async function getRiskOutlook(): Promise<RiskOutlook> {
  return fetchJson<RiskOutlook>(`${API_BASE}/risk/outlook`)
}

// Archived forecasts vs realized realtime score
export async function getForecastSkill(
  leadHours: number[] = [6, 24, 72],
  days: number = 30,
): Promise<ForecastSkill> {
  const params = new URLSearchParams({ lead_hours: leadHours.join(','), days: String(days) })
  return fetchJson<ForecastSkill>(`${API_BASE}/risk/forecast-skill?${params}`)
}

// Timeline Sources
export async function getAvailableSources(): Promise<AvailableSources> {
  return fetchJson<AvailableSources>(`${API_BASE}/timeline/sources`)
}

// Unified Timeline (River + Rainfall + Tide + Risk)
export interface UnifiedTimelineResult {
  data: UnifiedTimelineData
  /** Backfills still running server side (X-Backfill-Pending): data is partial, retry later. */
  backfillPending: string[]
}

export async function getUnifiedTimeline(
  start?: string,
  end?: string,
  riverStations?: string[],
  rainfallSources?: string[],
  includeTide?: boolean,
  includeRisk?: boolean,
  resolution: string = '1h',
  includeSoil?: boolean,
  includeInfoclimat?: boolean,
  includeWind?: boolean,
  includeForecast?: boolean,
  signal?: AbortSignal,
  includeRiskShadow?: boolean,
  indicatorSeries?: string[],
  includeRiskShadowV4?: boolean,
): Promise<UnifiedTimelineResult> {
  const params = new URLSearchParams({ resolution })
  if (start) params.set('start', start)
  if (end) params.set('end', end)
  if (riverStations && riverStations.length > 0) {
    params.set('river_stations', riverStations.join(','))
  }
  if (rainfallSources && rainfallSources.length > 0) {
    params.set('rainfall_sources', rainfallSources.join(','))
  }
  if (includeTide) params.set('include_tide', 'true')
  if (includeRisk) params.set('include_risk', 'true')
  if (includeSoil) params.set('include_soil', 'true')
  // Infoclimat: default true on backend, send false to disable
  if (includeInfoclimat === false) params.set('include_infoclimat', 'false')
  if (includeWind) params.set('include_wind', 'true')
  if (includeForecast) params.set('include_forecast', 'true')
  if (includeRiskShadow) params.set('include_risk_shadow', 'true')
  if (includeRiskShadowV4) params.set('include_risk_shadow_v4', 'true')
  if (indicatorSeries && indicatorSeries.length > 0) {
    params.set('indicators', indicatorSeries.join(','))
  }

  const response = await fetchResponse(`${API_BASE}/timeline/unified?${params}`, { signal })
  const data: UnifiedTimelineData = await response.json()
  const pendingHeader = response.headers.get('X-Backfill-Pending')
  const backfillPending = pendingHeader
    ? pendingHeader.split(',').map((p) => p.trim()).filter(Boolean)
    : []
  return { data, backfillPending }
}

// Basin Rainfall
export async function getBasinRainfall(): Promise<BasinRainfallData> {
  return fetchJson<BasinRainfallData>(`${API_BASE}/realtime/basin-rainfall`)
}

// TimeMachine - get realtime data at a historical timestamp
export async function getRealtimeDataAt(timestamp: string): Promise<RealtimeData> {
  const params = new URLSearchParams({ timestamp })
  return fetchJson<RealtimeData>(`${API_BASE}/realtime/at?${params}`)
}

// Episodes list (for TimeMachine quick navigation)
export async function getEpisodesList(): Promise<FloodEpisode[]> {
  return fetchJson<FloodEpisode[]>(`${API_BASE}/episodes?limit=20`)
}

// Update episode
export async function updateEpisode(
  id: number,
  episode: Partial<FloodEpisode>
): Promise<FloodEpisode> {
  return fetchJson<FloodEpisode>(`${API_BASE}/episodes/${id}`, {
    method: 'PUT',
    body: JSON.stringify(episode),
  })
}

// Delete episode
export async function deleteEpisode(id: number): Promise<void> {
  await fetchJson<unknown>(`${API_BASE}/episodes/${id}`, { method: 'DELETE' })
}

// Set the ground-truth outcome of an episode (re-confirming replaces it)
export interface ConfirmEpisodeOptions {
  severity?: number
  notes?: string // replaces existing notes when provided
  observedAt?: string | null // ISO datetime of the observed peak at the mill
  waterHeightCm?: number | null
  source?: string
}

export async function confirmEpisode(
  id: number,
  outcome: EpisodeOutcome,
  options: ConfirmEpisodeOptions = {}
): Promise<{ status: string; outcome: EpisodeOutcome; episode: FloodEpisode }> {
  const params = new URLSearchParams({ outcome })
  if (options.severity) params.set('actual_severity', options.severity.toString())
  if (options.notes !== undefined) params.set('notes', options.notes)
  if (options.observedAt) params.set('observed_at', options.observedAt)
  if (options.waterHeightCm !== undefined && options.waterHeightCm !== null)
    params.set('water_height_cm', options.waterHeightCm.toString())
  if (options.source) params.set('source', options.source)
  return fetchJson(`${API_BASE}/episodes/confirm/${id}?${params}`, {
    method: 'POST',
  })
}

// Reference labels of the annotated episodes (ground truth for evaluation)
export async function getEpisodeLabels(): Promise<EpisodeLabelSet> {
  return fetchJson<EpisodeLabelSet>(`${API_BASE}/episodes/labels`)
}

// Probabilistic forecasts: ensemble rain + GloFAS (informative, not in the score)
export async function getEnsembleForecast(): Promise<EnsembleForecast> {
  return fetchJson<EnsembleForecast>(`${API_BASE}/forecast/ensemble`)
}

// Observed basin rain from the OPERA radar
export async function getRadarBasinRain(): Promise<RadarBasinRain> {
  return fetchJson<RadarBasinRain>(`${API_BASE}/realtime/radar`)
}

// Lidoire topographic catchment (GeoJSON, Sandre BD TOPAGE)
export async function getBasinGeoJson(): Promise<BasinGeoJson> {
  return fetchJson<BasinGeoJson>(`${API_BASE}/geo/basin`)
}

// Official vigilance (Vigicrues troncons, Meteo-France departements)
export async function getVigilance(): Promise<VigilanceStatus> {
  return fetchJson<VigilanceStatus>(`${API_BASE}/vigilance`)
}

// Field observations
export async function getObservations(start?: string, end?: string): Promise<Observation[]> {
  const params = new URLSearchParams()
  if (start) params.set('start', start)
  if (end) params.set('end', end)
  const query = params.toString()
  return fetchJson<Observation[]>(`${API_BASE}/observations${query ? `?${query}` : ''}`)
}

/** Create an observation: JSON, or multipart when a photo is attached. */
export async function createObservation(input: ObservationInput, photo?: File | null): Promise<Observation> {
  if (!photo) {
    return fetchJson<Observation>(`${API_BASE}/observations`, {
      method: 'POST',
      body: JSON.stringify(input),
    })
  }
  const form = new FormData()
  form.append('data', JSON.stringify(input))
  form.append('photo', photo)
  // No Content-Type header: the browser sets the multipart boundary
  const response = await fetch(`${API_BASE}/observations`, {
    method: 'POST',
    cache: 'no-store',
    headers: authHeaders('POST'),
    body: form,
  })
  if (!response.ok) throw await buildApiError(response)
  return response.json()
}

export async function deleteObservation(id: number): Promise<void> {
  await fetchJson<unknown>(`${API_BASE}/observations/${id}`, { method: 'DELETE' })
}

/** Observation photo as a blob (private: sent with X-API-Key, never a plain <img src>). */
export async function getObservationPhoto(photoUrl: string): Promise<Blob> {
  if (!photoUrl.startsWith(`${API_BASE}/observations/`)) throw new Error('URL de photo invalide')
  const key = getApiKey()
  const response = await fetch(photoUrl, {
    cache: 'no-store',
    headers: key ? { 'X-API-Key': key } : {},
  })
  if (!response.ok) throw await buildApiError(response)
  return response.blob()
}

export async function getEpisodeObservations(episodeId: number): Promise<EpisodeObservations> {
  return fetchJson<EpisodeObservations>(`${API_BASE}/episodes/${episodeId}/observations`)
}

// Hydrological indicators (flows, groundwater, observed tide), outside the risk model
export async function getIndicators(): Promise<IndicatorsData> {
  return fetchJson<IndicatorsData>(`${API_BASE}/indicators`)
}

// Lidoire long profile, confluence stage (estimated) and backwater reach at a time
export async function getLidoireProfile(at?: string, points: boolean = true): Promise<LidoireProfileData> {
  const params = new URLSearchParams()
  if (at) params.set('at', at)
  if (!points) params.set('points', 'false')
  const query = params.toString()
  return fetchJson<LidoireProfileData>(`${API_BASE}/geo/profile${query ? `?${query}` : ''}`)
}

// Flood drivers at each labelled episode peak (flood plane Eyraud Q x Pessac)
export async function getEpisodeDrivers(): Promise<EpisodeDriversData> {
  return fetchJson<EpisodeDriversData>(`${API_BASE}/episodes/drivers`)
}

// Labelled episodes nearest to the situation at a time
export async function getRiskAnalogs(at?: string, k: number = 3): Promise<RiskAnalogsData> {
  const params = new URLSearchParams({ k: String(k) })
  if (at) params.set('at', at)
  return fetchJson<RiskAnalogsData>(`${API_BASE}/risk/analogs?${params.toString()}`)
}

// OPERA radar frame over the basin: metadata (bbox, legend) and the same-origin PNG URL
export async function getRadarFrameMeta(t: string = 'latest'): Promise<RadarFrameMeta> {
  return fetchJson<RadarFrameMeta>(`${API_BASE}/realtime/radar/frame.json?t=${encodeURIComponent(t)}`)
}
