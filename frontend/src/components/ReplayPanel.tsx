import { useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle } from 'lucide-react'
import { errorMessage, getEpisodeLabels, getLidoireProfile, getUnifiedTimeline, isAbortError } from '../api'
import type { EpisodeLabel, LidoireProfileData, UnifiedTimelineData } from '../types'
import { useInView, usePrefersReducedMotion } from '../hooks/useChartFrame'
import { useElementWidth } from '../hooks/useElementWidth'
import { confluenceParamsFrom } from '../utils/lidoireProfile'
import { formatNumber } from '../utils/format'
import {
  EYRAUD_SERIES,
  HOUR_MS,
  LIBOURNE_ID,
  PESSAC_ID,
  REPLAY_RAIN_SOURCE,
  hourlyTimes,
  nearestIndex,
  nextPlayIndex,
  replayFrame,
  replaySeries,
  replayStory,
  replayWindow,
  type ReplayFrame,
  type ReplayPresetId,
} from '../utils/replay'
import LidoireProfile, { LidoireProfileReadout } from './LidoireProfile'
import ReplayScrubber from './ReplayScrubber'
import SyncStrips from './SyncStrips'

// Module caches: a replay window or a profile hour is fetched once per page
// load (the API caches too: 60 s, 1 h for times older than a day).
const TIMELINE_CACHE = new Map<string, { data: UnifiedTimelineData; pending: string[] }>()
const PROFILE_CACHE = new Map<string, LidoireProfileData>()
const PROFILE_CACHE_MAX = 300

/** Ground profile fetch (points), shared by every window */
let groundPromise: Promise<LidoireProfileData> | null = null

const PLAY_INTERVAL_MS = 80
const PLAY_INTERVAL_REDUCED_MS = 400
/** The API is asked for the cursor hour once the scrubber rests that long */
const SETTLE_MS = 350

function hourKey(t: number): string {
  return new Date(Math.floor(t / HOUR_MS) * HOUR_MS).toISOString()
}

/** The API value replaces the local estimate of the same hour. */
function withApi(frame: ReplayFrame, api: LidoireProfileData | undefined): ReplayFrame & { source: 'api' | 'local' } {
  if (!api || api.confluence.z_c_m_ngf == null) return { ...frame, source: 'local' }
  const b = api.backwater
  return {
    ...frame,
    zc: api.confluence.z_c_m_ngf,
    reach: {
      status: b.status,
      distance_m: b.distance_m,
      connected_distance_m: b.connected_distance_m,
      disconnected_low_points: b.disconnected_low_points,
      z_m: b.z_m,
      distanceBelowMillM: b.distance_below_mill_m,
      connectedDistanceBelowMillM: b.connected_distance_below_mill_m,
      reachesMill: b.reaches_mill,
    },
    source: 'api',
  }
}

export interface ReplayPanelProps {
  /** Date of the TimeMachine (?tm=): the replay opens around it */
  focusAt?: Date | null
  /** Opens the whole Dashboard at a date (TimeMachine, ?tm= in the URL) */
  onOpenDashboardAt?: (date: Date) => void
}

/**
 * "Coupe & replay" section: the Lidoire long profile animated by a scrubber
 * over 10 days or a labelled flood, with the synchronised strips.
 */
export default function ReplayPanel({ focusAt = null, onOpenDashboardAt }: ReplayPanelProps) {
  const [viewRef, inView] = useInView<HTMLDivElement>()
  const [widthRef, width] = useElementWidth<HTMLDivElement>(640)
  const reducedMotion = usePrefersReducedMotion()
  const [now, setNow] = useState(() => new Date())
  const [presetId, setPresetId] = useState<ReplayPresetId>(focusAt ? 'custom' : 'now')
  const [customAt, setCustomAt] = useState<Date | null>(focusAt)
  const [labels, setLabels] = useState<EpisodeLabel[] | null>(null)
  const [ground, setGround] = useState<LidoireProfileData | null>(null)
  const [groundError, setGroundError] = useState<string | null>(null)
  const [loaded, setLoaded] = useState<{ key: string; data: UnifiedTimelineData; pending: string[] } | null>(null)
  const [timelineError, setTimelineError] = useState<string | null>(null)
  const [index, setIndex] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [floorZ, setFloorZ] = useState<number | null>(null)
  const [, setApiVersion] = useState(0)

  // Labels (hour-precise peaks of the presets) and the ground profile
  useEffect(() => {
    if (!inView) return
    let alive = true
    getEpisodeLabels()
      .then((set) => alive && setLabels(set.episodes))
      .catch((err) => console.error('Replay: labels unavailable', err))
    groundPromise ??= getLidoireProfile(undefined, true)
    groundPromise
      .then((p) => alive && setGround(p))
      .catch((err) => {
        groundPromise = null
        if (alive) setGroundError(errorMessage(err))
      })
    return () => {
      alive = false
    }
  }, [inView])

  const win = useMemo(() => replayWindow(presetId, labels, now, customAt), [presetId, labels, now, customAt])
  // Primitive keys: a new window object with the same bounds changes nothing
  const startMs = win.start.getTime()
  const endMs = win.end.getTime()
  const windowKey = `${startMs}|${endMs}`
  const times = useMemo(() => hourlyTimes(new Date(startMs), new Date(endMs)), [startMs, endMs])

  // New window: cursor on its focus (peak, chosen date or now), player stopped
  const focusT = win.focus.getTime()
  useEffect(() => {
    setPlaying(false)
    setIndex(nearestIndex(times, focusT))
  }, [times, focusT])

  // TimeMachine date chosen elsewhere (top banner, ?tm=, the button below):
  // move the cursor when it falls in the window, else open a window around it
  const focusMs = focusAt?.getTime() ?? null
  const timesRef = useRef(times)
  timesRef.current = times
  useEffect(() => {
    if (focusMs == null) return
    const ts = timesRef.current
    if (ts.length > 0 && focusMs >= ts[0] && focusMs <= ts[ts.length - 1]) {
      setPlaying(false)
      setIndex(nearestIndex(ts, focusMs))
      return
    }
    setPresetId('custom')
    setCustomAt(new Date(focusMs))
  }, [focusMs])

  // Series of the window (+ one envelope before its start, for Z_c)
  useEffect(() => {
    if (!inView) return
    const cached = TIMELINE_CACHE.get(windowKey)
    if (cached) {
      setLoaded({ key: windowKey, ...cached })
      setTimelineError(null)
      return
    }
    const controller = new AbortController()
    setTimelineError(null)
    getUnifiedTimeline(
      new Date(startMs - 13 * HOUR_MS).toISOString(),
      new Date(endMs).toISOString(),
      [PESSAC_ID, LIBOURNE_ID],
      [REPLAY_RAIN_SOURCE],
      false, // tide
      true, // risk score
      '1h',
      false, // soil
      false, // infoclimat (would trigger its own backfill)
      false, // wind
      false, // forecast
      controller.signal,
      true, // shadow 3.4-gate
      [EYRAUD_SERIES],
      true, // shadow 4.0a
    )
      .then(({ data, backfillPending }) => {
        // A window still being backfilled is not cached: the next visit refetches
        if (backfillPending.length === 0) TIMELINE_CACHE.set(windowKey, { data, pending: [] })
        setLoaded({ key: windowKey, data, pending: backfillPending })
      })
      .catch((err) => {
        if (!isAbortError(err)) setTimelineError(errorMessage(err))
      })
    return () => controller.abort()
  }, [inView, windowKey, startMs, endMs])

  // Player: 1 h every 80 ms, or 6 h every 400 ms under reduced motion
  useEffect(() => {
    if (!playing) return
    const id = setInterval(
      () => {
        setIndex((i) => nextPlayIndex(i, times.length, reducedMotion))
      },
      reducedMotion ? PLAY_INTERVAL_REDUCED_MS : PLAY_INTERVAL_MS,
    )
    return () => clearInterval(id)
  }, [playing, times.length, reducedMotion])
  useEffect(() => {
    if (playing && index >= times.length - 1) setPlaying(false)
  }, [playing, index, times.length])

  const cursorT = times[Math.min(index, times.length - 1)] ?? null

  // Authoritative Z_c and reach from the API once the cursor rests
  useEffect(() => {
    if (playing || cursorT == null || !ground) return
    const key = hourKey(cursorT)
    if (PROFILE_CACHE.has(key)) return
    const controller = new AbortController()
    const timer = setTimeout(() => {
      getLidoireProfile(key, false)
        .then((p) => {
          if (controller.signal.aborted) return
          if (PROFILE_CACHE.size >= PROFILE_CACHE_MAX) PROFILE_CACHE.delete(PROFILE_CACHE.keys().next().value!)
          PROFILE_CACHE.set(key, p)
          setApiVersion((v) => v + 1)
        })
        .catch((err) => {
          if (!isAbortError(err)) console.error('Replay: profile unavailable at', key, err)
        })
    }, SETTLE_MS)
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [playing, cursorT, ground])

  const params = useMemo(() => confluenceParamsFrom(ground), [ground])
  const points = useMemo(() => ground?.profile.points ?? [], [ground])
  const millD = ground?.mill.d_m ?? 5833.4
  const data = loaded?.key === windowKey ? loaded.data : null
  const series = useMemo(() => replaySeries(data), [data])
  const zcSeries = useMemo(
    () =>
      data
        ? times
            .map((t) => ({ t, v: replayFrame(series, t, [], millD, params).zc }))
            .filter((p): p is { t: number; v: number } => p.v != null)
        : [],
    [data, times, series, millD, params],
  )

  const frame = cursorT != null && data ? withApi(replayFrame(series, cursorT, points, millD, params), PROFILE_CACHE.get(hourKey(cursorT))) : null
  const story = useMemo(() => {
    if (!data || times.length === 0) return ''
    const peakT = win.peak ? times[nearestIndex(times, win.peak.getTime())] : null
    const atPeak = peakT != null ? replayFrame(series, peakT, points, millD, params) : null
    const last = replayFrame(series, times[times.length - 1], points, millD, params)
    return replayStory(presetId, series, win, atPeak, last)
  }, [data, times, series, points, millD, params, presetId, win])

  const isLive = presetId === 'now' && index >= times.length - 1
  const marker = win.peak && (win.label?.outcome ?? 'flooded') === 'flooded' ? win.peak.getTime() : null
  const millGround = ground?.mill.ground_z_m_ngf ?? 12

  return (
    <div ref={viewRef} className="rounded-card border border-line bg-surface p-3 shadow-card sm:p-4">
      <div ref={widthRef} className="min-w-0 space-y-4">
        <ReplayScrubber
          presetId={presetId}
          onPreset={(id) => {
            if (id === 'now') setNow(new Date())
            setPresetId(id === 'custom' && !customAt ? 'now' : id)
          }}
          customAt={customAt}
          onCustomDate={(d) => {
            setCustomAt(d)
            setPresetId('custom')
          }}
          times={times}
          index={index}
          onIndex={(i) => {
            setPlaying(false)
            setIndex(i)
          }}
          playing={playing}
          onTogglePlay={() => {
            if (!playing && index >= times.length - 1) setIndex(0)
            setPlaying(!playing)
          }}
          reducedMotion={reducedMotion}
          frame={frame}
          isLive={isLive}
          story={story}
          onOpenDashboard={onOpenDashboardAt}
        />

        {ground && frame ? (
          <>
            <LidoireProfile profile={ground} zc={frame.zc} reach={frame.reach} floorZ={floorZ} width={width} />
            <LidoireProfileReadout profile={ground} zc={frame.zc} reach={frame.reach} floorZ={floorZ} />
          </>
        ) : groundError ? (
          <p role="alert" className="flex items-start gap-2 rounded-lg border border-line-2 px-3 py-2 text-[14px] text-ink-2">
            <AlertTriangle className="mt-0.5 h-4 w-4 flex-none" aria-hidden="true" />
            Profil en long indisponible ({groundError}). Les séries restent lisibles ci-dessous.
          </p>
        ) : (
          <div className="h-[300px] w-full animate-pulse rounded-lg bg-surface-2" aria-hidden="true" />
        )}

        {ground && (
          <div className="flex flex-wrap items-end gap-x-3 gap-y-1">
            <label className="flex min-w-0 max-w-[460px] flex-1 flex-col gap-1 text-[13.5px] text-ink-2">
              <span>
                Tester une altitude de plancher du moulin (m NGF, hypothèse : inconnue aujourd’hui){' '}
                <output className="num text-[15px] text-ink">{floorZ != null ? `${formatNumber(floorZ, 2)} m` : ''}</output>
              </span>
              <input
                type="range"
                min={Math.floor(millGround - 1)}
                max={Math.ceil(millGround + 2)}
                step={0.05}
                value={floorZ ?? millGround}
                onChange={(e) => setFloorZ(Number(e.target.value))}
                className="h-11 w-full accent-[var(--lv-red)]"
              />
            </label>
            {floorZ != null && (
              <button
                type="button"
                onClick={() => setFloorZ(null)}
                className="min-h-[44px] rounded-lg border border-line-2 px-3 text-[13.5px] text-ink-2 hover:bg-surface-2 sm:min-h-[34px]"
              >
                Effacer
              </button>
            )}
          </div>
        )}

        {data ? (
          <SyncStrips
            series={series}
            times={times}
            index={index}
            onScrub={(i) => {
              setPlaying(false)
              setIndex(i)
            }}
            width={width}
            zeros={{ pessac: params.upstreamZero, libourne: params.downstreamZero }}
            zc={zcSeries}
            markerAt={marker}
            markerLabel="inondation labellisée"
          />
        ) : timelineError ? (
          <p role="alert" className="flex items-start gap-2 rounded-lg border border-line-2 px-3 py-2 text-[14px] text-ink-2">
            <AlertTriangle className="mt-0.5 h-4 w-4 flex-none" aria-hidden="true" />
            Séries indisponibles : {timelineError}
          </p>
        ) : (
          <div className="h-[320px] w-full animate-pulse rounded-lg bg-surface-2" aria-hidden="true" />
        )}
        {loaded?.key === windowKey && loaded.pending.length > 0 && (
          <p className="text-[13px] text-ink-3">Rattrapage des données en cours ({loaded.pending.join(', ')}) : réessayer dans une minute.</p>
        )}

        <p className="font-mono text-[11.5px] text-ink-3">
          Profil : Sandre BD TOPAGE, IGN RGE ALTI (Licence Ouverte). Z_c = {formatNumber(1 - params.alpha, 1)} × Pessac +{' '}
          {formatNumber(params.alpha, 1)} × Libourne en m NGF, max sur {formatNumber(params.envelopeHours, 1)} h.
          {frame && (frame.source === 'api' ? ' Valeur du serveur à cette heure.' : ' Estimation locale des séries horaires.')}
        </p>
      </div>
    </div>
  )
}
