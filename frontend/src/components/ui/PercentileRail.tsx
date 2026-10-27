import { useId } from 'react'
import type { EpisodeOutcome } from '../../types'
import { useElementWidth } from '../../hooks/useElementWidth'
import { layoutRail, type RailSpec } from '../../utils/rail'

/** Shape of an episode outcome (never the colour alone, spec section 5). */
export function OutcomeGlyph({ outcome, x, y }: { outcome: EpisodeOutcome; x: number; y: number }) {
  switch (outcome) {
    case 'flooded':
      return <circle cx={x} cy={y} r={4.6} fill="var(--silt)" stroke="var(--surface)" strokeWidth={1.4} />
    case 'near_miss':
      return (
        <g>
          <circle cx={x} cy={y} r={4} fill="var(--surface)" stroke="var(--silt)" strokeWidth={1.5} />
          <circle cx={x} cy={y} r={1.4} fill="var(--silt)" />
        </g>
      )
    case 'false_positive':
      return (
        <path
          d={`M${x - 3.4},${y - 3.4}L${x + 3.4},${y + 3.4}M${x - 3.4},${y + 3.4}L${x + 3.4},${y - 3.4}`}
          stroke="var(--ink-3)"
          strokeWidth={1.7}
          strokeLinecap="round"
        />
      )
    default:
      return (
        <circle cx={x} cy={y} r={4} fill="var(--surface)" stroke="var(--ink-3)" strokeWidth={1.3} strokeDasharray="2 1.6" />
      )
  }
}

/** Legend of the episode shapes (shared by the mechanism cards). */
export function OutcomeLegend({ className = '' }: { className?: string }) {
  const items: [EpisodeOutcome, string][] = [
    ['flooded', 'inondé'],
    ['near_miss', 'presque'],
    ['false_positive', 'pas inondé'],
    ['unknown', 'à annoter'],
  ]
  return (
    <ul className={`flex flex-wrap items-center gap-x-3.5 gap-y-1 text-[12.5px] text-ink-2 ${className}`} aria-label="Issues des épisodes">
      {items.map(([o, label]) => (
        <li key={o} className="inline-flex items-center gap-1.5">
          <svg width="12" height="12" aria-hidden="true">
            <OutcomeGlyph outcome={o} x={6} y={6} />
          </svg>
          {label}
        </li>
      ))}
      <li className="inline-flex items-center gap-1.5">
        <svg width="12" height="14" aria-hidden="true">
          <line x1={6} x2={6} y1={0} y2={14} stroke="var(--ink)" strokeWidth={2} />
        </svg>
        maintenant
      </li>
    </ul>
  )
}

/**
 * The value now on a physical axis, among the values of the past labelled
 * episodes (shape = outcome), with thresholds (ticks; "*" = provisional,
 * in-sample) and an unknown threshold drawn as a band.
 */
export default function PercentileRail({ spec, initialWidth = 320 }: { spec: RailSpec; initialWidth?: number }) {
  const [ref, width] = useElementWidth<HTMLDivElement>(initialWidth)
  const hatchId = `rail-hatch-${useId().replace(/:/g, '')}`
  const L = layoutRail(spec, width)
  const y = L.trackY
  const left = L.x(spec.domain[0])
  const vx = L.valueX
  const labelLeft = vx !== null && vx < width / 2

  return (
    <div ref={ref} className="w-full min-w-0">
      <svg width={L.width} height={L.height} role="img" aria-label={spec.ariaLabel} className="block overflow-visible">
        <defs>
          <pattern id={hatchId} width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <line x1="0" y1="0" x2="0" y2="6" stroke="var(--silt)" strokeWidth="1.2" opacity="0.55" />
          </pattern>
        </defs>
        <rect x={left} y={y - 4} width={Math.max(0, L.x(spec.domain[1]) - left)} height={8} rx={4} fill="var(--surface-3)" />
        {L.band && (
          <g>
            <rect x={L.band.x0} y={y - 5} width={Math.max(2, L.band.x1 - L.band.x0)} height={10} fill="var(--silt-soft)" />
            <rect x={L.band.x0} y={y - 5} width={Math.max(2, L.band.x1 - L.band.x0)} height={10} fill={`url(#${hatchId})`} />
            <text x={(L.band.x0 + L.band.x1) / 2} y={L.bandLabelY} textAnchor="middle" className="fill-ink-2 text-[11px]">
              {L.band.label}
            </text>
          </g>
        )}
        {vx !== null && (
          <rect x={left} y={y - 4} width={Math.max(4, vx - left)} height={8} rx={4} fill="var(--water)" />
        )}
        {L.ticks.map((t) => (
          <g key={`${t.label}-${t.value}`}>
            <line
              x1={t.x}
              x2={t.x}
              y1={t.kind === 'reference' ? y + 5 : y - 7}
              y2={y + 10}
              stroke={t.kind === 'reference' ? 'var(--ink-3)' : 'var(--ink-2)'}
              strokeWidth={t.kind === 'reference' ? 1 : 1.5}
              strokeDasharray={t.kind === 'estimated' ? '3 2' : undefined}
            />
            {t.showLabel && (
              <text x={t.x} y={y + 21 + t.labelRow * 13} textAnchor="middle" className="fill-ink-3 text-[11px]">
                {t.label}
                {t.provisional ? '*' : ''}
              </text>
            )}
          </g>
        ))}
        {L.episodes.map((e) => (
          <g key={e.key}>
            <title>{e.title}</title>
            <OutcomeGlyph outcome={e.outcome} x={e.x} y={e.y} />
          </g>
        ))}
        {L.marker && (
          <g>
            <line x1={L.marker.x} x2={L.marker.x} y1={y - 12} y2={y + 12} stroke="var(--ink)" strokeWidth={2} strokeDasharray="1 3" strokeLinecap="round" />
            <path d={`M${L.marker.x},${y - 4}l4,4l-4,4l-4,-4z`} fill="var(--surface)" stroke="var(--ink)" strokeWidth={1.4} />
            <text
              x={L.marker.x < width / 2 ? Math.max(L.marker.x - 6, 2) : L.marker.x + 6}
              y={L.markerLabelY}
              textAnchor={L.marker.x < width / 2 ? 'start' : 'end'}
              className="fill-ink-2 text-[11px]"
            >
              {L.marker.label}
            </text>
          </g>
        )}
        {vx !== null && (
          <g>
            <line x1={vx} x2={vx} y1={14} y2={y + 7} stroke="var(--ink)" strokeWidth={2} />
            <circle cx={vx} cy={y} r={4} fill="var(--ink)" stroke="var(--surface)" strokeWidth={1.5} />
            <text
              x={labelLeft ? vx + 6 : vx - 6}
              y={22}
              textAnchor={labelLeft ? 'start' : 'end'}
              className="fill-ink text-[13px] font-bold"
            >
              {L.valueOutside === 'below' ? '◂ ' : ''}
              {spec.valueLabel}
              {L.valueOutside === 'above' ? ' ▸' : ''}
            </text>
          </g>
        )}
      </svg>
    </div>
  )
}
