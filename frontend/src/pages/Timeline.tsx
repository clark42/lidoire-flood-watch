import { useState, useEffect, useMemo, useCallback, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { format, subDays, addDays, parseISO } from 'date-fns'
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Brush,
} from 'recharts'
import { ChevronLeft, ChevronRight, Calendar, AlertTriangle, X } from 'lucide-react'
import InfoTooltip from '../components/InfoTooltip'
import { TOOLTIPS } from '../tooltipContent'
import {
  ApiError,
  errorMessage,
  getAvailableSources,
  getUnifiedTimeline,
  getEpisodesList,
  isAbortError,
} from '../api'
import type {
  AvailableSources,
  UnifiedTimelineData,
  FloodEpisode,
} from '../types'
import TimelineSourceSelector from '../components/TimelineSourceSelector'
import { formatParis, parseApiDate } from '../utils/time'

type Resolution = '15m' | '1h' | '6h' | '12h' | '1d'

// Default rain source: the Open-Meteo point at the moulin (always collected).
// Infoclimat is on by default too; Netatmo ids follow the backend config city
// names (settings.lidoire_netatmo_priority) and stay empty while Netatmo is
// not authorised (RainfallStationInfo.available === false).
const DEFAULT_RAIN_SOURCE = 'open-meteo:Moulin-Lidoire'

// Old ids (misspelled city names) that may still be in bookmarked URLs
const LEGACY_RAIN_SOURCE_IDS: Record<string, string> = {
  'netatmo-lidoire:Saint-Remy': 'netatmo-lidoire:Saint-Rémy',
  "netatmo-lidoire:Saint-Antoine-sur-l'Isle": 'netatmo-lidoire:Saint-Antoine-de-Breuilh',
}

// X-Backfill-Pending: the backend returned partial data and keeps backfilling.
// Refetch every BACKFILL_RETRY_MS, at most BACKFILL_MAX_RETRIES times per query.
const BACKFILL_RETRY_MS = 8000
const BACKFILL_MAX_RETRIES = 8

/** Unknown ids listed in a 422 detail ({error, unknown, valid}), or []. */
function unknownIdsFromError(err: unknown): string[] {
  if (!(err instanceof ApiError) || err.status !== 422) return []
  const detail = err.detail
  if (detail && typeof detail === 'object' && 'unknown' in detail) {
    const unknown = (detail as { unknown: unknown }).unknown
    if (Array.isArray(unknown)) return unknown.filter((u): u is string => typeof u === 'string')
  }
  return []
}

export default function Timeline() {
  const [searchParams, setSearchParams] = useSearchParams()
  const isInitialMount = useRef(true)

  // Parse URL params with defaults
  const parseParamList = (key: string, fallback: string[]): string[] => {
    const val = searchParams.get(key)
    return val ? val.split(',').filter(Boolean) : fallback
  }

  // Sources — initialized from URL
  const [selectedRiverStations, setSelectedRiverStations] = useState<string[]>(
    () => parseParamList('rivers', ['P555001001'])
  )
  const [selectedRainfallStations, setSelectedRainfallStations] = useState<string[]>(
    () => Array.from(new Set(
      parseParamList('rain', [DEFAULT_RAIN_SOURCE]).map(id => LEGACY_RAIN_SOURCE_IDS[id] ?? id)
    ))
  )
  const [tideEnabled, setTideEnabled] = useState(() => searchParams.get('tide') !== '0')
  const [riskEnabled, setRiskEnabled] = useState(() => searchParams.get('risk') !== '0')
  const [soilEnabled, setSoilEnabled] = useState(() => searchParams.get('soil') === '1')
  const [infoclimatEnabled, setInfoclimatEnabled] = useState(() => searchParams.get('infoclimat') !== '0')
  const [windEnabled, setWindEnabled] = useState(() => searchParams.get('wind') === '1')
  const [forecastEnabled, setForecastEnabled] = useState(() => searchParams.get('forecast') === '1')
  // Shadow model 3.4-gate (v3.4 + backwater gate), compared with the displayed score
  const [riskShadowEnabled, setRiskShadowEnabled] = useState(() => searchParams.get('risk_shadow') === '1')
  // Hydrological indicators (flow:EYRAUD, gw:MOULIETS, tide_obs:BORDEAUX...), outside the model
  const [selectedIndicators, setSelectedIndicators] = useState<string[]>(() => parseParamList('ind', []))

  // Data
  const [unifiedData, setUnifiedData] = useState<UnifiedTimelineData | null>(null)
  const [availableSources, setAvailableSources] = useState<AvailableSources | null>(null)
  const [episodes, setEpisodes] = useState<FloodEpisode[]>([])

  // Controls — initialized from URL
  const [startDate, setStartDate] = useState(() => {
    const s = searchParams.get('start')
    return s ? parseISO(s) : subDays(new Date(), 7)
  })
  const [endDate, setEndDate] = useState(() => {
    const e = searchParams.get('end')
    return e ? parseISO(e) : new Date()
  })
  const [resolution, setResolution] = useState<Resolution>(
    () => (searchParams.get('resolution') as Resolution) || '1h'
  )
  const [loading, setLoading] = useState(true)

  // Sync state → URL (skip initial mount)
  useEffect(() => {
    if (isInitialMount.current) {
      isInitialMount.current = false
      return
    }
    const params: Record<string, string> = {
      start: startDate.toISOString(),
      end: endDate.toISOString(),
      resolution,
    }
    if (selectedRiverStations.length > 0) {
      params.rivers = selectedRiverStations.join(',')
    }
    if (selectedRainfallStations.length > 0) {
      params.rain = selectedRainfallStations.join(',')
    }
    if (!tideEnabled) params.tide = '0'
    if (!riskEnabled) params.risk = '0'
    if (soilEnabled) params.soil = '1'
    if (!infoclimatEnabled) params.infoclimat = '0'
    if (windEnabled) params.wind = '1'
    if (forecastEnabled) params.forecast = '1'
    if (riskShadowEnabled) params.risk_shadow = '1'
    if (selectedIndicators.length > 0) params.ind = selectedIndicators.join(',')
    setSearchParams(params, { replace: true })
  }, [startDate, endDate, resolution, selectedRiverStations, selectedRainfallStations, tideEnabled, riskEnabled, soilEnabled, infoclimatEnabled, windEnabled, forecastEnabled, riskShadowEnabled, selectedIndicators, setSearchParams])

  // Fetch available sources and episodes on mount
  useEffect(() => {
    async function fetchSources() {
      try {
        const [sources, eps] = await Promise.all([
          getAvailableSources(),
          getEpisodesList(),
        ])
        setAvailableSources(sources)
        setEpisodes(eps)
      } catch (err) {
        console.error('Error fetching sources:', err)
      }
    }
    fetchSources()
  }, [])

  // Fetch state
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [backfillPending, setBackfillPending] = useState<string[]>([])
  const [backfillRetry, setBackfillRetry] = useState(0)
  const [backfillGaveUp, setBackfillGaveUp] = useState(false)
  const lastFetcher = useRef<unknown>(null)
  const retriesForQuery = useRef(0)

  // One query = one identity of this callback (changes with every input)
  const fetchTimeline = useCallback(
    (signal: AbortSignal) =>
      getUnifiedTimeline(
        startDate.toISOString(),
        endDate.toISOString(),
        selectedRiverStations.length > 0 ? selectedRiverStations : undefined,
        selectedRainfallStations.length > 0 ? selectedRainfallStations : undefined,
        tideEnabled,
        riskEnabled,
        resolution,
        soilEnabled,
        infoclimatEnabled,
        windEnabled,
        forecastEnabled,
        signal,
        riskShadowEnabled,
        selectedIndicators.length > 0 ? selectedIndicators : undefined,
      ),
    [startDate, endDate, selectedRiverStations, selectedRainfallStations, tideEnabled, riskEnabled, soilEnabled, infoclimatEnabled, windEnabled, forecastEnabled, riskShadowEnabled, selectedIndicators, resolution],
  )

  // Each run aborts the previous one, so a slow response (uncached period,
  // 5 s inline backfill wait) can never overwrite the result of a newer query.
  useEffect(() => {
    const isRetry = lastFetcher.current === fetchTimeline
    lastFetcher.current = fetchTimeline
    if (!isRetry) {
      // New query: fresh retry budget, spinner, no pending timer from the old one
      retriesForQuery.current = 0
      setBackfillGaveUp(false)
      setBackfillPending([])
      setLoading(true)
    }

    const controller = new AbortController()
    fetchTimeline(controller.signal)
      .then(({ data, backfillPending: pending }) => {
        if (controller.signal.aborted) return
        setUnifiedData(data)
        setError(null)
        setBackfillPending(pending)
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted || isAbortError(err)) return
        console.error('Error fetching timeline:', err)
        setBackfillPending([])
        // Never leave another period's series under an error message
        setUnifiedData(null)

        const unknown = unknownIdsFromError(err)
        if (unknown.length > 0) {
          // Bookmarked URL with removed/renamed ids: drop them from the
          // selection; the state change rewrites the URL and refetches.
          setError(null)
          setNotice(`Sources inconnues retirees de la selection : ${unknown.join(', ')}`)
          setSelectedRiverStations((prev) => prev.filter((id) => !unknown.includes(id)))
          setSelectedRainfallStations((prev) => prev.filter((id) => !unknown.includes(id)))
          setSelectedIndicators((prev) => prev.filter((id) => !unknown.includes(id)))
          return
        }
        setError(errorMessage(err))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })

    return () => controller.abort()
  }, [fetchTimeline, backfillRetry])

  // Bounded refetch while the backend reports running backfills
  useEffect(() => {
    if (backfillPending.length === 0) return
    if (retriesForQuery.current >= BACKFILL_MAX_RETRIES) {
      setBackfillGaveUp(true)
      return
    }
    const timer = window.setTimeout(() => {
      retriesForQuery.current += 1
      setBackfillRetry((n) => n + 1)
    }, BACKFILL_RETRY_MS)
    return () => window.clearTimeout(timer)
  }, [backfillPending])

  // Period navigation
  const shiftPeriod = (days: number) => {
    setStartDate((prev) => addDays(prev, days))
    setEndDate((prev) => addDays(prev, days))
  }

  const setPeriod = (days: number) => {
    const now = new Date()
    setEndDate(now)
    setStartDate(subDays(now, days))
  }

  // Source toggles
  const toggleRiverStation = (stationId: string) => {
    setSelectedRiverStations((prev) =>
      prev.includes(stationId)
        ? prev.filter((id) => id !== stationId)
        : [...prev, stationId]
    )
  }

  const toggleRainfallStation = (stationId: string) => {
    setSelectedRainfallStations((prev) =>
      prev.includes(stationId)
        ? prev.filter((id) => id !== stationId)
        : [...prev, stationId]
    )
  }

  const toggleIndicator = (seriesId: string) => {
    setSelectedIndicators((prev) =>
      prev.includes(seriesId)
        ? prev.filter((id) => id !== seriesId)
        : [...prev, seriesId]
    )
  }

  const selectAllRivers = () => {
    if (availableSources) {
      setSelectedRiverStations(availableSources.river_stations.map((s) => s.id))
    }
  }

  const selectAllRainfall = () => {
    if (availableSources) {
      setSelectedRainfallStations(availableSources.rainfall_stations.map((s) => s.id))
    }
  }

  const goToEpisode = (ep: FloodEpisode) => {
    const epStart = parseApiDate(ep.start_time) ?? new Date()
    const epEnd = parseApiDate(ep.end_time)
      ?? new Date(epStart.getTime() + 5 * 24 * 60 * 60 * 1000)
    // Add 3-day buffer before/after
    setStartDate(subDays(epStart, 3))
    setEndDate(addDays(epEnd, 1))
  }

  const clearAllSources = () => {
    setSelectedRiverStations([])
    setSelectedRainfallStations([])
    setTideEnabled(false)
    setRiskEnabled(false)
    setSoilEnabled(false)
    setInfoclimatEnabled(false)
    setWindEnabled(false)
    setForecastEnabled(false)
    setSelectedIndicators([])
  }

  // Unified chart data: merge all series into timestamp-indexed rows
  const chartData = useMemo(() => {
    if (!unifiedData) return []

    const timestampMap = new Map<string, Record<string, number | null>>()

    for (const [seriesKey, series] of Object.entries(unifiedData.series)) {
      for (const point of series.data) {
        const ts = point.timestamp
        if (!timestampMap.has(ts)) {
          timestampMap.set(ts, { time: parseApiDate(ts)?.getTime() ?? 0 })
        }
        const row = timestampMap.get(ts)!
        row[seriesKey] = point.value
      }
    }

    return Array.from(timestampMap.entries())
      .sort((a, b) => (a[1].time ?? 0) - (b[1].time ?? 0))
      .map(([ts, values]) => ({
        ...values,
        displayTime: formatParis(ts, 'dd/MM HH:mm'),
      }))
  }, [unifiedData])

  // Group series by type for rendering (4 groups for separate charts)
  type SeriesEntry = { key: string; name: string; color: string; type: string; unit: string }
  const seriesInfo = useMemo(() => {
    if (!unifiedData) return { level: [], rain: [], risk: [], soil: [], wind: [], flowLocal: [], flowDordogne: [], groundwater: [] }

    const level: SeriesEntry[] = []
    const rain: SeriesEntry[] = []
    const risk: SeriesEntry[] = []
    const soil: SeriesEntry[] = []
    const wind: SeriesEntry[] = []
    const flowLocal: SeriesEntry[] = []
    const flowDordogne: SeriesEntry[] = []
    const groundwater: SeriesEntry[] = []

    for (const [key, series] of Object.entries(unifiedData.series)) {
      const info: SeriesEntry = {
        key, name: series.name, color: series.color,
        type: series.type, unit: series.unit,
      }
      if (series.type === 'river_level' || series.type === 'tide' || series.type === 'forecast_level' || series.type === 'tide_observed') {
        level.push(info)
      } else if (
        series.type === 'risk_score' ||
        series.type === 'risk_score_shadow' ||
        series.type === 'forecast_risk_score'
      ) {
        risk.push(info)
      } else if (series.type === 'forecast_rainfall') {
        rain.push(info)
      } else if (
        series.type === 'soil_moisture' ||
        series.type === 'soil_temperature' ||
        series.type === 'evapotranspiration'
      ) {
        soil.push(info)
      } else if (series.type === 'wind_speed' || series.type === 'wind_westerly') {
        wind.push(info)
      } else if (series.type === 'flow_tributary') {
        flowLocal.push(info)
      } else if (series.type === 'flow_dordogne') {
        flowDordogne.push(info)
      } else if (series.type === 'groundwater') {
        groundwater.push(info)
      } else {
        rain.push(info)
      }
    }

    return { level, rain, risk, soil, wind, flowLocal, flowDordogne, groundwater }
  }, [unifiedData])

  // Tooltip config per panel
  const panelTooltips: Record<string, typeof TOOLTIPS[keyof typeof TOOLTIPS]> = {
    level: TOOLTIPS.timelineLevels,
    rain: TOOLTIPS.timelineRainfall,
    risk: TOOLTIPS.timelineRisk,
    soil: TOOLTIPS.timelineSoilMoisture,
  }

  // Ordered list of visible chart panels (for knowing which is last = gets Brush)
  const visiblePanels = useMemo(() => {
    const panels: { id: string; label: string; series: SeriesEntry[]; height: number; yLabel: string }[] = []
    if (seriesInfo.level.length > 0) {
      panels.push({ id: 'level', label: 'Niveaux d\'eau', series: seriesInfo.level, height: 220, yLabel: 'Niveau (m)' })
    }
    if (seriesInfo.rain.length > 0) {
      // Derive unit label from first non-cumul series
      const rateUnit = seriesInfo.rain.find((s) => s.type === 'rainfall')?.unit || 'mm/h'
      panels.push({ id: 'rain', label: 'Pluviometrie', series: seriesInfo.rain, height: 180, yLabel: `Pluie (${rateUnit})` })
    }
    if (seriesInfo.risk.length > 0) {
      panels.push({ id: 'risk', label: 'Score de risque', series: seriesInfo.risk, height: 130, yLabel: 'Score (/100)' })
    }
    if (seriesInfo.soil.length > 0) {
      panels.push({ id: 'soil', label: 'Conditions sol', series: seriesInfo.soil, height: 180, yLabel: '' })
    }
    if (seriesInfo.wind.length > 0) {
      panels.push({ id: 'wind', label: 'Vent', series: seriesInfo.wind, height: 150, yLabel: 'Vitesse (km/h)' })
    }
    // Hydrological indicators (outside the model): tributaries and Dordogne on separate axes
    if (seriesInfo.flowLocal.length > 0) {
      panels.push({ id: 'flow_local', label: 'Debits affluents (analogues Lidoire)', series: seriesInfo.flowLocal, height: 160, yLabel: 'Debit (m³/s)' })
    }
    if (seriesInfo.flowDordogne.length > 0) {
      panels.push({ id: 'flow_dordogne', label: 'Debits Dordogne', series: seriesInfo.flowDordogne, height: 160, yLabel: 'Debit (m³/s)' })
    }
    if (seriesInfo.groundwater.length > 0) {
      panels.push({ id: 'groundwater', label: 'Nappe alluviale', series: seriesInfo.groundwater, height: 140, yLabel: 'm NGF' })
    }
    return panels
  }, [seriesInfo])

  return (
    <div className="space-y-6">
      {/* Controls */}
      <div className="bg-white rounded-lg shadow p-4">
        <div className="flex flex-wrap items-center justify-between gap-4">
          {/* Period Navigation */}
          <div className="flex items-center gap-2">
            <button
              onClick={() => shiftPeriod(-7)}
              className="p-2 hover:bg-gray-100 rounded"
              title="Semaine precedente"
            >
              <ChevronLeft className="w-5 h-5" />
            </button>

            <div className="flex items-center gap-1">
              <Calendar className="w-4 h-4 text-gray-500" />
              <input
                type="datetime-local"
                value={format(startDate, "yyyy-MM-dd'T'HH:mm")}
                onChange={(e) => {
                  if (e.target.value) setStartDate(new Date(e.target.value))
                }}
                className="border rounded px-2 py-1 text-sm w-[170px]"
              />
              <span className="text-gray-400">—</span>
              <input
                type="datetime-local"
                value={format(endDate, "yyyy-MM-dd'T'HH:mm")}
                onChange={(e) => {
                  if (e.target.value) setEndDate(new Date(e.target.value))
                }}
                className="border rounded px-2 py-1 text-sm w-[170px]"
              />
            </div>

            <button
              onClick={() => shiftPeriod(7)}
              className="p-2 hover:bg-gray-100 rounded"
              title="Semaine suivante"
            >
              <ChevronRight className="w-5 h-5" />
            </button>
          </div>

          {/* Quick Period Buttons */}
          <div className="flex gap-2">
            {[
              { label: '24h', days: 1 },
              { label: '3j', days: 3 },
              { label: '7j', days: 7 },
              { label: '14j', days: 14 },
              { label: '30j', days: 30 },
            ].map(({ label, days }) => (
              <button
                key={days}
                onClick={() => setPeriod(days)}
                className="px-3 py-1 text-sm rounded border hover:bg-gray-50"
              >
                {label}
              </button>
            ))}
          </div>

          {/* Resolution */}
          <div className="flex items-center gap-2">
            <span className="text-sm text-gray-500">Resolution:</span>
            <select
              value={resolution}
              onChange={(e) => setResolution(e.target.value as Resolution)}
              className="border rounded px-2 py-1 text-sm"
            >
              <option value="15m">15 min</option>
              <option value="1h">1 heure</option>
              <option value="6h">6 heures</option>
              <option value="12h">12 heures</option>
              <option value="1d">1 jour</option>
            </select>
          </div>
        </div>

        {/* Source Selector */}
        {availableSources && (
          <div className="mt-4 pt-4 border-t">
            <TimelineSourceSelector
              riverStations={availableSources.river_stations}
              rainfallStations={availableSources.rainfall_stations}
              selectedRiverStations={selectedRiverStations}
              selectedRainfallStations={selectedRainfallStations}
              onRiverStationToggle={toggleRiverStation}
              onRainfallStationToggle={toggleRainfallStation}
              onSelectAllRivers={selectAllRivers}
              onSelectAllRainfall={selectAllRainfall}
              onClearAll={clearAllSources}
              tideEnabled={tideEnabled}
              riskEnabled={riskEnabled}
              onTideToggle={() => setTideEnabled((v) => !v)}
              onRiskToggle={() => setRiskEnabled((v) => !v)}
              riskShadowEnabled={riskShadowEnabled}
              onRiskShadowToggle={() => setRiskShadowEnabled((v) => !v)}
              soilEnabled={soilEnabled}
              onSoilToggle={() => setSoilEnabled((v) => !v)}
              infoclimatEnabled={infoclimatEnabled}
              onInfoclimatToggle={() => setInfoclimatEnabled((v) => !v)}
              windEnabled={windEnabled}
              onWindToggle={() => setWindEnabled((v) => !v)}
              forecastEnabled={forecastEnabled}
              onForecastToggle={() => setForecastEnabled((v) => !v)}
              indicatorSeries={availableSources.indicator_series}
              selectedIndicators={selectedIndicators}
              onIndicatorToggle={toggleIndicator}
            />
          </div>
        )}
      </div>

      {/* Episode Quick Navigation */}
      {episodes.length > 0 && (
        <div className="bg-white rounded-lg shadow p-3">
          <div className="flex items-center gap-3 overflow-x-auto">
            <span className="flex items-center gap-1 text-xs font-semibold text-gray-500 uppercase tracking-wide whitespace-nowrap">
              <AlertTriangle className="w-3.5 h-3.5" />
              Episodes
            </span>
            {episodes.map((ep) => {
              const epDate = parseApiDate(ep.peak_time || ep.start_time)
              const isActive =
                epDate !== null && epDate >= startDate && epDate <= endDate
              return (
                <button
                  key={ep.id}
                  onClick={() => goToEpisode(ep)}
                  className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium whitespace-nowrap transition-colors ${
                    isActive
                      ? 'bg-orange-100 text-orange-800 ring-1 ring-orange-300'
                      : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                  }`}
                  title={ep.description || undefined}
                >
                  <span className={`w-2 h-2 rounded-full ${
                    ep.severity >= 4 ? 'bg-red-500'
                    : ep.severity >= 3 ? 'bg-orange-500'
                    : 'bg-yellow-500'
                  }`} />
                  {formatParis(epDate, 'dd MMM yyyy')}
                  <span className="text-gray-400">({ep.severity}/5)</span>
                </button>
              )
            })}
          </div>
        </div>
      )}

      {error && (
        <div role="alert" className="bg-red-50 border border-red-200 text-red-800 rounded-lg px-4 py-3 text-sm flex items-start gap-2">
          <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0" />
          <span>Impossible de charger la timeline. {error}</span>
        </div>
      )}
      {notice && (
        <div role="status" className="bg-yellow-50 border border-yellow-200 text-yellow-800 rounded-lg px-4 py-3 text-sm flex items-start justify-between gap-2">
          <span>{notice}</span>
          <button
            type="button"
            onClick={() => setNotice(null)}
            className="text-yellow-700 hover:text-yellow-900"
            aria-label="Fermer"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      )}
      {backfillPending.length > 0 && !loading && (
        <div role="status" className="bg-blue-50 border border-blue-200 text-blue-800 rounded-lg px-4 py-3 text-sm">
          {backfillGaveUp
            ? `Donnees encore incompletes (${backfillPending.join(', ')}) : le chargement historique continue cote serveur. Rechargez la page dans quelques minutes.`
            : `Donnees en cours de chargement (${backfillPending.join(', ')}) : les courbes seront completees automatiquement.`}
        </div>
      )}

      {/* Charts — stacked, synchronized via syncId */}
      <div className="bg-white rounded-lg shadow p-4">
        {loading ? (
          <div className="h-[450px] flex items-center justify-center">
            <div className="animate-pulse text-gray-400">Chargement...</div>
          </div>
        ) : chartData.length === 0 ? (
          <div className="h-[450px] flex items-center justify-center text-gray-400">
            Aucune donnee pour la periode et les sources selectionnees
          </div>
        ) : (
          <div className="space-y-0">
            {visiblePanels.map((panel, idx) => {
              const isLast = idx === visiblePanels.length - 1
              const isSoil = panel.id === 'soil'
              const isRain = panel.id === 'rain'

              // For soil: split into moisture (left Y) and temp+ET0 (right Y)
              const soilMoisture = isSoil
                ? panel.series.filter((s) => s.type === 'soil_moisture')
                : []
              const soilOther = isSoil
                ? panel.series.filter((s) => s.type !== 'soil_moisture')
                : []
              const hasSoilRightAxis = soilOther.length > 0

              // For rain: split rate series (left Y) and cumul (right Y)
              const rainRate = isRain
                ? panel.series.filter((s) => s.type === 'rainfall' || s.type === 'forecast_rainfall')
                : []
              const rainCumul = isRain
                ? panel.series.filter((s) => s.type === 'rainfall_cumul')
                : []
              const hasRainRightAxis = rainCumul.length > 0

              return (
                <div key={panel.id}>
                  {/* Panel header */}
                  <div className="flex items-center gap-3 mb-1 mt-3 first:mt-0">
                    <span className="text-xs font-semibold text-gray-500 uppercase tracking-wide flex items-center gap-1">
                      {panel.label}
                      {panelTooltips[panel.id] && (
                        <InfoTooltip {...panelTooltips[panel.id]} />
                      )}
                    </span>
                    <div className="flex-1 border-t border-gray-200" />
                    {/* Inline legend chips */}
                    <div className="flex gap-2 flex-wrap">
                      {panel.series.map((s) => {
                        // Per-series tooltip for soil panel
                        const seriesTooltip =
                          s.type === 'soil_temperature' ? TOOLTIPS.timelineSoilTemp
                          : s.type === 'evapotranspiration' ? TOOLTIPS.timelineET0
                          : s.type === 'tide' ? TOOLTIPS.timelineTide
                          : null
                        return (
                          <span key={s.key} className="flex items-center gap-1 text-xs text-gray-600">
                            <span
                              className="inline-block w-2.5 h-2.5 rounded-full"
                              style={{ backgroundColor: s.color }}
                            />
                            {s.name}
                            {seriesTooltip && <InfoTooltip {...seriesTooltip} />}
                          </span>
                        )
                      })}
                    </div>
                  </div>

                  <ResponsiveContainer width="100%" height={panel.height + (isLast ? 40 : 0)}>
                    <LineChart
                      data={chartData}
                      syncId="timeline"
                      margin={{ top: 5, right: 10, bottom: isLast ? 5 : 0, left: 10 }}
                    >
                      <CartesianGrid strokeDasharray="3 3" opacity={0.5} />

                      {/* X axis: only show ticks on the last chart */}
                      <XAxis
                        dataKey="displayTime"
                        tick={isLast ? { fontSize: 11 } : false}
                        tickLine={isLast}
                        axisLine={{ stroke: '#e5e7eb' }}
                        interval="preserveStartEnd"
                        height={isLast ? 25 : 5}
                      />

                      {/* Y axis — soil and rain panels have dual axes */}
                      {isSoil ? (
                        <>
                          <YAxis
                            yAxisId="left"
                            orientation="left"
                            domain={[0, 'auto']}
                            tick={{ fontSize: 11 }}
                            width={55}
                            label={{
                              value: 'm\u00b3/m\u00b3',
                              angle: -90,
                              position: 'insideLeft',
                              style: { fontSize: 11, fill: '#7c3aed' },
                            }}
                          />
                          {hasSoilRightAxis && (
                            <YAxis
                              yAxisId="right"
                              orientation="right"
                              domain={['auto', 'auto']}
                              tick={{ fontSize: 11 }}
                              width={45}
                              label={{
                                value: '\u00b0C / mm',
                                angle: 90,
                                position: 'insideRight',
                                style: { fontSize: 11, fill: '#f97316' },
                              }}
                            />
                          )}
                        </>
                      ) : isRain ? (
                        <>
                          <YAxis
                            yAxisId="left"
                            orientation="left"
                            domain={[0, 'auto']}
                            tick={{ fontSize: 11 }}
                            width={55}
                            label={{
                              value: panel.yLabel,
                              angle: -90,
                              position: 'insideLeft',
                              style: { fontSize: 11, fill: '#6b7280' },
                            }}
                          />
                          {hasRainRightAxis && (
                            <YAxis
                              yAxisId="right"
                              orientation="right"
                              domain={[0, 'auto']}
                              tick={{ fontSize: 11 }}
                              width={50}
                              label={{
                                value: 'Cumul (mm)',
                                angle: 90,
                                position: 'insideRight',
                                style: { fontSize: 11, fill: '#1e40af' },
                              }}
                            />
                          )}
                        </>
                      ) : (
                        <YAxis
                          yAxisId="left"
                          orientation="left"
                          domain={panel.id === 'risk' ? [0, 100] : panel.id === 'groundwater' ? ['auto', 'auto'] : [0, 'auto']}
                          tick={{ fontSize: 11 }}
                          width={55}
                          label={{
                            value: panel.yLabel,
                            angle: -90,
                            position: 'insideLeft',
                            style: { fontSize: 11, fill: '#6b7280' },
                          }}
                        />
                      )}

                      <Tooltip
                        labelFormatter={(label) => label}
                        formatter={(value: number, name: string) => {
                          const series = unifiedData?.series[name]
                          if (series) {
                            return [
                              `${value?.toFixed(2)} ${series.unit}`,
                              series.name,
                            ]
                          }
                          return [value?.toFixed(2), name]
                        }}
                      />

                      {/* Lines */}
                      {isSoil ? (
                        <>
                          {soilMoisture.map((info) => (
                            <Line
                              key={info.key}
                              yAxisId="left"
                              type="monotone"
                              dataKey={info.key}
                              name={info.key}
                              stroke={info.color}
                              strokeWidth={1.5}
                              dot={false}
                              connectNulls
                            />
                          ))}
                          {soilOther.map((info) => (
                            <Line
                              key={info.key}
                              yAxisId={hasSoilRightAxis ? 'right' : 'left'}
                              type="monotone"
                              dataKey={info.key}
                              name={info.key}
                              stroke={info.color}
                              strokeWidth={1.5}
                              strokeDasharray={info.type === 'soil_temperature' ? '4 2' : undefined}
                              dot={false}
                              connectNulls
                            />
                          ))}
                        </>
                      ) : isRain ? (
                        <>
                          {rainRate.map((info) => (
                            <Line
                              key={info.key}
                              yAxisId="left"
                              type="monotone"
                              dataKey={info.key}
                              name={info.key}
                              stroke={info.color}
                              strokeWidth={info.type === 'forecast_rainfall' ? 2 : 1.5}
                              strokeDasharray={info.type === 'forecast_rainfall' ? '6 3' : undefined}
                              dot={false}
                              connectNulls
                            />
                          ))}
                          {rainCumul.map((info) => (
                            <Line
                              key={info.key}
                              yAxisId={hasRainRightAxis ? 'right' : 'left'}
                              type="monotone"
                              dataKey={info.key}
                              name={info.key}
                              stroke={info.color}
                              strokeWidth={2}
                              strokeDasharray="6 3"
                              dot={false}
                              connectNulls
                            />
                          ))}
                        </>
                      ) : (
                        panel.series.map((info) => (
                          <Line
                            key={info.key}
                            yAxisId="left"
                            type="monotone"
                            dataKey={info.key}
                            name={info.key}
                            stroke={info.color}
                            strokeWidth={
                              info.type === 'tide' || info.type === 'risk_score' ? 2
                              : info.type.startsWith('forecast_') ? 2
                              : 1.5
                            }
                            strokeDasharray={
                              info.type === 'tide' ? '8 4'
                              : info.type === 'risk_score' ? '5 5'
                              : info.type === 'risk_score_shadow' ? '2 3'
                              : info.type.startsWith('forecast_') ? '6 3'
                              : undefined
                            }
                            dot={false}
                            connectNulls
                          />
                        ))
                      )}

                      {/* Brush on last chart only */}
                      {isLast && (
                        <Brush
                          dataKey="displayTime"
                          height={25}
                          stroke="#0891b2"
                          fill="#f0fdfa"
                          tickFormatter={(value) => value}
                        />
                      )}
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}
