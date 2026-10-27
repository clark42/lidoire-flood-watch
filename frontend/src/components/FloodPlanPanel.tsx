import { useEffect, useMemo, useState } from 'react'
import { AlertTriangle } from 'lucide-react'
import { errorMessage, getEpisodeDrivers, getUnifiedTimeline } from '../api'
import type { EpisodeDriversData, UnifiedTimelineData } from '../types'
import { useElementWidth, useInView, usePrefersReducedMotion } from '../hooks/useChartFrame'
import {
  dailyTrail,
  dayWindow,
  episodePoints,
  floodPhaseCaption,
  todayPoint,
  PHASE_EYRAUD_SERIES,
  PHASE_PESSAC_SERIES,
  type TodayPoint,
  type TrailPoint,
} from '../utils/phasePlane'
import { DAY_MS, HOUR_MS, PESSAC_ID, seriesPoints } from '../utils/replay'
import { parseApiDate } from '../utils/time'
import FloodPhasePlane, { FloodPhaseLegend, FloodPhaseTable, type PhaseTrail } from './FloodPhasePlane'

interface TrailDef {
  id: 'now' | 'feb2026' | 'feb2024'
  label: string
  color: string
  /** Episode key whose peak centres the trail */
  episodeKey?: string
  fallbackPeak?: string
}

const TRAILS: TrailDef[] = [
  { id: 'now', label: '21 derniers jours', color: 'var(--ink)' },
  { id: 'feb2026', label: 'fév. 2026', color: 'var(--silt)', episodeKey: '2026-02-12', fallbackPeak: '2026-02-12T16:00:00Z' },
  { id: 'feb2024', label: 'fév. 2024', color: 'var(--accent)', episodeKey: '2024-02-26', fallbackPeak: '2024-02-26T11:00:00Z' },
]
const TRAIL_DAYS_BEFORE = 14
const TRAIL_DAYS_AFTER = 7
const NOW_TRAIL_DAYS = 21

/** Pessac + Eyraud only: no rain, tide or Infoclimat cache-through */
function fetchLevels(start: Date, end: Date, resolution: '1h' | '1d', withPessac = true): Promise<UnifiedTimelineData> {
  return getUnifiedTimeline(
    start.toISOString(),
    end.toISOString(),
    withPessac ? [PESSAC_ID] : [],
    [],
    false,
    false,
    resolution,
    false,
    false,
    false,
    false,
    undefined,
    false,
    [PHASE_EYRAUD_SERIES],
  ).then((r) => r.data)
}

/** "Plan des crues" section: drivers of the labelled episodes, today and trails. */
export default function FloodPlanPanel() {
  const [viewRef, inView] = useInView<HTMLDivElement>()
  const [widthRef, width] = useElementWidth<HTMLDivElement>(640)
  const reducedMotion = usePrefersReducedMotion()
  const [now] = useState(() => Date.now())
  const [drivers, setDrivers] = useState<EpisodeDriversData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [fallback, setFallback] = useState<Record<string, number | null>>({})
  const [trails, setTrails] = useState<Partial<Record<TrailDef['id'], TrailPoint[]>>>({})
  const [today, setToday] = useState<TodayPoint | null>(null)
  const [shown, setShown] = useState<Set<TrailDef['id']>>(() => new Set(TRAILS.map((t) => t.id)))

  useEffect(() => {
    if (!inView) return
    let alive = true
    getEpisodeDrivers()
      .then((d) => alive && setDrivers(d))
      .catch((err) => alive && setError(errorMessage(err)))
    fetchLevels(new Date(now - NOW_TRAIL_DAYS * DAY_MS), new Date(now), '1d')
      .then((d) => alive && setTrails((t) => ({ ...t, now: dailyTrail(d) })))
      .catch((err) => console.error('Flood plan: current trail unavailable', err))
    fetchLevels(new Date(now - 36 * HOUR_MS), new Date(now), '1h')
      .then((d) => {
        if (!alive) return
        setToday(todayPoint(seriesPoints(d, PHASE_PESSAC_SERIES), seriesPoints(d, PHASE_EYRAUD_SERIES), now))
      })
      .catch((err) => console.error('Flood plan: today unavailable', err))
    return () => {
      alive = false
    }
  }, [inView, now])

  // Trails of the two Februaries, centred on their labelled peak
  useEffect(() => {
    if (!inView || (!drivers && !error)) return
    let alive = true
    for (const def of TRAILS) {
      if (!def.episodeKey) continue
      const peak =
        parseApiDate(drivers?.episodes.find((e) => e.key === def.episodeKey)?.peak_time) ?? parseApiDate(def.fallbackPeak)
      if (!peak) continue
      const start = new Date(Math.floor(peak.getTime() / DAY_MS) * DAY_MS - TRAIL_DAYS_BEFORE * DAY_MS)
      const end = new Date(Math.min(now, start.getTime() + (TRAIL_DAYS_BEFORE + TRAIL_DAYS_AFTER + 1) * DAY_MS))
      fetchLevels(start, end, '1d')
        .then((d) => alive && setTrails((t) => ({ ...t, [def.id]: dailyTrail(d) })))
        .catch((err) => console.error(`Flood plan: trail ${def.id} unavailable`, err))
    }
    return () => {
      alive = false
    }
  }, [inView, drivers, error, now])

  // Episodes without an hourly Eyraud point in their 12.5 h window (the
  // stored history is daily on some periods): daily max of the peak day
  useEffect(() => {
    if (!drivers) return
    let alive = true
    for (const e of drivers.episodes) {
      if (e.values.eyraud_q_m3s != null) continue
      const day = dayWindow(e.peak_time)
      if (!day) continue
      fetchLevels(new Date(day.start), new Date(day.end), '1d', false)
        .then((d) => {
          if (!alive) return
          const values = seriesPoints(d, PHASE_EYRAUD_SERIES).map((p) => p.v)
          setFallback((f) => ({ ...f, [e.key]: values.length ? Math.max(...values) : null }))
        })
        .catch((err) => console.error(`Flood plan: daily Eyraud unavailable for ${e.key}`, err))
    }
    return () => {
      alive = false
    }
  }, [drivers])

  const episodes = useMemo(() => episodePoints(drivers, fallback), [drivers, fallback])
  const phaseTrails: PhaseTrail[] = TRAILS.filter((t) => shown.has(t.id)).map((t) => ({
    id: t.id,
    label: t.label,
    color: t.color,
    points: trails[t.id] ?? [],
    animate: t.id !== 'now' && !reducedMotion,
  }))
  const toggle = (id: TrailDef['id']) =>
    setShown((s) => {
      const next = new Set(s)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  return (
    <div ref={viewRef} className="rounded-card border border-line bg-surface p-3 shadow-card sm:p-4">
      <div ref={widthRef} className="min-w-0 space-y-3">
        <div role="group" aria-label="Traces affichées" className="flex flex-wrap gap-1.5">
          {TRAILS.map((t) => (
            <button
              key={t.id}
              type="button"
              aria-pressed={shown.has(t.id)}
              onClick={() => toggle(t.id)}
              className={`inline-flex min-h-[44px] items-center gap-2 rounded-full border px-3 text-[13.5px] sm:min-h-[34px] ${
                shown.has(t.id) ? 'border-ink text-ink' : 'border-line-2 text-ink-3'
              }`}
            >
              <span className="inline-block h-0.5 w-4" style={{ background: t.color }} aria-hidden="true" />
              {t.id === 'now' ? 'Trace actuelle' : `Trace ${t.label}`}
            </button>
          ))}
        </div>
        {drivers ? (
          <>
            <FloodPhasePlane episodes={episodes} today={today} trails={phaseTrails} width={width} />
            <FloodPhaseLegend trails={TRAILS} />
            <p className="max-w-[75ch] text-[13.5px] text-ink-2">{floodPhaseCaption(episodes)}</p>
            {drivers.partial && (
              <p className="text-[13px] text-ink-3">Certaines valeurs manquent (requêtes en échec : {drivers.errors.join(', ') || 'épisodes'}).</p>
            )}
            <FloodPhaseTable episodes={episodes} />
          </>
        ) : error ? (
          <p role="alert" className="flex items-start gap-2 rounded-lg border border-line-2 px-3 py-2 text-[14px] text-ink-2">
            <AlertTriangle className="mt-0.5 h-4 w-4 flex-none" aria-hidden="true" />
            Valeurs des épisodes indisponibles ({error}).
          </p>
        ) : (
          <div className="h-[360px] w-full animate-pulse rounded-lg bg-surface-2" aria-hidden="true" />
        )}
      </div>
    </div>
  )
}
