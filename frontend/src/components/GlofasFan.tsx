import { getEnsembleForecast, getEpisodeDrivers } from '../api'
import type { EnsembleForecast, EpisodeDriversData, GlofasForecast } from '../types'
import { useApiResource } from '../hooks/useApiResource'
import { useElementWidth } from '../hooks/useElementWidth'
import {
  bandPath,
  dayTime,
  decadeTicks,
  fanDays,
  fanDomain,
  fanSummary,
  floodBand,
  hasQuartiles,
  linePath,
} from '../utils/glofasFan'
import { linearScale, logScale } from '../utils/scale'
import { formatNumber, formatQuantity } from '../utils/format'
import { formatParis } from '../utils/time'
import ValueTag from './ui/ValueTag'
import '../styles/field.css'

const dayLabel = (date: string) => formatParis(new Date(dayTime(date)), 'd MMM')
const pctText = (p: number | null | undefined) => (p == null ? '–' : `${formatNumber(p * 100, 0)} %`)

function LegendLine({ className, children }: { className: string; children: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <svg width="20" height="8" aria-hidden="true">
        <line x1="1" x2="19" y1="4" y2="4" className={className} />
      </svg>
      {children}
    </span>
  )
}

export interface GlofasFanViewProps {
  glofas: GlofasForecast | null
  drivers?: EpisodeDriversData | null
  width: number
  now: Date
  error?: string | null
}

/** 30-day GloFAS fan on a log scale (pure: data in, SVG out). */
export function GlofasFanView({ glofas, drivers, width, now, error }: GlofasFanViewProps) {
  if (!glofas) {
    return <p className="text-ink-2">{error ? `GloFAS indisponible : ${error}` : 'Chargement de l’éventail GloFAS…'}</p>
  }
  const days = fanDays(glofas)
  if (!glofas.available || days.length < 2) {
    return <p className="text-ink-2">GloFAS indisponible{glofas.note ? ` : ${glofas.note}` : ''}.</p>
  }
  const clim = glofas.climatology ?? null
  const floods = floodBand(drivers)
  const quartiles = hasQuartiles(days)
  const summary = fanSummary(days, clim)

  const narrow = width < 560
  const m = { l: narrow ? 40 : 52, r: narrow ? 10 : 16, t: 24, b: 30 }
  const H = narrow ? 250 : 290
  const domain = fanDomain(days, [clim?.p90, clim?.p99, floods?.min, floods?.max])
  const x = linearScale([dayTime(days[0].date), dayTime(days[days.length - 1].date)], [m.l, width - m.r])
  const y = logScale(domain, [H - m.b, m.t])
  const xNow = now.getTime() >= x.domain[0] && now.getTime() <= x.domain[1] ? x(now.getTime()) : null
  const step = narrow ? 7 : 5
  // Edge labels stay inside the SVG (overflow is clipped)
  const tickAnchor = (px: number) => (px < m.l + 24 ? 'start' : px > width - m.r - 24 ? 'end' : 'middle')

  return (
    <div className="fchart">
      <svg
        width={width}
        height={H}
        viewBox={`0 0 ${width} ${H}`}
        role="img"
        aria-label={`Éventail GloFAS du débit de la Lidoire sur ${days.length} jours, échelle logarithmique. Membre le plus humide : ${formatQuantity(summary.wettest?.value)} m³/s.`}
      >
        {decadeTicks(domain).map((v) => (
          <g key={v}>
            <line x1={m.l} x2={width - m.r} y1={y(v)} y2={y(v)} className="fchart-grid" />
            <text x={m.l - 5} y={y(v) + 3} textAnchor="end" className="fchart-ax">
              {formatNumber(v, v < 0.1 ? 2 : v < 1 ? 1 : 0)}
            </text>
          </g>
        ))}
        <text x={m.l - 5} y={11} textAnchor="end" className="fchart-ax">
          m³/s
        </text>

        {floods && (
          <g>
            <rect
              x={m.l}
              y={y(floods.max)}
              width={width - m.l - m.r}
              height={Math.max(2, y(floods.min) - y(floods.max))}
              className="gfan-floods"
            />
            <text x={width - m.r - 4} y={y(floods.max) - 4} textAnchor="end" className="fchart-ax gfan-floods-label">
              {narrow ? 'crues simulées' : `valeurs simulées aux crues (${floods.years.join(', ')})`}
            </text>
          </g>
        )}

        {clim &&
          (
            [
              ['p90', clim.p90],
              ['p99', clim.p99],
            ] as const
          ).map(([label, v]) => (
            <g key={label}>
              <line x1={m.l} x2={width - m.r} y1={y(v)} y2={y(v)} className="gfan-clim" />
              <text x={width - m.r - 4} y={y(v) - 3} textAnchor="end" className="fchart-ax">
                {`${label} climatologie (${formatQuantity(v)})`}
              </text>
            </g>
          ))}

        <path d={bandPath(days, x, y, 'p10', 'p90')} className="gfan-outer" />
        {quartiles && <path d={bandPath(days, x, y, 'p25', 'p75')} className="gfan-inner" />}
        <path d={linePath(days, x, y, 'max')} className="gfan-max" />
        <path d={linePath(days, x, y, 'p50')} className="gfan-median" />

        {days.map((d, i) =>
          i % step === 0 ? (
            <text key={d.date} x={x(dayTime(d.date))} y={H - m.b + 14} textAnchor={tickAnchor(x(dayTime(d.date)))} className="fchart-ax">
              {dayLabel(d.date)}
            </text>
          ) : null,
        )}
        <line x1={m.l} x2={width - m.r} y1={H - m.b} y2={H - m.b} className="fchart-base" />
        {xNow != null && (
          <g>
            <line x1={xNow} x2={xNow} y1={m.t} y2={H - m.b} className="gfan-now" />
            <text x={xNow + 4} y={H - m.b - 6} className="fchart-ax">
              {narrow ? 'prévision' : `prévision ${glofas.members ?? ''} membres`}
            </text>
          </g>
        )}
      </svg>

      <div className="mt-2 flex flex-wrap gap-x-3.5 gap-y-1.5 text-[12px] text-ink-2" aria-label="Légende de l’éventail">
        <span className="inline-flex items-center gap-1.5">
          <span aria-hidden="true" className="inline-block h-2.5 w-3.5 rounded-sm bg-water-soft" />
          p10-p90
        </span>
        {quartiles ? (
          <span className="inline-flex items-center gap-1.5">
            <span aria-hidden="true" className="inline-block h-2.5 w-3.5 rounded-sm bg-water opacity-60" />
            p25-p75
          </span>
        ) : (
          <span>p25-p75 : non servi par l’API</span>
        )}
        <LegendLine className="gfan-median">médiane</LegendLine>
        <LegendLine className="gfan-max">membre le plus humide</LegendLine>
        <LegendLine className="gfan-clim">climatologie p90 / p99</LegendLine>
        {floods && (
          <span className="inline-flex items-center gap-1.5">
            <span aria-hidden="true" className="inline-block h-2.5 w-3.5 rounded-sm bg-silt-soft" />
            crues passées (simulé)
          </span>
        )}
      </div>

      <p className="mt-2 max-w-[75ch] text-[13px] text-ink-2">
        <ValueTag kind="model">modèle, tendance, pas un niveau</ValueTag>{' '}
        Run du {glofas.run ? formatParis(new Date(dayTime(glofas.run)), 'd MMM') : '?'}, {glofas.members ?? '?'} membres
        {glofas.stale ? ' (données anciennes)' : ''}. Sur {days.length} jours, le membre le plus humide atteint{' '}
        <b className="text-ink">{formatQuantity(summary.wettest?.value)} m³/s</b>
        {summary.wettest ? ` le ${dayLabel(summary.wettest.date)}` : ''}
        {clim ? ` (climatologie de la maille : p90 ${formatQuantity(clim.p90)}, p99 ${formatQuantity(clim.p99)})` : ''}.
        {' '}Probabilité sur 10 jours de dépasser p95 : {pctText(glofas.max_prob_gt_p95_10d?.value)}, p99 :{' '}
        {pctText(glofas.max_prob_gt_p99_10d?.value)}.{' '}
        {floods
          ? `Zone des crues : ${formatQuantity(floods.min)}-${formatQuantity(floods.max)} m³/s simulés sur ${floods.count} crues.`
          : 'Valeurs simulées aux crues passées : pas encore servies par l’API.'}{' '}
        GloFAS sous-estime les pics : il donne une tendance à J+3…J+10, jamais le niveau au moulin.
      </p>
      {glofas.attribution && <p className="mt-1 text-[12px] text-ink-3">{glofas.attribution}</p>}
    </div>
  )
}

const THIRTY_MIN = 30 * 60_000

/** "Le mois qui vient": GloFAS v4 fan of the Lidoire cell, informative only. */
export default function GlofasFan({ now }: { now: Date }) {
  const [ref, width] = useElementWidth<HTMLDivElement>()
  const ensemble = useApiResource<EnsembleForecast>('forecast/ensemble', getEnsembleForecast, THIRTY_MIN)
  const drivers = useApiResource<EpisodeDriversData>('episodes/drivers', getEpisodeDrivers)
  return (
    <div className="min-w-0 rounded-card border border-line bg-surface p-4">
      {/* Measured without the card padding (clientWidth includes it) */}
      <div ref={ref} className="min-w-0">
        <GlofasFanView glofas={ensemble.data?.glofas ?? null} drivers={drivers.data} width={width} now={now} error={ensemble.error} />
      </div>
    </div>
  )
}
