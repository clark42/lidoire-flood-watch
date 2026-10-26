import { useState, useEffect, useCallback, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { format } from 'date-fns'
import { fr } from 'date-fns/locale'
import {
  Droplets,
  Waves,
  TrendingUp,
  TrendingDown,
  Minus,
  Clock,
  AlertTriangle,
  RefreshCw,
  Activity,
  CloudRain,
} from 'lucide-react'
import {
  getRealtimeData,
  getNetatmoData,
  getInfoclimatData,
  getDataSourcesStatus,
  getBasinRainfall,
  getRealtimeDataAt,
  getEpisodesList,
} from '../api'
import type {
  RealtimeData,
  RiskLevel,
  NetatmoData,
  InfoclimatData,
  DataSource,
  BasinRainfallData,
  FloodEpisode,
} from '../types'
import RiskGauge from '../components/RiskGauge'
import DataCard from '../components/DataCard'
import StationsPanel from '../components/StationsPanel'
import RainfallPanel from '../components/RainfallPanel'
import ForecastPanel from '../components/ForecastPanel'
import DataSourcesStatus from '../components/DataSourcesStatus'
import RiskBreakdownPanel from '../components/RiskBreakdownPanel'
import HydroMap from '../components/HydroMap'
import BasinRainfallGauge from '../components/BasinRainfallGauge'
import MetricSparkline from '../components/MetricSparkline'
import TimeMachineBanner from '../components/TimeMachineBanner'
import SoilConditionsPanel from '../components/SoilConditionsPanel'
import NowcastRainChart from '../components/NowcastRainChart'
import InfoTooltip from '../components/InfoTooltip'
import { TOOLTIPS } from '../tooltipContent'
import { formatAge, parseApiDate } from '../utils/time'

const RISK_BG_COLORS: Record<RiskLevel, string> = {
  green: 'bg-green-50 border-green-200',
  yellow: 'bg-yellow-50 border-yellow-200',
  orange: 'bg-orange-50 border-orange-200',
  red: 'bg-red-50 border-red-200',
}

function TendencyIcon({ tendency }: { tendency: string | null }) {
  if (tendency === 'hausse') return <TrendingUp className="w-4 h-4 text-red-500" />
  if (tendency === 'baisse') return <TrendingDown className="w-4 h-4 text-green-500" />
  return <Minus className="w-4 h-4 text-gray-400" />
}

const POLL_INTERVAL_MS = 60_000
// Newest river measurement older than this is flagged as stale (collector or
// Hub'Eau failure). Hub'Eau publishes with up to ~1-2 h of latency, so a
// shorter threshold would flag normal operation.
const STALE_AFTER_MS = 3 * 60 * 60_000
const PESSAC_STATION_ID = 'P555001001'

/**
 * Time of the newest underlying measurement. RealtimeData.timestamp is the
 * request time (always "now"), so freshness uses the station observations:
 * max of stations[].current_level.timestamp, falling back to Pessac / the
 * reference river level.
 */
function latestMeasurementDate(d: RealtimeData): Date | null {
  const candidates: (string | null | undefined)[] = [
    ...(d.stations?.stations ?? []).map(s => s.current_level?.timestamp),
    d.river?.current?.timestamp,
  ]
  let latest: Date | null = null
  for (const value of candidates) {
    const date = parseApiDate(value)
    if (date && (!latest || date > latest)) latest = date
  }
  return latest
}

const SECONDARY_SOURCE_LABELS = {
  netatmo: 'Netatmo',
  infoclimat: 'Infoclimat',
  sources: 'Statut des sources',
  basin: 'Pluie bassin',
  episodes: 'Episodes',
} as const

type SecondarySource = keyof typeof SECONDARY_SOURCE_LABELS

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : 'Erreur de chargement'
}

export default function Dashboard() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [data, setData] = useState<RealtimeData | null>(null)
  const [netatmo, setNetatmo] = useState<NetatmoData | null>(null)
  const [infoclimat, setInfoclimat] = useState<InfoclimatData | null>(null)
  const [sources, setSources] = useState<DataSource[]>([])
  const [selectedStation, setSelectedStation] = useState<string | null>(null)
  const [breakdownExpanded, setBreakdownExpanded] = useState(false)
  const [loading, setLoading] = useState(true)
  // Error of the last /realtime fetch (null when it succeeded)
  const [error, setError] = useState<string | null>(null)
  const [lastFailureAt, setLastFailureAt] = useState<Date | null>(null)
  const [failedSecondary, setFailedSecondary] = useState<SecondarySource[]>([])
  const [basinRainfall, setBasinRainfall] = useState<BasinRainfallData | null>(null)
  const [episodes, setEpisodes] = useState<FloodEpisode[]>([])
  // Wall clock used to refresh the displayed data age
  const [now, setNow] = useState(() => Date.now())

  // TimeMachine state — initialized from URL ?tm= param
  const tmParam = searchParams.get('tm')
  const [timeMachineActive, setTimeMachineActive] = useState(!!tmParam)
  const [timeMachineDate, setTimeMachineDate] = useState<Date | null>(
    tmParam ? new Date(tmParam) : null
  )
  const [timeMachineLoading, setTimeMachineLoading] = useState(false)
  const [timeMachineError, setTimeMachineError] = useState<string | null>(null)
  // Id of the latest TimeMachine request (older responses are ignored)
  const timeMachineRequestRef = useRef(0)

  // Refs used by the polling loop (avoid stale closures and overlapping polls)
  const inFlightRef = useRef(false)
  const timeMachineActiveRef = useRef(timeMachineActive)
  useEffect(() => {
    timeMachineActiveRef.current = timeMachineActive
  }, [timeMachineActive])

  // Sparkline accumulation
  const [sparklineData, setSparklineData] = useState<{
    river: { t: number; v: number }[]
    tide: { t: number; v: number }[]
    rain: { t: number; v: number }[]
    risk: { t: number; v: number }[]
  }>({ river: [], tide: [], rain: [], risk: [] })

  const fetchData = useCallback(async () => {
    // In-flight guard: a slow backend must not stack overlapping polls
    if (inFlightRef.current) return
    inFlightRef.current = true
    try {
      setLoading(true)

      // Fetch all data in parallel
      const [
        realtimeResult,
        netatmoResult,
        infoclimatResult,
        sourcesResult,
        basinResult,
        episodesResult,
      ] = await Promise.allSettled([
        getRealtimeData(),
        getNetatmoData(),
        getInfoclimatData(),
        getDataSourcesStatus(),
        getBasinRainfall(),
        getEpisodesList(),
      ])

      // TimeMachine may have been enabled while this poll was in flight:
      // never overwrite historical data with live data.
      if (timeMachineActiveRef.current) return

      if (realtimeResult.status === 'fulfilled') {
        const d = realtimeResult.value
        setData(d)
        setError(null)
        setLastFailureAt(null)
        // Set default selected station to primary (keep the user's choice)
        const primaryId = d.stations?.primary_station_id
        if (primaryId) {
          setSelectedStation(prev => prev ?? primaryId)
        }

        // Accumulate sparkline data
        const t = Date.now()
        const pessac = d.stations?.stations.find(s => s.station_id === PESSAC_STATION_ID)
        setSparklineData(prev => ({
          // "Dordogne" reference is Pessac (Hub'Eau), not Libourne (tidal)
          river: pessac
            ? [...prev.river, { t, v: pessac.current_level.height_m }].slice(-60)
            : prev.river,
          tide: [...prev.tide, { t, v: d.tide.current_coefficient || 0 }].slice(-60),
          rain: [...prev.rain, { t, v: d.rainfall.last_24h_mm }].slice(-60),
          risk: [...prev.risk, { t, v: d.risk.score }].slice(-60),
        }))
      } else {
        setError(errorMessage(realtimeResult.reason))
        setLastFailureAt(new Date())
      }

      const failed: SecondarySource[] = []
      if (netatmoResult.status === 'fulfilled') setNetatmo(netatmoResult.value)
      else failed.push('netatmo')
      if (infoclimatResult.status === 'fulfilled') setInfoclimat(infoclimatResult.value)
      else failed.push('infoclimat')
      if (sourcesResult.status === 'fulfilled') setSources(sourcesResult.value.sources)
      else failed.push('sources')
      if (basinResult.status === 'fulfilled') setBasinRainfall(basinResult.value)
      else failed.push('basin')
      if (episodesResult.status === 'fulfilled') setEpisodes(episodesResult.value)
      else failed.push('episodes')
      setFailedSecondary(failed)
    } finally {
      inFlightRef.current = false
      setLoading(false)
    }
  }, [])

  const handleTimeMachineToggle = useCallback(() => {
    if (timeMachineActive) {
      // Return to live — remove ?tm from URL; the polling effect refetches.
      // Bumping the request id drops any TimeMachine response still in flight.
      timeMachineRequestRef.current += 1
      timeMachineActiveRef.current = false
      setTimeMachineLoading(false)
      setTimeMachineError(null)
      setTimeMachineActive(false)
      setTimeMachineDate(null)
      setSearchParams((prev) => {
        prev.delete('tm')
        return prev
      }, { replace: true })
    } else {
      timeMachineActiveRef.current = true
      setTimeMachineActive(true)
    }
  }, [timeMachineActive, setSearchParams])

  const handleTimeMachineDateChange = useCallback(async (date: Date) => {
    // Only the latest request may write: picking another date or going back
    // to live invalidates any response still in flight.
    const requestId = ++timeMachineRequestRef.current
    const isCurrent = () =>
      requestId === timeMachineRequestRef.current && timeMachineActiveRef.current
    setTimeMachineDate(date)
    setTimeMachineError(null)
    // Sync URL with ?tm= param
    setSearchParams({ tm: date.toISOString() }, { replace: true })
    setTimeMachineLoading(true)
    try {
      const result = await getRealtimeDataAt(date.toISOString())
      if (!isCurrent()) return
      setData(result)
      const primaryId = result.stations?.primary_station_id
      if (primaryId) {
        setSelectedStation(prev => prev ?? primaryId)
      }
    } catch (err) {
      if (!isCurrent()) return
      console.error('TimeMachine error:', err)
      setTimeMachineError(errorMessage(err))
    } finally {
      if (requestId === timeMachineRequestRef.current) setTimeMachineLoading(false)
    }
  }, [setSearchParams])

  // On mount: if ?tm= is present, load TimeMachine data
  useEffect(() => {
    if (tmParam && timeMachineDate) {
      handleTimeMachineDateChange(timeMachineDate)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Live polling, paused while the tab is hidden or TimeMachine is active
  useEffect(() => {
    if (timeMachineActive) return

    let interval: ReturnType<typeof setInterval> | null = null
    const start = () => {
      if (interval) return
      fetchData()
      interval = setInterval(fetchData, POLL_INTERVAL_MS)
    }
    const stop = () => {
      if (interval) {
        clearInterval(interval)
        interval = null
      }
    }
    const onVisibilityChange = () => {
      if (document.hidden) stop()
      else start()
    }

    if (!document.hidden) start()
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => {
      stop()
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }, [timeMachineActive, fetchData])

  // Refresh the displayed data age
  useEffect(() => {
    const tick = setInterval(() => setNow(Date.now()), 30_000)
    return () => clearInterval(tick)
  }, [])

  if (loading && !data) {
    return (
      <div className="flex items-center justify-center h-64">
        <RefreshCw className="w-8 h-8 animate-spin text-blue-500" />
      </div>
    )
  }

  if (error && !data) {
    return (
      <div className="bg-red-50 border border-red-200 rounded-lg p-6 text-center">
        <AlertTriangle className="w-12 h-12 text-red-500 mx-auto mb-4" />
        <p className="text-red-700">{error}</p>
        <button
          onClick={() => fetchData()}
          className="mt-4 px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700"
        >
          Reessayer
        </button>
      </div>
    )
  }

  if (!data) return null

  const riskLevel = data.risk.level
  const stations = data.stations?.stations || []
  const pessacStation = stations.find(s => s.station_id === PESSAC_STATION_ID)

  // Freshness is based on the newest measurement, not on the request time
  const dataDate = latestMeasurementDate(data)
  const dataAgeMs = dataDate ? now - dataDate.getTime() : null
  const isStale = !timeMachineActive && (
    error !== null || (dataAgeMs !== null && dataAgeMs > STALE_AFTER_MS)
  )

  return (
    <div className="space-y-6">
      {/* Stale / fetch error banner (live mode only) */}
      {isStale && (
        <div
          role="alert"
          className="bg-red-50 border border-red-200 rounded-lg p-4 flex items-start gap-3"
        >
          <AlertTriangle className="w-5 h-5 text-red-500 flex-shrink-0 mt-0.5" />
          <div className="flex-1 text-sm text-red-800">
            <p className="font-medium">
              Donnees non actualisees
              {dataDate ? ` — derniere mesure ${formatAge(dataDate, now)}` : ''}
            </p>
            {error && (
              <p className="mt-1">
                Echec du chargement temps reel
                {lastFailureAt ? ` (${format(lastFailureAt, 'HH:mm:ss', { locale: fr })})` : ''}
                {' '}: {error}. Les valeurs affichees peuvent etre perimees.
              </p>
            )}
          </div>
          <button
            onClick={() => fetchData()}
            disabled={loading}
            className="px-3 py-1 text-sm bg-red-600 text-white rounded hover:bg-red-700 disabled:opacity-50"
          >
            Reessayer
          </button>
        </div>
      )}

      {/* Secondary sources failures (non blocking) */}
      {!timeMachineActive && failedSecondary.length > 0 && (
        <div className="bg-yellow-50 border border-yellow-200 rounded-lg px-4 py-2 text-sm text-yellow-800 flex items-center gap-2">
          <AlertTriangle className="w-4 h-4 text-yellow-600 flex-shrink-0" />
          <span>
            Sources indisponibles : {failedSecondary.map(k => SECONDARY_SOURCE_LABELS[k]).join(', ')}
          </span>
        </div>
      )}

      {/* TimeMachine Banner */}
      <TimeMachineBanner
        active={timeMachineActive}
        onToggle={handleTimeMachineToggle}
        selectedDate={timeMachineDate}
        onDateChange={handleTimeMachineDateChange}
        episodes={episodes}
        loading={timeMachineLoading}
      />
      {timeMachineActive && timeMachineError && (
        <div role="alert" className="bg-red-50 border border-red-200 rounded-lg px-4 py-2 text-sm text-red-800 flex items-center gap-2">
          <AlertTriangle className="w-4 h-4 text-red-500 flex-shrink-0" />
          <span>
            Chargement TimeMachine impossible : {timeMachineError}. Les valeurs affichees
            ne correspondent pas a la date choisie.
          </span>
        </div>
      )}

      {/* Data Sources Status - Compact at top */}
      <DataSourcesStatus sources={sources} loading={loading} />

      {/* Alerts */}
      {data.alerts.length > 0 && (
        <div className="space-y-2">
          {data.alerts
            .filter(a => !a.includes('Netatmo') && !a.includes('Mode TimeMachine'))
            .map((alert, i) => (
              <div
                key={i}
                className="bg-orange-50 border border-orange-200 rounded-lg p-4 flex items-center gap-3"
              >
                <AlertTriangle className="w-5 h-5 text-orange-500 flex-shrink-0" />
                <span className="text-orange-800">{alert}</span>
              </div>
            ))}
        </div>
      )}

      {/* Risk Overview */}
      <div className={`rounded-xl border-2 p-6 ${RISK_BG_COLORS[riskLevel]}`}>
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-lg font-medium text-gray-600">Niveau de risque actuel</h2>
            <p className="text-3xl font-bold mt-2">{data.risk.description}</p>
            <div className="mt-4 flex flex-wrap gap-2">
              {data.risk.recommendations.map((rec, i) => (
                <span
                  key={i}
                  className="px-3 py-1 bg-white/50 rounded-full text-sm"
                >
                  {rec}
                </span>
              ))}
            </div>
          </div>
          <RiskGauge
            score={data.risk.score}
            level={riskLevel}
            onClick={() => setBreakdownExpanded(!breakdownExpanded)}
            clickable
          />
        </div>
      </div>

      {/* Risk Breakdown Panel */}
      <RiskBreakdownPanel risk={data.risk} defaultExpanded={breakdownExpanded} />

      {/* HydroMap - Full Width */}
      <HydroMap
        stations={stations}
        netatmoStations={netatmo?.lidoire_stations}
        infoclimatStations={infoclimat?.nearest_stations}
        tide={data.tide}
      />

      {/* Sparklines Row - 4 cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <MetricSparkline
          title="Dordogne"
          value={pessacStation ? `${pessacStation.current_level.height_m.toFixed(2)} m` : '- m'}
          subtitle={pessacStation ? `Pessac · ${pessacStation.tendency}` : 'Pessac indisponible'}
          data={sparklineData.river}
          color="#0891b2"
          icon={<Waves className="w-4 h-4" />}
        />
        <MetricSparkline
          title="Maree"
          value={`Coef ${data.tide.current_coefficient || '-'}`}
          subtitle={
            data.tide.next_high_tide
              ? `PM ${format(new Date(data.tide.next_high_tide.timestamp), 'HH:mm', { locale: fr })}`
              : undefined
          }
          data={sparklineData.tide}
          color="#3b82f6"
          icon={<Waves className="w-4 h-4" />}
        />
        <MetricSparkline
          title="Pluie bassin"
          value={`${basinRainfall?.weighted_average_24h_mm?.toFixed(1) || data.rainfall.last_24h_mm.toFixed(1)} mm`}
          subtitle={basinRainfall ? "24h Netatmo Lidoire" : "24h Open-Meteo"}
          data={sparklineData.rain}
          color="#6366f1"
          icon={<CloudRain className="w-4 h-4" />}
        />
        <MetricSparkline
          title="Risque"
          value={`${data.risk.score.toFixed(0)}/100`}
          subtitle={data.risk.level}
          data={sparklineData.risk}
          color={
            data.risk.level === 'red'
              ? '#ef4444'
              : data.risk.level === 'orange'
                ? '#f97316'
                : data.risk.level === 'yellow'
                  ? '#eab308'
                  : '#22c55e'
          }
          icon={<Activity className="w-4 h-4" />}
        />
      </div>

      {/* Main Grid - 2 columns on desktop */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Left Column - Stations */}
        <StationsPanel
          stations={stations}
          selectedStation={selectedStation}
          onSelectStation={setSelectedStation}
          loading={loading}
        />

        {/* Right Column - Basin Rainfall + Soil + Weather & Forecasts */}
        <div className="space-y-6">
          <NowcastRainChart data={data.nowcast_rain} />

          <BasinRainfallGauge data={basinRainfall} loading={loading} />

          <SoilConditionsPanel
            soil={data.soil}
            loading={loading}
          />

          <RainfallPanel
            netatmo={netatmo}
            infoclimat={infoclimat ? {
              available: infoclimat.rainfall?.available || false,
              stations_found: infoclimat.stations_found,
              nearest_stations: infoclimat.nearest_stations,
              rainfall: infoclimat.rainfall,
            } : undefined}
            forecast={{
              forecast_24h_mm: data.rainfall.forecast_24h_mm,
              forecast_72h_mm: data.rainfall.forecast_72h_mm,
            }}
            loading={loading}
          />

          {/* ForecastPanel v2 - autonomous, fetches its own data */}
          {!timeMachineActive && <ForecastPanel hours={168} autoRefresh={true} />}
        </div>
      </div>

      {/* Quick Data Cards - Secondary info */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        {/* Tide */}
        <DataCard
          title="Maree"
          icon={<Waves className="w-5 h-5" />}
          color="blue"
        >
          <div className="space-y-2">
            <div className="flex justify-between">
              <span className="text-gray-500 flex items-center gap-1">
                Coefficient
                <InfoTooltip {...TOOLTIPS.tideCoefficient} />
              </span>
              <span className="font-bold text-lg">
                {data.tide.current_coefficient || '-'}
              </span>
            </div>
            {data.tide.next_high_tide && (
              <div className="flex justify-between text-sm">
                <span className="text-gray-500 flex items-center gap-1">
                  Prochaine PM
                  <InfoTooltip {...TOOLTIPS.tidePhaseBmPm} />
                </span>
                <span>
                  {format(new Date(data.tide.next_high_tide.timestamp), 'HH:mm', {
                    locale: fr,
                  })}
                </span>
              </div>
            )}
            {data.tide.next_low_tide && (
              <div className="flex justify-between text-sm">
                <span className="text-gray-500 flex items-center gap-1">
                  Prochaine BM
                </span>
                <span>
                  {format(new Date(data.tide.next_low_tide.timestamp), 'HH:mm', {
                    locale: fr,
                  })}
                </span>
              </div>
            )}
          </div>
        </DataCard>

        {/* Selected Station Level */}
        <DataCard
          title={
            stations.find((s) => s.station_id === selectedStation)?.station_name ||
            'Dordogne'
          }
          icon={<TrendingUp className="w-5 h-5" />}
          color="cyan"
        >
          {(() => {
            const station = stations.find((s) => s.station_id === selectedStation)
            if (!station) {
              return (
                <div className="text-gray-500">
                  Selectionnez une station
                </div>
              )
            }
            return (
              <div className="space-y-2">
                <div className="flex items-end justify-between">
                  <span className="text-gray-500">Niveau</span>
                  <div className="flex items-center gap-2">
                    <span className="font-bold text-2xl">
                      {station.current_level.height_m.toFixed(2)}
                    </span>
                    <span className="text-gray-500">m</span>
                    <TendencyIcon tendency={station.tendency} />
                  </div>
                </div>
                <div className="text-sm text-gray-500 flex items-center gap-1">
                  {station.tendency === 'hausse' && (
                    <span className="text-red-500">
                      +{station.rate_cm_per_hour.toFixed(1)} cm/h
                    </span>
                  )}
                  {station.tendency === 'baisse' && (
                    <span className="text-green-500">
                      {station.rate_cm_per_hour.toFixed(1)} cm/h
                    </span>
                  )}
                  {station.tendency === 'stable' && 'Stable'}
                  {station.tendency !== 'stable' && (
                    <InfoTooltip {...TOOLTIPS.rateOfRise} />
                  )}
                </div>
              </div>
            )
          })()}
        </DataCard>

        {/* Rainfall - aggregated max across sources (used by risk model) */}
        <DataCard
          title="Pluie (modele)"
          icon={<Droplets className="w-5 h-5" />}
          color="indigo"
        >
          <div className="space-y-2">
            <div className="flex justify-between">
              <span className="text-gray-500">1h</span>
              <span className="font-bold">
                {data.rainfall.last_hour_mm.toFixed(1)} mm
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-500">24h</span>
              <span className="font-bold">
                {data.rainfall.last_24h_mm.toFixed(1)} mm
              </span>
            </div>
            <div className="flex justify-between text-sm">
              <span className="text-gray-500">72h</span>
              <span>{data.rainfall.last_72h_mm.toFixed(1)} mm</span>
            </div>
            <div className="flex justify-between text-sm">
              <span className="text-gray-500">7j</span>
              <span>{data.rainfall.last_7d_mm?.toFixed(1) || '0.0'} mm</span>
            </div>
            <div className="text-xs text-gray-400 pt-1 border-t">
              Max(Open-Meteo, Netatmo, Infoclimat)
            </div>
          </div>
        </DataCard>
      </div>

      {/* Last Update */}
      <div className="flex items-center justify-between text-sm text-gray-500">
        <div className="flex items-center gap-2">
          <Clock className="w-4 h-4" />
          <span>
            {timeMachineActive && timeMachineDate
              ? `Mode TimeMachine - ${format(timeMachineDate, 'dd/MM/yyyy HH:mm', { locale: fr })}`
              : `Derniere mesure : ${dataDate ? `${format(dataDate, 'dd/MM/yyyy HH:mm:ss', { locale: fr })} (${formatAge(dataDate, now)})` : '-'}`}
          </span>
          {isStale && <span className="text-red-600 font-medium">perimees</span>}
        </div>
        <button
          onClick={() => fetchData()}
          disabled={loading || timeMachineActive}
          className="flex items-center gap-2 px-3 py-1 rounded hover:bg-gray-100 disabled:opacity-50"
        >
          <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          Actualiser
        </button>
      </div>
    </div>
  )
}
