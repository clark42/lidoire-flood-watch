// API client for Lidoire Flood Watch backend

import type {
  RealtimeData,
  FloodEpisode,
  EpisodeAnalysis,
  RiskScore,
  MultiStationData,
  NetatmoData,
  InfoclimatData,
  DataSource,
  ForecastData,
  AvailableSources,
  UnifiedTimelineData,
  BasinRainfallData,
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

async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...options,
    cache: 'no-store',
    headers: {
      'Content-Type': 'application/json',
      ...authHeaders(options?.method),
      ...options?.headers,
    },
  })

  if (response.status === 401 || response.status === 403) {
    throw new Error(
      `API error: ${response.status} - cle API manquante ou invalide (voir Configuration)`
    )
  }
  if (!response.ok) {
    throw new Error(`API error: ${response.status} ${response.statusText}`)
  }

  return response.json()
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
}): Promise<any> {
  const searchParams = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    searchParams.set(key, value.toString())
  })

  return fetchJson<any>(`${API_BASE}/risk/simulate?${searchParams}`, {
    method: 'POST',
  })
}

// Status
export async function getStatus(): Promise<any> {
  return fetchJson<any>(`${API_BASE}/status`)
}

// Sources detail (exhaustive config)
export async function getSourcesDetail(): Promise<any> {
  return fetchJson<any>(`${API_BASE}/sources-detail`)
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

// Data sources status
export async function getDataSourcesStatus(): Promise<{ sources: DataSource[] }> {
  return fetchJson<{ sources: DataSource[] }>(`${API_BASE}/realtime/sources`)
}

// Forecast
export async function getForecast(hours: number = 168): Promise<ForecastData> {
  return fetchJson<ForecastData>(`${API_BASE}/risk/forecast?hours=${hours}`)
}

// Timeline Sources
export async function getAvailableSources(): Promise<AvailableSources> {
  return fetchJson<AvailableSources>(`${API_BASE}/timeline/sources`)
}

// Unified Timeline (River + Rainfall + Tide + Risk)
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
): Promise<UnifiedTimelineData> {
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

  return fetchJson<UnifiedTimelineData>(`${API_BASE}/timeline/unified?${params}`)
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
  const response = await fetch(`${API_BASE}/episodes/${id}`, { method: 'DELETE' })
  if (!response.ok) {
    throw new Error(`API error: ${response.status} ${response.statusText}`)
  }
}

// Confirm or reject episode (for model calibration)
export async function confirmEpisode(
  id: number,
  wasFlooded: boolean,
  actualSeverity?: number,
  notes?: string
): Promise<{ status: string; episode: FloodEpisode }> {
  const params = new URLSearchParams({ was_flooded: wasFlooded.toString() })
  if (actualSeverity) params.set('actual_severity', actualSeverity.toString())
  if (notes) params.set('notes', notes)
  return fetchJson(`${API_BASE}/episodes/confirm/${id}?${params}`, {
    method: 'POST',
  })
}
