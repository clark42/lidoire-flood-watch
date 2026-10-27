import { useEffect, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import {
  getApiKey,
  getEpisodeDrivers,
  getObservationPhoto,
  getObservations,
  getRiskAnalogs,
  isAuthError,
} from '../api'
import type { EpisodeDriversData, EpisodeOutcome, RiskAnalog, RiskAnalogsData } from '../types'
import { useApiResource } from '../hooks/useApiResource'
import {
  analogsIntro,
  axisBars,
  floodRows,
  observationWindow,
  OUTCOME_LABELS,
} from '../utils/analogs'
import { formatNumber, formatQuantity, MISSING_VALUE } from '../utils/format'
import { formatParis } from '../utils/time'
import ValueTag from './ui/ValueTag'
import '../styles/field.css'

/** Outcome shape (never colour alone): disc, dotted ring, cross, dashed "?" */
export function OutcomeMark({ outcome }: { outcome: EpisodeOutcome }) {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true" className="flex-none">
      {outcome === 'flooded' && <circle cx="7" cy="7" r="5.5" style={{ fill: 'var(--silt)' }} />}
      {outcome === 'near_miss' && (
        <>
          <circle cx="7" cy="7" r="5" fill="none" strokeWidth="2" style={{ stroke: 'var(--silt)' }} />
          <circle cx="7" cy="7" r="1.8" style={{ fill: 'var(--silt)' }} />
        </>
      )}
      {outcome === 'false_positive' && <path d="M3,3L11,11M11,3L3,11" strokeWidth="2" style={{ stroke: 'var(--ink-2)' }} />}
      {outcome === 'unknown' && (
        <circle cx="7" cy="7" r="5" fill="none" strokeDasharray="2 2" style={{ stroke: 'var(--ink-3)' }} />
      )}
    </svg>
  )
}

function OutcomeChip({ outcome, heightCm }: { outcome: EpisodeOutcome; heightCm: number | null }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded px-1.5 py-px text-[12px] font-bold ${
        outcome === 'flooded' ? 'bg-silt-soft text-silt' : 'bg-surface-2 text-ink-2'
      }`}
    >
      <OutcomeMark outcome={outcome} />
      {OUTCOME_LABELS[outcome] ?? outcome}
      {heightCm != null && ` · ${formatNumber(heightCm, 0)} cm`}
    </span>
  )
}

type PhotoState =
  | { kind: 'none'; text: string }
  | { kind: 'loading' }
  | { kind: 'photo'; url: string; alt: string }
  | { kind: 'private' }
  | { kind: 'error'; text: string }

/**
 * Photo slot of an analog: the first observation photo within the analog
 * window, fetched with X-API-Key (photos are private, never a plain src).
 */
export function AnalogPhoto({ analog, windowHours }: { analog: RiskAnalog; windowHours: number }) {
  const withPhoto = analog.observations?.with_photo ?? 0
  const [state, setState] = useState<PhotoState>(() =>
    analog.observations == null
      ? { kind: 'none', text: 'Relevés indisponibles' }
      : withPhoto === 0
        ? { kind: 'none', text: 'Pas encore de photo de cet épisode' }
        : getApiKey()
          ? { kind: 'loading' }
          : { kind: 'private' },
  )

  useEffect(() => {
    if (withPhoto === 0 || !getApiKey()) return
    const win = observationWindow(analog.peak_time, windowHours)
    if (!win) return
    let alive = true
    let objectUrl: string | null = null
    ;(async () => {
      try {
        const obs = await getObservations(win.start, win.end)
        const first = obs.find((o) => o.has_photo && o.photo_url)
        if (!first?.photo_url) {
          if (alive) setState({ kind: 'none', text: 'Photo introuvable' })
          return
        }
        const blob = await getObservationPhoto(first.photo_url)
        if (!alive) return
        objectUrl = URL.createObjectURL(blob)
        setState({
          kind: 'photo',
          url: objectUrl,
          alt: `Relevé du ${formatParis(first.observed_at, 'd MMMM yyyy HH:mm')}${first.note ? ` : ${first.note}` : ''}`,
        })
      } catch (e) {
        if (!alive) return
        setState(isAuthError(e) ? { kind: 'private' } : { kind: 'error', text: 'Photo indisponible' })
      }
    })()
    return () => {
      alive = false
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [analog.peak_time, withPhoto, windowHours])

  if (state.kind === 'photo') {
    return <img src={state.url} alt={state.alt} className="analog-photo w-full object-cover" />
  }
  return (
    <div className="analog-photo grid place-items-center p-2 text-center text-[12.5px] text-ink-3">
      {state.kind === 'loading' && <span>Chargement de la photo…</span>}
      {state.kind === 'private' && (
        <span>
          {withPhoto} photo{withPhoto > 1 ? 's' : ''} privée{withPhoto > 1 ? 's' : ''} :{' '}
          <Link to="/config#api-key" className="underline">
            clé API requise
          </Link>
        </span>
      )}
      {(state.kind === 'none' || state.kind === 'error') && (
        <span>
          {state.text}
          <br />
          <span className="font-mono text-[11px]">à joindre depuis un relevé terrain</span>
        </span>
      )}
    </div>
  )
}

function PercentileBar({ value, color, label }: { value: number | null; color: string; label: string }) {
  return (
    <div className="relative h-1.5 overflow-hidden rounded-full bg-surface-3" title={label}>
      {value != null && (
        <span className="absolute inset-y-0 left-0 rounded-full" style={{ width: `${value}%`, background: color }} />
      )}
    </div>
  )
}

const pct = (v: number | null) => (v == null ? MISSING_VALUE : `p${formatNumber(v, 0)}`)

export function AnalogCard({
  analog,
  data,
  photo,
}: {
  analog: RiskAnalog
  data: RiskAnalogsData
  photo?: ReactNode
}) {
  const bars = axisBars(analog, data)
  const episodeColor = analog.outcome === 'flooded' ? 'var(--silt)' : 'var(--ink-3)'
  const note = analog.notes?.split('.')[0]
  return (
    <article className="min-w-0 rounded-card border border-line bg-surface p-4">
      {photo}
      <div className="mt-2.5 flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="font-display text-[19px] font-semibold tracking-[0.01em]">
          {formatParis(analog.peak_time, 'd MMMM yyyy')}
        </h3>
        <OutcomeChip outcome={analog.outcome} heightCm={analog.water_height_cm} />
      </div>
      <p className="text-[13px] text-ink-3">
        distance {formatNumber(analog.distance, 0)}
        {analog.close ? ' · proche' : ''}
        {note ? ` · ${note}` : ''}
      </p>
      <div className="mt-2.5 grid grid-cols-[72px_minmax(0,1fr)_38px] items-center gap-x-2 gap-y-1 text-[12px]">
        {bars.map((b) => (
          <div key={b.axis} className="contents">
            <span className="text-ink-3">{b.label}</span>
            <div className="grid gap-0.5">
              <PercentileBar value={b.today} color="var(--ink)" label={`aujourd’hui ${pct(b.today)}`} />
              <PercentileBar value={b.episode} color={episodeColor} label={`épisode ${pct(b.episode)}`} />
            </div>
            <span className="text-right tabular-nums text-ink-3">
              {pct(b.episode)}
              <span className="sr-only">{` à l’épisode, ${pct(b.today)} aujourd’hui`}</span>
            </span>
          </div>
        ))}
      </div>
      <div className="mt-1.5 flex flex-wrap gap-x-3.5 text-[12px] text-ink-2" aria-hidden="true">
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block h-1.5 w-3.5 rounded-full bg-ink" />
          aujourd’hui
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block h-1.5 w-3.5 rounded-full" style={{ background: episodeColor }} />
          épisode
        </span>
      </div>
    </article>
  )
}

const cell = 'px-2 py-1.5 text-right tabular-nums'

/** Labelled floods next to today, so that the cards are read in context. */
export function FloodTable({ drivers, data }: { drivers: EpisodeDriversData; data: RiskAnalogsData | null }) {
  const rows = floodRows(drivers, data?.situation ?? null, data?.axes ?? ['pessac', 'eyraud_q', 'rain_72h', 'piezo'])
  const today = data?.situation?.values
  return (
    <article className="min-w-0 rounded-card border border-line bg-surface p-4">
      <h3 className="font-display text-[19px] font-semibold tracking-[0.01em]">Pour comparaison : les crues labellisées</h3>
      <p className="text-[13px] text-ink-3">
        Valeurs au pic labellisé (daté au jour près), fenêtres causales. Eyraud{' '}
        <ValueTag kind="analog" /> : bassin voisin mesuré à La Force.
      </p>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full min-w-[560px] border-collapse text-[13px]">
          <caption className="sr-only">Crues labellisées et situation du jour</caption>
          <thead>
            <tr className="border-b border-line text-ink-2">
              <th scope="col" className="px-2 py-1.5 text-left font-bold">Date</th>
              <th scope="col" className={`${cell} font-bold`}>Pessac</th>
              <th scope="col" className={`${cell} font-bold`}>Eyraud</th>
              <th scope="col" className={`${cell} font-bold`}>Pluie 72 h</th>
              <th scope="col" className={`${cell} font-bold`}>Nappe</th>
              <th scope="col" className={`${cell} font-bold`}>Hauteur moulin</th>
              <th scope="col" className={`${cell} font-bold`}>Distance</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key} className="border-b border-line">
                <th scope="row" className="px-2 py-1.5 text-left font-normal">
                  {formatParis(r.peakTime, 'd MMM yyyy')}
                  {r.notes.length > 0 && <span className="block text-[11px] text-ink-3">{r.notes.join(' ; ')}</span>}
                </th>
                <td className={cell}>{formatNumber(r.values.pessac_m, 2)} m</td>
                <td className={cell}>{formatQuantity(r.values.eyraud_q_m3s)} m³/s</td>
                <td className={cell}>{formatNumber(r.values.rain_72h_mm, 0)} mm</td>
                <td className={cell}>{formatNumber(r.values.piezo_m_ngf, 2)}</td>
                <td className={cell}>{r.waterHeightCm != null ? `${formatNumber(r.waterHeightCm, 0)} cm` : MISSING_VALUE}</td>
                <td className={cell}>{formatNumber(r.distance, 0)}</td>
              </tr>
            ))}
          </tbody>
          {today && (
            <tfoot>
              <tr className="font-bold">
                <th scope="row" className="px-2 py-1.5 text-left">Aujourd’hui</th>
                <td className={cell}>{formatNumber(today.pessac_m, 2)} m</td>
                <td className={cell}>{formatQuantity(today.eyraud_q_m3s)} m³/s</td>
                <td className={cell}>{formatNumber(today.rain_72h_mm, 0)} mm</td>
                <td className={cell}>{formatNumber(today.piezo_m_ngf, 2)}</td>
                <td className={cell}>{MISSING_VALUE}</td>
                <td className={cell}>0</td>
              </tr>
            </tfoot>
          )}
        </table>
      </div>
      <p className="mt-2 text-[12px] text-ink-3">
        Nappe en m NGF. {rows.length} crues seulement, en échantillon : une lecture, pas une loi.
      </p>
    </article>
  )
}

export interface AnalogCardsViewProps {
  analogs: RiskAnalogsData | null
  analogsError?: string | null
  drivers: EpisodeDriversData | null
  driversError?: string | null
  /** Photo slot of a card (the container fetches the private photos) */
  renderPhoto?: (analog: RiskAnalog, data: RiskAnalogsData) => ReactNode
}

export function AnalogCardsView({ analogs, analogsError, drivers, driversError, renderPhoto }: AnalogCardsViewProps) {
  if (!analogs) {
    return (
      <p className="text-ink-2">{analogsError ? `Analogues indisponibles : ${analogsError}` : 'Recherche des analogues…'}</p>
    )
  }
  const intro = analogsIntro(analogs)
  return (
    <div className="space-y-3">
      <p className="max-w-[70ch] text-ink-2" role="status">
        <b className="text-ink">{intro.headline}</b> {intro.detail}
      </p>
      {analogs.analogs.length === 0 ? (
        <p className="text-ink-2">Aucun épisode labellisé comparable (labels absents).</p>
      ) : (
        <div className="grid grid-cols-1 gap-3 min-[700px]:grid-cols-3">
          {analogs.analogs.map((a) => (
            <AnalogCard key={a.key} analog={a} data={analogs} photo={renderPhoto?.(a, analogs)} />
          ))}
        </div>
      )}
      {drivers ? (
        <FloodTable drivers={drivers} data={analogs} />
      ) : (
        <p className="text-[13px] text-ink-3">
          {driversError ? `Tableau des crues indisponible : ${driversError}` : 'Chargement des crues labellisées…'}
        </p>
      )}
      <p className="text-[12px] text-ink-3">
        Percentiles : {analogs.percentile_basis?.pessac ?? 'Pessac'} ; {analogs.percentile_basis?.eyraud_q ?? 'Eyraud'}.
        {analogs.labels_partial && ' Valeurs partielles pour certains épisodes.'}
      </p>
    </div>
  )
}

const TEN_MIN = 10 * 60_000

/**
 * "Ça ressemble à…": the nearest labelled episodes, honest when nothing is
 * close. `at` (TimeMachine, ISO): the situation at that date.
 */
export default function AnalogCards({ at }: { at?: string }) {
  const analogs = useApiResource<RiskAnalogsData>(`risk/analogs@${at ?? 'now'}`, () => getRiskAnalogs(at), TEN_MIN)
  const drivers = useApiResource<EpisodeDriversData>('episodes/drivers', getEpisodeDrivers)
  return (
    <AnalogCardsView
      analogs={analogs.data}
      analogsError={analogs.error}
      drivers={drivers.data}
      driversError={drivers.error}
      renderPhoto={(a, data) => <AnalogPhoto analog={a} windowHours={data.observation_window_hours} />}
    />
  )
}
