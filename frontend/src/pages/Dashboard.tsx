import { useState, useEffect, useCallback, useRef, lazy, Suspense } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  Droplets,
  Waves,
  TrendingUp,
  TrendingDown,
  Minus,
  Clock,
  AlertTriangle,
  RefreshCw,
  CloudRain,
} from 'lucide-react'
import {
  getRealtimeData,
  getNetatmoData,
  getInfoclimatData,
  getStatus,
  getBasinRainfall,
  getRealtimeDataAt,
  getEpisodesList,
  getUnifiedTimeline,
} from '../api'
import type {
  RealtimeData,
  NetatmoData,
  InfoclimatData,
  SystemStatus,
  BasinRainfallData,
  FloodEpisode,
  UnifiedTimelineData,
} from '../types'
import DataCard from '../components/DataCard'
import StationsPanel from '../components/StationsPanel'
import RainfallPanel from '../components/RainfallPanel'
import DataSourcesStatus from '../components/DataSourcesStatus'
import RiskBreakdownPanel from '../components/RiskBreakdownPanel'
import TimeMachineBanner from '../components/TimeMachineBanner'
import SoilConditionsPanel from '../components/SoilConditionsPanel'
import VigilanceBadges from '../components/VigilanceBadges'
import ObservationQuickForm from '../components/ObservationQuickForm'
import IndicatorsPanel from '../components/IndicatorsPanel'
import InfoTooltip from '../components/InfoTooltip'
import VerdictHero from '../components/VerdictHero'
import ActionChecklist from '../components/ActionChecklist'
import SectionNav from '../components/SectionNav'
import DashboardSection from '../components/DashboardSection'
import LevelMeter from '../components/ui/LevelMeter'
import { ValueTagLegend } from '../components/ui/ValueTag'
import { useRiskOutlook } from '../hooks/useRiskOutlook'
import { DASHBOARD_SECTIONS } from './dashboardSections'
import { TOOLTIPS } from '../tooltipContent'
import { formatAge, formatParis, parseApiDate } from '../utils/time'
import { formatNumber } from '../utils/format'

// leaflet is only needed by the map: separate chunk, the rest renders first
const HydroMap = lazy(() => import('../components/HydroMap'))
// recharts (~100 kB gzip) is only needed below the verdict: the panels that
// draw charts load in a separate chunk, after the decision is on screen
const ForecastPanel = lazy(() => import('../components/ForecastPanel'))
const EnsemblePanel = lazy(() => import('../components/EnsemblePanel'))
const MetricSparkline = lazy(() => import('../components/MetricSparkline'))
const NowcastRainChart = lazy(() => import('../components/NowcastRainChart'))
const BasinRainfallGauge = lazy(() => import('../components/BasinRainfallGauge'))

function PanelFallback({ height = 'h-40' }: { height?: string }) {
  return <div className={`${height} w-full animate-pulse rounded-card bg-surface-2`} aria-hidden="true" />
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

// Sparklines: last 24 h from /api/timeline/unified (re-seeded every 30 min),
// live points appended between seeds.
const SPARKLINE_WINDOW_MS = 24 * 3600_000
const SPARKLINE_SEED_EVERY_MS = 30 * 60_000
const SPARKLINE_SEED_RETRY_MS = 5 * 60_000
const SPARKLINE_RAIN_SOURCE = 'open-meteo:Moulin-Lidoire'
const SPARKLINE_SERIES = {
  river: `river:${PESSAC_STATION_ID}`,
  // Tide estimated at the Lidoire confluence (tide_port_confluence)
  tide: 'tide:LIDOIRE',
  rain: `rainfall:${SPARKLINE_RAIN_SOURCE}`,
  risk: 'risk:score',
} as const

type SparkPoint = { t: number; v: number }
type SparklineKey = keyof typeof SPARKLINE_SERIES
type SparklineSet = Record<SparklineKey, SparkPoint[]>

const EMPTY_SPARKLINES: SparklineSet = { river: [], tide: [], rain: [], risk: [] }

/** Union by timestamp (newest value wins), limited to the last 24 h, sorted. */
function mergeSparkPoints(prev: SparkPoint[], added: SparkPoint[], now: number): SparkPoint[] {
  const byTime = new Map<number, number>()
  for (const p of prev) byTime.set(p.t, p.v)
  for (const p of added) byTime.set(p.t, p.v)
  return Array.from(byTime, ([t, v]) => ({ t, v }))
    .filter(p => p.t >= now - SPARKLINE_WINDOW_MS)
    .sort((a, b) => a.t - b.t)
}

function seriesSparkPoints(data: UnifiedTimelineData, key: string): SparkPoint[] {
  const points: SparkPoint[] = []
  for (const p of data.series[key]?.data ?? []) {
    const date = parseApiDate(p.timestamp)
    if (date && p.value != null) points.push({ t: date.getTime(), v: p.value })
  }
  return points
}

/**
 * Time of the last measurement of the primary station (Pessac). RealtimeData
 * .timestamp is the request time (always "now"), and the max over all stations
 * would hide a stale Pessac behind a fresh Libourne/Vigicrues point.
 */
function primaryMeasurementDate(d: RealtimeData): Date | null {
  const primaryId = d.stations?.primary_station_id || PESSAC_STATION_ID
  const primary = d.stations?.stations.find(s => s.station_id === primaryId)
  return parseApiDate(primary?.current_level?.timestamp)
}

/** Stations whose last point is older than STALE_AFTER_MS (or missing). */
function staleStationNames(d: RealtimeData, now: number): string[] {
  return (d.stations?.stations ?? [])
    .filter(s => {
      const at = parseApiDate(s.current_level?.timestamp)
      return !at || now - at.getTime() > STALE_AFTER_MS
    })
    .map(s => s.station_name)
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
  const [sources, setSources] = useState<SystemStatus['sources'] | null>(null)
  const [selectedStation, setSelectedStation] = useState<string | null>(null)
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

  // 6 / 24 / 72 h outlook (verdict and checklist), live mode only
  const outlook = useRiskOutlook(!timeMachineActive)

  // Refs used by the polling loop (avoid stale closures and overlapping polls)
  const inFlightRef = useRef(false)
  const timeMachineActiveRef = useRef(timeMachineActive)
  useEffect(() => {
    timeMachineActiveRef.current = timeMachineActive
  }, [timeMachineActive])

  // Sparklines: seeded with the last 24 h, then live points appended
  const [sparklineData, setSparklineData] = useState<SparklineSet>(EMPTY_SPARKLINES)
  const nextSeedAtRef = useRef(0)

  const seedSparklines = useCallback(async () => {
    const now = Date.now()
    try {
      const { data } = await getUnifiedTimeline(
        new Date(now - SPARKLINE_WINDOW_MS).toISOString(),
        new Date(now).toISOString(),
        [PESSAC_STATION_ID],
        [SPARKLINE_RAIN_SOURCE],
        true, // tide
        true, // risk score
        '1h',
        false, // soil
        false, // infoclimat
      )
      setSparklineData(prev => {
        const next = { ...prev }
        for (const key of Object.keys(SPARKLINE_SERIES) as SparklineKey[]) {
          next[key] = mergeSparkPoints(prev[key], seriesSparkPoints(data, SPARKLINE_SERIES[key]), now)
        }
        return next
      })
    } catch (err) {
      // Non blocking: live points keep accumulating, retry sooner
      console.error('Sparkline history unavailable:', err)
      nextSeedAtRef.current = now + SPARKLINE_SEED_RETRY_MS
    }
  }, [])

  const fetchData = useCallback(async () => {
    // In-flight guard: a slow backend must not stack overlapping polls
    if (inFlightRef.current) return
    inFlightRef.current = true
    try {
      if (Date.now() >= nextSeedAtRef.current) {
        nextSeedAtRef.current = Date.now() + SPARKLINE_SEED_EVERY_MS
        void seedSparklines()
      }
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
        getStatus(),
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

        // Append live points (the tide curve comes from the seed only:
        // /realtime has no current tide height)
        const t = Date.now()
        const pessac = d.stations?.stations.find(s => s.station_id === PESSAC_STATION_ID)
        const pessacAt = parseApiDate(pessac?.current_level?.timestamp)
        setSparklineData(prev => ({
          // "Dordogne" reference is Pessac (Hub'Eau), not Libourne (tidal)
          river: pessac && pessacAt
            ? mergeSparkPoints(prev.river, [{ t: pessacAt.getTime(), v: pessac.current_level.height_m }], t)
            : prev.river,
          tide: prev.tide,
          // Hourly rain (mm/h), same unit as the Open-Meteo history
          rain: mergeSparkPoints(prev.rain, [{ t, v: d.rainfall.last_hour_mm }], t),
          risk: mergeSparkPoints(prev.risk, [{ t, v: d.risk.score }], t),
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
      if (sourcesResult.status === 'fulfilled') setSources(sourcesResult.value.sources ?? null)
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
  }, [seedSparklines])

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
      // The banner moves to the top of the page in TimeMachine mode
      requestAnimationFrame(() => document.getElementById('maintenant')?.scrollIntoView())
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

  const stations = data.stations?.stations || []
  const pessacStation = stations.find(s => s.station_id === PESSAC_STATION_ID)

  // Freshness is based on the primary station measurement, not on the request time
  const primaryName = stations.find(
    s => s.station_id === (data.stations?.primary_station_id || PESSAC_STATION_ID)
  )?.station_name ?? 'Pessac'
  const dataDate = primaryMeasurementDate(data)
  const dataAgeMs = dataDate ? now - dataDate.getTime() : null
  const staleStations = timeMachineActive ? [] : staleStationNames(data, now)
  const isStale = !timeMachineActive && (
    error !== null || dataAgeMs === null || dataAgeMs > STALE_AFTER_MS
  )

  const timeMachineBanner = (
    <TimeMachineBanner
      active={timeMachineActive}
      onToggle={handleTimeMachineToggle}
      selectedDate={timeMachineDate}
      onDateChange={handleTimeMachineDateChange}
      episodes={episodes}
      loading={timeMachineLoading}
    />
  )
  const alerts = data.alerts.filter(a => !a.includes('Netatmo') && !a.includes('Mode TimeMachine'))
  const selected = stations.find((s) => s.station_id === selectedStation)

  return (
    <div>
      <SectionNav sections={DASHBOARD_SECTIONS} />

      {/* 1. Decision: verdict, horizons, confidence, official vigilances */}
      <DashboardSection id="maintenant" className="!pt-4">
        {/* Stale / fetch error banner (live mode only) */}
        {isStale && (
          <div
            role="alert"
            className="bg-red-50 border border-red-200 rounded-lg p-4 flex flex-wrap items-start gap-3"
          >
            <AlertTriangle className="w-5 h-5 text-red-500 flex-shrink-0 mt-0.5" />
            <div className="flex-1 min-w-0 text-sm text-red-800">
              <p className="font-medium">
                Données non actualisées
                {dataDate
                  ? ` — dernière mesure ${primaryName} ${formatAge(dataDate, now)}`
                  : ` — aucune mesure ${primaryName}`}
              </p>
              {staleStations.length > 0 && (
                <p className="mt-1">Stations sans mesure depuis 3 h : {staleStations.join(', ')}</p>
              )}
              {error && (
                <p className="mt-1">
                  Échec du chargement temps réel
                  {lastFailureAt ? ` (${formatParis(lastFailureAt, 'HH:mm:ss')})` : ''}
                  {' '}: {error}. Les valeurs affichées peuvent être périmées.
                </p>
              )}
            </div>
            <button
              onClick={() => fetchData()}
              disabled={loading}
              className="min-h-[44px] px-3 text-sm bg-red-600 text-white rounded hover:bg-red-700 disabled:opacity-50 sm:min-h-0 sm:py-1"
            >
              Réessayer
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

        {timeMachineActive ? (
          <>
            {timeMachineBanner}
            {timeMachineError && (
              <div role="alert" className="bg-red-50 border border-red-200 rounded-lg px-4 py-2 text-sm text-red-800 flex items-center gap-2">
                <AlertTriangle className="w-4 h-4 text-red-500 flex-shrink-0" />
                <span>
                  Chargement TimeMachine impossible : {timeMachineError}. Les valeurs affichées
                  ne correspondent pas à la date choisie.
                </span>
              </div>
            )}
            {/* The outlook is live only: show the score of the chosen date */}
            <div className="rounded-card border border-line bg-surface p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="eyebrow">Score à cette date</span>
                <LevelMeter level={data.risk.level} className="text-[13px]" />
              </div>
              <p className="num mt-1 text-[40px] font-semibold leading-none">
                {formatNumber(data.risk.score, 0)}
                <span className="text-[0.5em] text-ink-3"> /100</span>
              </p>
              <p className="mt-2 text-ink-2">{data.risk.description}</p>
            </div>
          </>
        ) : (
          <VerdictHero
            outlook={outlook.data}
            loading={outlook.loading}
            error={outlook.error}
            onRetry={outlook.refresh}
            current={{ score: data.risk.score, level: data.risk.level }}
            sources={sources}
            vigilance={<VigilanceBadges />}
            now={new Date(now)}
          />
        )}

        {/* Alerts of the realtime model */}
        {alerts.length > 0 && (
          <div className="space-y-2">
            {alerts.map((alert, i) => (
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

        <ValueTagLegend />
      </DashboardSection>

      {/* 2. Why: measured drivers, hydrological indicators, score detail */}
      <DashboardSection id="pourquoi">
        <Suspense fallback={<PanelFallback height="h-28" />}>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <MetricSparkline
              title="Dordogne"
              value={pessacStation ? `${pessacStation.current_level.height_m.toFixed(2)} m` : '- m'}
              subtitle={pessacStation ? `Pessac · ${pessacStation.tendency}` : 'Pessac indisponible'}
              data={sparklineData.river}
              color="#0891b2"
              icon={<Waves className="w-4 h-4" />}
            />
            <MetricSparkline
              title="Marée"
              value={`Coef ${data.tide.current_coefficient || '-'}`}
              subtitle={
                data.tide.next_high_tide
                  ? `PM ${formatParis(data.tide.next_high_tide.timestamp, 'HH:mm')} · hauteur 24 h`
                  : undefined
              }
              data={sparklineData.tide}
              color="#3b82f6"
              icon={<Waves className="w-4 h-4" />}
            />
            <MetricSparkline
              title="Pluie bassin"
              value={`${(basinRainfall?.available
                ? basinRainfall.weighted_average_24h_mm
                : data.rainfall.last_24h_mm
              ).toFixed(1)} mm`}
              subtitle={basinRainfall?.available
                ? `24h Netatmo Lidoire (${basinRainfall.stations_count} st.)`
                : '24h modèle · Netatmo indisponible'}
              data={sparklineData.rain}
              color="#6366f1"
              icon={<CloudRain className="w-4 h-4" />}
            />
          </div>
        </Suspense>
        {!timeMachineActive && <IndicatorsPanel />}
        <RiskBreakdownPanel risk={data.risk} />
      </DashboardSection>

      {/* 3. Next hours: forecast score, nowcast, ensembles */}
      <DashboardSection id="prochaines-heures">
        <Suspense fallback={<PanelFallback height="h-72" />}>
          {!timeMachineActive && <ForecastPanel hours={168} autoRefresh={true} />}
          <NowcastRainChart data={data.nowcast_rain} />
          {!timeMachineActive && <EnsemblePanel />}
        </Suspense>
      </DashboardSection>

      {/* 4. Act: owner's checklist, field observation */}
      <DashboardSection id="agir">
        {timeMachineActive ? (
          <p className="text-ink-2">Liste et relevé terrain disponibles en direct uniquement.</p>
        ) : (
          <div className="grid grid-cols-1 min-[960px]:grid-cols-2 gap-4">
            {outlook.data ? (
              <ActionChecklist checklist={outlook.data.checklist} now={new Date(now)} />
            ) : (
              <p className="text-ink-2">
                {outlook.loading ? 'Chargement de la liste…' : 'Liste indisponible : prévision absente.'}
              </p>
            )}
            <ObservationQuickForm />
          </div>
        )}
      </DashboardSection>

      {/* 5. Replay (TimeMachine entry; the banner moves to the top once active) */}
      <DashboardSection id="replay">
        {timeMachineActive ? (
          <p className="text-ink-2">
            TimeMachine actif : le bandeau en haut de page permet de changer de date ou de revenir au direct.
          </p>
        ) : timeMachineBanner}
      </DashboardSection>

      {/* 6. Map and stations */}
      <DashboardSection id="carte">
        {/* isolate: Leaflet panes (z-index 400-1000) stay under the sticky section bar */}
        <div className="isolate">
          <Suspense fallback={<div className="w-full h-[380px] rounded-xl shadow bg-surface-2 animate-pulse" />}>
            <HydroMap
              stations={stations}
              netatmoStations={netatmo?.lidoire_stations}
              infoclimatStations={infoclimat?.nearest_stations}
              tide={data.tide}
            />
          </Suspense>
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <StationsPanel
            stations={stations}
            selectedStation={selectedStation}
            onSelectStation={setSelectedStation}
            loading={loading}
          />
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-1 gap-4 content-start">
            {/* Selected Station Level */}
            <DataCard
              title={selected?.station_name || 'Dordogne'}
              icon={<TrendingUp className="w-5 h-5" />}
              color="cyan"
            >
              {!selected ? (
                <div className="text-gray-500">Sélectionnez une station</div>
              ) : (
                <div className="space-y-2">
                  <div className="flex items-end justify-between">
                    <span className="text-gray-500">Niveau</span>
                    <div className="flex items-center gap-2">
                      <span className="font-bold text-2xl">
                        {selected.current_level.height_m.toFixed(2)}
                      </span>
                      <span className="text-gray-500">m</span>
                      <TendencyIcon tendency={selected.tendency} />
                    </div>
                  </div>
                  <div className="text-sm text-gray-500 flex items-center gap-1">
                    {selected.tendency === 'hausse' && (
                      <span className="text-red-500">
                        +{selected.rate_cm_per_hour.toFixed(1)} cm/h
                      </span>
                    )}
                    {selected.tendency === 'baisse' && (
                      <span className="text-green-500">
                        {selected.rate_cm_per_hour.toFixed(1)} cm/h
                      </span>
                    )}
                    {selected.tendency === 'stable' && 'Stable'}
                    {selected.tendency !== 'stable' && (
                      <InfoTooltip {...TOOLTIPS.rateOfRise} />
                    )}
                  </div>
                </div>
              )}
            </DataCard>

            {/* Tide */}
            <DataCard
              title="Marée"
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
                    <span>{formatParis(data.tide.next_high_tide.timestamp, 'HH:mm')}</span>
                  </div>
                )}
                {data.tide.next_low_tide && (
                  <div className="flex justify-between text-sm">
                    <span className="text-gray-500 flex items-center gap-1">
                      Prochaine BM
                    </span>
                    <span>{formatParis(data.tide.next_low_tide.timestamp, 'HH:mm')}</span>
                  </div>
                )}
              </div>
            </DataCard>
          </div>
        </div>
      </DashboardSection>

      {/* 7. Rain and soil */}
      <DashboardSection id="pluie-sol">
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <div className="space-y-6 min-w-0">
            <Suspense fallback={<PanelFallback />}>
              <BasinRainfallGauge data={basinRainfall} loading={loading} />
            </Suspense>
            {/* Rainfall - aggregated max across sources (used by risk model) */}
            <DataCard
              title="Pluie (modèle)"
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
                <div className="text-xs text-gray-500 pt-1 border-t">
                  Max(Open-Meteo, Netatmo, Infoclimat)
                </div>
              </div>
            </DataCard>
            <SoilConditionsPanel soil={data.soil} loading={loading} />
          </div>
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
        </div>
      </DashboardSection>

      {/* 8. Reliability: source freshness, last update */}
      <DashboardSection id="fiabilite">
        <DataSourcesStatus
          sources={sources}
          nextHighTide={data.tide.next_high_tide?.timestamp}
          now={now}
          loading={loading}
        />
        <div className="flex flex-wrap items-center justify-between gap-2 text-sm text-ink-3">
          <div className="flex flex-wrap items-center gap-2 min-w-0">
            <Clock className="w-4 h-4 flex-none" aria-hidden="true" />
            <span className="font-mono text-[12.5px]">
              {timeMachineActive && timeMachineDate
                ? `Mode TimeMachine - ${formatParis(timeMachineDate, 'dd/MM/yyyy HH:mm')}`
                : `Dernière mesure ${primaryName} : ${dataDate ? `${formatParis(dataDate, 'dd/MM/yyyy HH:mm:ss')} (${formatAge(dataDate, now)})` : '-'}`}
            </span>
            {isStale && <span className="text-red-600 font-medium">périmées</span>}
            {!isStale && staleStations.length > 0 && (
              <span className="text-orange-600" title="Sans mesure depuis 3 h">
                périmées : {staleStations.join(', ')}
              </span>
            )}
          </div>
          <button
            onClick={() => fetchData()}
            disabled={loading || timeMachineActive}
            className="flex min-h-[44px] items-center gap-2 px-3 rounded hover:bg-surface-2 disabled:opacity-50 sm:min-h-0 sm:py-1"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} aria-hidden="true" />
            Actualiser
          </button>
        </div>
      </DashboardSection>
    </div>
  )
}
