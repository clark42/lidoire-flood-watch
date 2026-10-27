import type { PointerEvent } from 'react'
import { scaleLinear } from 'd3-scale'
import { line } from 'd3-shape'
import { formatNumber } from '../utils/format'
import { HOUR_MS, nearestIndex, type ReplaySeries, type TimePoint } from '../utils/replay'
import { formatParis, parisHour } from '../utils/time'
import ValueTag from './ui/ValueTag'

export interface SyncStripsProps {
  series: ReplaySeries
  /** Hourly times of the scrubber (ms) */
  times: readonly number[]
  index: number
  onScrub?: (index: number) => void
  width: number
  /** NGF zeros of Pessac and Libourne (m) */
  zeros: { pessac: number; libourne: number }
  /** Estimated confluence stage per hour (m NGF) */
  zc?: readonly TimePoint[]
  /** Labelled time of the episode (flood mark across the strips) */
  markerAt?: number | null
  markerLabel?: string
}

interface Row {
  key: 'levels' | 'eyraud' | 'score' | 'rain'
  title: string
  h: number
  top: number
}

/** Path with breaks where two points are more than maxGap apart. */
function gappedPath(
  points: readonly TimePoint[],
  x: (t: number) => number,
  y: (v: number) => number,
  maxGapMs: number,
): string {
  const withBreaks: (TimePoint | null)[] = []
  points.forEach((p, i) => {
    if (i > 0 && p.t - points[i - 1].t > maxGapMs) withBreaks.push(null)
    withBreaks.push(p)
  })
  return (
    line<TimePoint | null>()
      .defined((p) => p != null)
      .x((p) => x(p!.t))
      .y((p) => y(p!.v))(withBreaks) ?? ''
  )
}

function extent(values: number[], pad: number, fallback: [number, number]): [number, number] {
  if (values.length === 0) return fallback
  const lo = Math.min(...values)
  const hi = Math.max(...values)
  return [Math.floor(lo - pad), Math.ceil(hi + pad)]
}

const LEVEL_TICKS_STEP = 2

/**
 * Synchronised strips of the replay on one time axis (Paris days): Dordogne
 * levels in m NGF (Pessac, Libourne, estimated Z_c), Eyraud discharge, v3.4
 * score and its shadows, rain. Dragging on the strips moves the cursor.
 */
export default function SyncStrips({
  series,
  times,
  index,
  onScrub,
  width,
  zeros,
  zc = [],
  markerAt = null,
  markerLabel = 'inondation',
}: SyncStripsProps) {
  const narrow = width < 560
  const ml = narrow ? 32 : 44
  const mr = narrow ? 8 : 14
  const t0 = times[0] ?? 0
  const t1 = times[times.length - 1] ?? t0 + HOUR_MS
  const x = scaleLinear().domain([t0, t1]).range([ml, width - mr])
  const inRange = (p: TimePoint) => p.t >= t0 - HOUR_MS && p.t <= t1 + HOUR_MS
  const rowsDef: Omit<Row, 'top'>[] = [
    { key: 'levels', title: 'Dordogne, m NGF', h: narrow ? 80 : 92 },
    { key: 'eyraud', title: 'Eyraud, m³/s', h: 46 },
    { key: 'score', title: 'Score /100', h: 58 },
    { key: 'rain', title: 'Pluie, mm/h', h: 36 },
  ]
  let top = 18
  const rows: Row[] = rowsDef.map((r) => {
    const row = { ...r, top }
    top += r.h + 22
    return row
  })
  const height = top - 4
  const axisY = height - 12

  const pessacNgf = series.pessac.filter(inRange).map((p) => ({ t: p.t, v: p.v + zeros.pessac }))
  const libourneNgf = series.libourne.filter(inRange).map((p) => ({ t: p.t, v: p.v + zeros.libourne }))
  const zcIn = zc.filter(inRange)
  const lv = rows[0]
  const [lvLo, lvHi] = extent(
    [...pessacNgf, ...libourneNgf, ...zcIn].map((p) => p.v),
    0.3,
    [0, 10],
  )
  const yLv = scaleLinear().domain([lvLo, lvHi]).range([lv.top + lv.h, lv.top])
  const lvTicks: number[] = []
  const lvStep = lvHi - lvLo > 8 ? LEVEL_TICKS_STEP * 2 : LEVEL_TICKS_STEP
  for (let v = Math.ceil(lvLo / lvStep) * lvStep; v <= lvHi; v += lvStep) lvTicks.push(v)

  const eq = rows[1]
  const eyraud = series.eyraud.filter(inRange)
  const qMax = Math.max(1, ...eyraud.map((p) => p.v))
  const yQ = scaleLinear().domain([0, qMax * 1.1]).range([eq.top + eq.h, eq.top])
  const eyraudDaily =
    eyraud.length > 1 && (eyraud[eyraud.length - 1].t - eyraud[0].t) / (eyraud.length - 1) >= 12 * HOUR_MS

  const sc = rows[2]
  const yS = scaleLinear().domain([0, 100]).range([sc.top + sc.h, sc.top])
  const score = series.score.filter(inRange)
  const shadow = series.shadow.filter(inRange)
  const shadowV4 = series.shadowV4.filter(inRange)

  const rn = rows[3]
  const rain = series.rain.filter(inRange)
  const rMax = Math.max(2, ...rain.map((p) => p.v))
  const yR = scaleLinear().domain([0, rMax]).range([rn.top + rn.h, rn.top])
  const barW = Math.max(1, x(t0 + HOUR_MS) - x(t0) - 0.6)

  // Paris midnights (DST-safe: read the Paris hour of each hourly time)
  const midnights = times.filter((t) => parisHour(new Date(t)) === 0)
  const labelEvery = midnights.length > (narrow ? 5 : 9) ? 2 : 1
  const cursorT = times[Math.min(Math.max(index, 0), times.length - 1)] ?? t0
  const cursorX = x(cursorT)

  const scrubFrom = (ev: PointerEvent<SVGRectElement>) => {
    if (!onScrub) return
    const rect = (ev.currentTarget.ownerSVGElement ?? ev.currentTarget).getBoundingClientRect()
    const t = x.invert(Math.min(width - mr, Math.max(ml, ev.clientX - rect.left)))
    onScrub(nearestIndex(times, t))
  }

  const ariaLabel =
    `Séries synchronisées du ${formatParis(new Date(t0), 'dd/MM/yyyy HH:mm')} au ${formatParis(new Date(t1), 'dd/MM/yyyy HH:mm')} ` +
    `(heure de Paris) : niveaux de la Dordogne, débit de l’Eyraud, score, pluie. Curseur ${formatParis(new Date(cursorT), 'dd/MM HH:mm')}.`

  return (
    <div className="min-w-0">
      <svg width={width} height={height} role="img" aria-label={ariaLabel} className="block max-w-full touch-pan-y select-none">
        {rows.map((r) => (
          <text key={r.key} x={ml} y={r.top - 6} className="fill-ink-2 text-[11px] font-semibold">
            {r.title}
          </text>
        ))}

        {/* Days (Paris) */}
        {midnights.map((t, i) => (
          <g key={t}>
            <line x1={x(t)} x2={x(t)} y1={rows[0].top} y2={axisY - 8} stroke="var(--line)" strokeWidth="1" />
            {i % labelEvery === 0 && (
              <text x={x(t)} y={axisY + 4} textAnchor="middle" className="fill-ink-3 font-mono text-[10px]">
                {formatParis(new Date(t), narrow ? 'd' : 'EEE d')}
              </text>
            )}
          </g>
        ))}

        {/* Levels, m NGF */}
        {lvTicks.map((v) => (
          <g key={`lv${v}`}>
            <line x1={ml} x2={width - mr} y1={yLv(v)} y2={yLv(v)} stroke="var(--line)" />
            <text x={ml - 4} y={yLv(v) + 3.5} textAnchor="end" className="fill-ink-3 font-mono text-[10px]">
              {v}
            </text>
          </g>
        ))}
        <path d={gappedPath(libourneNgf, x, yLv, 4 * HOUR_MS)} fill="none" stroke="var(--ink-3)" strokeWidth="1.2" />
        {zcIn.length > 0 && (
          <path d={gappedPath(zcIn, x, yLv, 2 * HOUR_MS)} fill="none" stroke="var(--accent)" strokeWidth="1.4" strokeDasharray="5 3" />
        )}
        <path d={gappedPath(pessacNgf, x, yLv, 4 * HOUR_MS)} fill="none" stroke="var(--water)" strokeWidth="2" />

        {/* Eyraud discharge (analogue of the Lidoire) */}
        <line x1={ml} x2={width - mr} y1={yQ(0)} y2={yQ(0)} stroke="var(--line-2)" />
        <text x={ml - 4} y={yQ(qMax) + 3.5} textAnchor="end" className="fill-ink-3 font-mono text-[10px]">
          {formatNumber(qMax, qMax < 10 ? 1 : 0)}
        </text>
        <path d={gappedPath(eyraud, x, yQ, eyraudDaily ? 30 * HOUR_MS : 6 * HOUR_MS)} fill="none" stroke="var(--silt)" strokeWidth="1.6" strokeDasharray={eyraudDaily ? '4 3' : undefined} />
        {eyraud.length <= 60 &&
          eyraud.map((p) => <circle key={p.t} cx={x(p.t)} cy={yQ(p.v)} r="2" fill="var(--silt)" />)}

        {/* Scores */}
        {[15, 35, 55].map((v) => (
          <g key={`s${v}`}>
            <line x1={ml} x2={width - mr} y1={yS(v)} y2={yS(v)} stroke="var(--line)" />
            <text x={ml - 4} y={yS(v) + 3.5} textAnchor="end" className="fill-ink-3 font-mono text-[10px]">
              {v}
            </text>
          </g>
        ))}
        <path d={gappedPath(shadowV4, x, yS, 4 * HOUR_MS)} fill="none" stroke="var(--silt)" strokeWidth="1.5" strokeDasharray="1.5 3" strokeLinecap="round" />
        <path d={gappedPath(shadow, x, yS, 4 * HOUR_MS)} fill="none" stroke="var(--accent)" strokeWidth="1.5" strokeDasharray="5 3" />
        <path d={gappedPath(score, x, yS, 4 * HOUR_MS)} fill="none" stroke="var(--ink)" strokeWidth="1.8" />

        {/* Rain (Open-Meteo, hourly) */}
        {rain.map((p) =>
          p.v > 0 ? (
            <rect key={p.t} x={x(p.t) - barW / 2} y={yR(p.v)} width={barW} height={yR(0) - yR(p.v)} fill="var(--water)" />
          ) : null,
        )}
        <line x1={ml} x2={width - mr} y1={yR(0)} y2={yR(0)} stroke="var(--line-2)" />
        <text x={ml - 4} y={yR(rMax) + 3.5} textAnchor="end" className="fill-ink-3 font-mono text-[10px]">
          {formatNumber(rMax, 0)}
        </text>

        {/* Labelled time of the episode */}
        {markerAt != null && markerAt >= t0 && markerAt <= t1 && (
          <g data-marker={markerAt}>
            <line x1={x(markerAt)} x2={x(markerAt)} y1={rows[0].top - 2} y2={axisY - 8} stroke="var(--silt)" strokeWidth="1.5" strokeDasharray="3 2" />
            <text
              x={x(markerAt) + (x(markerAt) > width - 90 ? -4 : 4)}
              y={rows[0].top + 10}
              textAnchor={x(markerAt) > width - 90 ? 'end' : 'start'}
              className="fill-ink text-[10.5px] font-semibold"
            >
              {markerLabel}
            </text>
          </g>
        )}

        {/* Cursor and drag area */}
        <line x1={cursorX} x2={cursorX} y1={rows[0].top - 4} y2={axisY - 8} stroke="var(--ink)" strokeWidth="1.5" data-cursor={cursorT} />
        {onScrub && (
          <rect
            x={ml}
            y={0}
            width={Math.max(0, width - ml - mr)}
            height={axisY}
            fill="transparent"
            style={{ cursor: 'ew-resize' }}
            onPointerDown={(ev) => {
              ev.currentTarget.setPointerCapture?.(ev.pointerId)
              scrubFrom(ev)
            }}
            onPointerMove={(ev) => {
              if (ev.buttons) scrubFrom(ev)
            }}
          />
        )}
      </svg>
      <ul className="mt-1 flex flex-wrap gap-x-4 gap-y-1.5 text-[12.5px] text-ink-2" aria-label="Légende des séries">
        <li className="flex items-center gap-1.5">
          <Swatch color="var(--water)" /> Pessac <ValueTag kind="measured" />
        </li>
        <li className="flex items-center gap-1.5">
          <Swatch color="var(--ink-3)" thin /> Libourne (marée, hors score) <ValueTag kind="measured" />
        </li>
        <li className="flex items-center gap-1.5">
          <Swatch color="var(--accent)" dash="5 3" /> Z_c confluence <ValueTag kind="estimated" />
        </li>
        <li className="flex items-center gap-1.5">
          <Swatch color="var(--silt)" dash={eyraudDaily ? '4 3' : undefined} /> Eyraud{eyraudDaily ? ' (journalier)' : ''}{' '}
          <ValueTag kind="analog" />
        </li>
        <li className="flex items-center gap-1.5">
          <Swatch color="var(--ink)" /> Score v3.4 <ValueTag kind="model" />
        </li>
        <li className="flex items-center gap-1.5">
          <Swatch color="var(--accent)" dash="5 3" /> Shadow 3.4-gate
          {shadow.length === 0 && <span className="text-ink-3">(aucun point)</span>}
        </li>
        <li className="flex items-center gap-1.5">
          <Swatch color="var(--silt)" dash="1.5 3" /> Shadow 4.0a
          {shadowV4.length === 0 && <span className="text-ink-3">(aucun point)</span>}
        </li>
        <li className="flex items-center gap-1.5">
          <span className="inline-block h-2.5 w-2 bg-water" aria-hidden="true" /> Pluie Open-Meteo <ValueTag kind="model" />
        </li>
      </ul>
    </div>
  )
}

function Swatch({ color, dash, thin = false }: { color: string; dash?: string; thin?: boolean }) {
  return (
    <svg width="18" height="8" aria-hidden="true" className="flex-none">
      <line x1="1" x2="17" y1="4" y2="4" stroke={color} strokeWidth={thin ? 1.2 : 2} strokeDasharray={dash} strokeLinecap="round" />
    </svg>
  )
}
