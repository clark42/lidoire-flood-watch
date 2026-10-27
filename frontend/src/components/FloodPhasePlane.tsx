import type { ReactNode } from 'react'
import type { EpisodeOutcome } from '../types'
import { formatNumber, formatQuantity } from '../utils/format'
import {
  EYRAUD_Q2_M3S,
  OUTCOME_LABELS,
  PESSAC_YELLOW_M,
  PHASE_LABEL_Q,
  PHASE_Q_MIN,
  PHASE_Q_TICKS,
  phaseLayout,
  phaseReading,
  trailPath,
  type EpisodePoint,
  type TodayPoint,
  type TrailPoint,
} from '../utils/phasePlane'
import { formatParis } from '../utils/time'

export interface PhaseTrail {
  id: string
  label: string
  /** CSS colour token */
  color: string
  points: TrailPoint[]
  /** Draw the trail progressively (off under reduced motion) */
  animate?: boolean
}

export interface FloodPhasePlaneProps {
  episodes: readonly EpisodePoint[]
  today: TodayPoint | null
  trails: readonly PhaseTrail[]
  width: number
}

/** Shape of an outcome (never colour alone): disc, dotted ring, cross, "?" ring. */
export function OutcomeMark({ outcome, cx, cy, size = 1 }: { outcome: EpisodeOutcome; cx: number; cy: number; size?: number }) {
  const r = 6 * size
  if (outcome === 'flooded') {
    return <circle cx={cx} cy={cy} r={r + 1} fill="var(--silt)" stroke="var(--surface)" strokeWidth="2" />
  }
  if (outcome === 'near_miss') {
    return (
      <g>
        <circle cx={cx} cy={cy} r={r} fill="var(--surface)" stroke="var(--silt)" strokeWidth="2" />
        <circle cx={cx} cy={cy} r={2 * size} fill="var(--silt)" />
      </g>
    )
  }
  if (outcome === 'false_positive') {
    const d = 4.5 * size
    return (
      <path
        d={`M${cx - d},${cy - d}L${cx + d},${cy + d}M${cx + d},${cy - d}L${cx - d},${cy + d}`}
        stroke="var(--ink-2)"
        strokeWidth="2"
        strokeLinecap="round"
      />
    )
  }
  return (
    <g>
      <circle cx={cx} cy={cy} r={r} fill="var(--surface)" stroke="var(--ink-3)" strokeWidth="1.5" strokeDasharray="2 2" />
      <text x={cx} y={cy + 3.3} textAnchor="middle" className="fill-ink-2 text-[9px] font-bold">
        ?
      </text>
    </g>
  )
}

function shortDate(iso: string, narrow: boolean): string {
  return formatParis(iso, narrow ? 'MM/yyyy' : 'd MMM yyyy')
}

/**
 * Flood plane: Eyraud discharge (runoff of a neighbouring catchment, log
 * scale) against the Pessac level (backwater and regional wetness), with
 * the labelled episodes, today's point and daily trails.
 */
export default function FloodPhasePlane({ episodes, today, trails, width }: FloodPhasePlaneProps) {
  const plotted = episodes.filter((e) => e.q != null && e.pessac != null)
  const maxPessac = Math.max(
    8,
    ...plotted.map((e) => e.pessac!),
    ...trails.flatMap((t) => t.points.map((p) => p.pessac)),
    today?.pessac ?? 0,
  )
  const L = phaseLayout(width, maxPessac)
  const { x, y, margin, height, narrow } = L
  const left = margin.left
  const right = width - margin.right
  const top = margin.top
  const bottom = height - margin.bottom
  const reading = phaseReading(episodes)
  const yTicks: number[] = []
  for (let v = 0; v <= y.domain()[1]; v += 2) yTicks.push(v)
  const qTicks = PHASE_Q_TICKS.filter((v) => v <= x.domain()[1])
  const labelX = x(PHASE_LABEL_Q)

  // Flood labels in a de-collided column (13 px apart)
  const labelled = plotted
    .filter((e) => e.outcome === 'flooded')
    .map((e) => ({ e, cx: x(e.q!), cy: y(e.pessac!) }))
    .sort((a, b) => a.cy - b.cy)
  let lastY = -Infinity
  const labels = labelled.map((o) => {
    const ly = Math.max(o.cy, lastY + 13)
    lastY = ly
    return { ...o, ly }
  })
  const minFloodQ = reading.minFloodQ?.q ?? null
  const todayX = today?.q != null ? x(Math.max(today.q, PHASE_Q_MIN)) : null
  const todayY = today?.pessac != null ? y(today.pessac) : null

  const ariaLabel =
    `Plan des crues : débit de l’Eyraud (échelle log) contre niveau de Pessac. ${plotted.length} épisodes labellisés placés` +
    (today?.pessac != null && today.q != null
      ? ` ; aujourd’hui Pessac ${formatNumber(today.pessac, 2)} m, Eyraud ${formatQuantity(today.q)} m³/s.`
      : '.')

  return (
    <svg width={width} height={height} role="img" aria-label={ariaLabel} className="block max-w-full select-none">
      {/* Zone of the labelled runoff floods: right of the weakest one */}
      {minFloodQ != null && (
        <g data-flood-zone={minFloodQ}>
          <rect x={x(minFloodQ)} y={top} width={Math.max(0, right - x(minFloodQ))} height={bottom - top} fill="var(--silt-soft)" opacity="0.55" />
          <text x={right - 4} y={top + 12} textAnchor="end" className="fill-ink-2 text-[10.5px] font-semibold">
            {narrow ? 'crues' : 'débit des crues labellisées'}
          </text>
        </g>
      )}

      {/* Grid */}
      {qTicks.map((v) => (
        <g key={`q${v}`}>
          <line x1={x(v)} x2={x(v)} y1={top} y2={bottom} stroke="var(--line)" />
          <text x={x(v)} y={bottom + 14} textAnchor="middle" className="fill-ink-3 font-mono text-[10.5px]">
            {formatNumber(v, v < 1 ? 1 : 0)}
          </text>
        </g>
      ))}
      {yTicks.map((v) => (
        <g key={`p${v}`}>
          <line x1={left} x2={right} y1={y(v)} y2={y(v)} stroke="var(--line)" />
          <text x={left - 5} y={y(v) + 3.5} textAnchor="end" className="fill-ink-3 font-mono text-[10.5px]">
            {v}
          </text>
        </g>
      ))}
      <text x={(left + right) / 2} y={height - 8} textAnchor="middle" className="fill-ink-2 text-[11.5px]">
        Eyraud, débit max (m³/s, échelle log) →
      </text>
      <text
        x={12}
        y={(top + bottom) / 2}
        textAnchor="middle"
        transform={`rotate(-90 12 ${(top + bottom) / 2})`}
        className="fill-ink-2 text-[11.5px]"
      >
        Pessac, niveau max (m) →
      </text>

      {/* Reference lines */}
      <line x1={left} x2={right} y1={y(PESSAC_YELLOW_M)} y2={y(PESSAC_YELLOW_M)} stroke="var(--lv-yellow)" strokeWidth="1.4" />
      <text x={left + 4} y={y(PESSAC_YELLOW_M) - 4} className="fill-ink-3 font-mono text-[10px]">
        {narrow ? '3,0 m' : '3,0 m : seuil jaune de Pessac'}
      </text>
      <line x1={x(EYRAUD_Q2_M3S)} x2={x(EYRAUD_Q2_M3S)} y1={top} y2={bottom} stroke="var(--ink-3)" strokeDasharray="3 3" />
      <text x={x(EYRAUD_Q2_M3S) - 4} y={bottom - 6} textAnchor="end" className="fill-ink-3 font-mono text-[10px]">
        Q2 ≈ {formatNumber(EYRAUD_Q2_M3S, 1)}
      </text>

      {/* Daily trails */}
      {trails.map((tr) => {
        if (tr.points.length === 0) return null
        const d = trailPath(tr.points, L)
        const peak = tr.points.reduce((a, b) => (b.pessac > a.pessac ? b : a))
        return (
          <g key={tr.id} data-trail={tr.id}>
            <path
              d={d}
              fill="none"
              stroke={tr.color}
              strokeWidth="1.6"
              strokeLinejoin="round"
              opacity="0.9"
              pathLength={tr.animate ? 1 : undefined}
              className={tr.animate ? 'phase-trail-draw' : undefined}
            />
            {tr.points.map((p) => (
              <circle key={p.day} cx={x(Math.max(p.q, PHASE_Q_MIN))} cy={y(p.pessac)} r="2.2" fill={tr.color}>
                <title>{`${formatParis(new Date(p.t), 'dd/MM/yyyy')} : Pessac ${formatNumber(p.pessac, 2)} m, Eyraud ${formatQuantity(p.q)} m³/s (max journaliers)`}</title>
              </circle>
            ))}
            {tr.id !== 'now' && (
              <text
                x={x(Math.max(peak.q, PHASE_Q_MIN)) - 6}
                y={y(peak.pessac) - 8}
                textAnchor="end"
                className="text-[10.5px] font-bold"
                style={{ fill: tr.color }}
              >
                {tr.label}
              </text>
            )}
          </g>
        )
      })}

      {/* Labelled episodes */}
      {plotted.map((e) => {
        const cx = x(e.q!)
        const cy = y(e.pessac!)
        return (
          <g key={e.key} data-episode={e.key} data-outcome={e.outcome} data-q-basis={e.qBasis}>
            <title>
              {`${formatParis(e.peak, 'dd/MM/yyyy HH:mm')} · ${OUTCOME_LABELS[e.outcome]}` +
                (e.waterHeightCm != null ? ` · ${formatNumber(e.waterHeightCm, 0)} cm dans le moulin` : '') +
                ` · Pessac ${formatNumber(e.pessac, 2)} m · Eyraud ${formatQuantity(e.q)} m³/s` +
                (e.qBasis === 'daily' ? ' (≈ max journalier, pas de point horaire)' : ' (max 12,5 h)') +
                (e.infiltration ? ' · infiltration, colmatée depuis' : '')}
            </title>
            {e.qBasis === 'daily' && <circle cx={cx} cy={cy} r="10" fill="none" stroke="var(--ink-3)" strokeDasharray="2 2" />}
            <OutcomeMark outcome={e.outcome} cx={cx} cy={cy} />
          </g>
        )
      })}
      {/* Flood labels: one column right of the points, with leader lines */}
      {labels.map(({ e, cx, cy, ly }) => (
        <g key={`l${e.key}`}>
          <path d={`M${cx + 8},${cy}L${labelX - 3},${ly}`} stroke="var(--line-2)" fill="none" />
          <text x={labelX} y={ly + 3.5} className="fill-ink text-[10.5px] font-semibold">
            {shortDate(e.peak, narrow)}
            {e.infiltration ? (narrow ? ' (infiltr.)' : ' (infiltration)') : ''}
          </text>
        </g>
      ))}

      {/* Today */}
      {todayX != null && todayY != null && (
        <g data-today="">
          <circle cx={todayX} cy={todayY} r="6" fill="none" stroke="var(--ink)" strokeWidth="1.5" className="phase-pulse" />
          <circle cx={todayX} cy={todayY} r="6.5" fill="var(--ink)" stroke="var(--surface)" strokeWidth="2" />
          <text x={todayX + 10} y={todayY - 10} className="fill-ink text-[12px] font-semibold">
            aujourd’hui
          </text>
        </g>
      )}
    </svg>
  )
}

/** Legend of the shapes and trails (always present: several series). */
export function FloodPhaseLegend({ trails }: { trails: readonly Pick<PhaseTrail, 'id' | 'label' | 'color'>[] }) {
  const item = (svg: ReactNode, text: string, key: string) => (
    <li key={key} className="flex items-center gap-1.5">
      {svg}
      {text}
    </li>
  )
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1.5 text-[12.5px] text-ink-2" aria-label="Légende du plan des crues">
      {(['flooded', 'near_miss', 'false_positive', 'unknown'] as EpisodeOutcome[]).map((o) =>
        item(
          <svg width="16" height="16" aria-hidden="true" className="flex-none">
            <OutcomeMark outcome={o} cx={8} cy={8} size={0.85} />
          </svg>,
          OUTCOME_LABELS[o],
          o,
        ),
      )}
      {item(
        <svg width="22" height="22" aria-hidden="true" className="flex-none">
          <circle cx="11" cy="11" r="9" fill="none" stroke="var(--ink-3)" strokeDasharray="2 2" />
          <circle cx="11" cy="11" r="4" fill="var(--ink-3)" />
        </svg>,
        '≈ débit journalier (point horaire absent)',
        'daily',
      )}
      {trails.map((t) =>
        item(
          <svg width="18" height="8" aria-hidden="true" className="flex-none">
            <line x1="1" x2="17" y1="4" y2="4" stroke={t.color} strokeWidth="2" />
          </svg>,
          `trace ${t.label} (max journaliers)`,
          t.id,
        ),
      )}
      {item(
        <svg width="16" height="16" aria-hidden="true" className="flex-none">
          <circle cx="8" cy="8" r="5.5" fill="var(--ink)" />
        </svg>,
        'aujourd’hui (max 12,5 h)',
        'today',
      )}
    </ul>
  )
}

/** Values of the plane as a table (tooltips doubled in writing). */
export function FloodPhaseTable({ episodes }: { episodes: readonly EpisodePoint[] }) {
  const rows = [...episodes].sort((a, b) => b.peak.localeCompare(a.peak))
  return (
    <details className="text-[13px]">
      <summary className="min-h-[44px] cursor-pointer py-2 text-ink-2 sm:min-h-0">Valeurs des {rows.length} épisodes (tableau)</summary>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[420px] border-collapse text-left">
          <thead className="text-ink-3">
            <tr>
              <th className="py-1 pr-3 font-semibold">Pic labellisé</th>
              <th className="py-1 pr-3 font-semibold">Issue</th>
              <th className="py-1 pr-3 text-right font-semibold">Pessac (m)</th>
              <th className="py-1 pr-3 text-right font-semibold">Eyraud (m³/s)</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((e) => (
              <tr key={e.key} className="border-t border-line">
                <td className="py-1 pr-3 font-mono text-[12px]">{formatParis(e.peak, 'dd/MM/yyyy HH:mm')}</td>
                <td className="py-1 pr-3">
                  {OUTCOME_LABELS[e.outcome]}
                  {e.waterHeightCm != null ? `, ${formatNumber(e.waterHeightCm, 0)} cm` : ''}
                  {e.infiltration ? ', infiltration' : ''}
                </td>
                <td className="num py-1 pr-3 text-right">{formatNumber(e.pessac, 2)}</td>
                <td className="num py-1 pr-3 text-right">
                  {e.qBasis === 'daily' ? '≈ ' : ''}
                  {formatQuantity(e.q)}
                  {e.qBasis === 'daily' ? ' (jour)' : ''}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  )
}
