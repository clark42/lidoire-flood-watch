import { useId, useState, type KeyboardEvent, type PointerEvent } from 'react'
import { line } from 'd3-shape'
import { scaleLinear } from 'd3-scale'
import type { EnsembleForecast, ForecastData, NowcastRainData, RadarBasinRain, RealtimeData } from '../types'
import { useElementWidth } from '../hooks/useElementWidth'
import { useEnsembleForecast, useRadarBasinRain, useRiskForecast } from '../hooks/useSharedResource'
import { availableModels, ENSEMBLE_MODEL_SHORT, modelLabel } from '../utils/ensemble'
import { LEVEL_THRESHOLDS } from '../utils/levels'
import { PESSAC_STATION_ID } from '../utils/mechanisms'
import { formatNumber, RISK_LEVEL_LABELS } from '../utils/format'
import { formatAge, formatParis, parseApiDate } from '../utils/time'
import {
  forecastPoints,
  levelRuns,
  nearestIndex,
  nightIntervals,
  peakIndex,
  rainRow,
  ribbonWindow,
  seriesMax,
  tideMarks,
  timeScale,
  timeTicks,
  vigicruesMarks,
  type RibbonHours,
} from '../utils/ribbon'
import ValueTag from './ui/ValueTag'
import InfoTooltip from './InfoTooltip'
import { TOOLTIPS } from '../tooltipContent'

const LEVEL_FILL = {
  green: 'var(--lv-green)',
  yellow: 'var(--lv-yellow)',
  orange: 'var(--lv-orange)',
  red: 'var(--lv-red)',
} as const

interface Row {
  key: 'score' | 'rain' | 'tide' | 'level'
  label: string
  h: number
  y: number
}

function layoutRows(): { rows: Row[]; height: number } {
  const spec: Omit<Row, 'y'>[] = [
    { key: 'score', label: 'Score v3.4', h: 58 },
    { key: 'rain', label: 'Pluie mm / 24 h', h: 64 },
    { key: 'tide', label: 'Pleines mers', h: 44 },
    { key: 'level', label: 'Pessac m', h: 56 },
  ]
  let y = 44
  const rows = spec.map((r) => {
    const row = { ...r, y }
    y += r.h + 30
    return row
  })
  return { rows, height: y - 20 }
}

export interface HorizonRibbonViewProps {
  forecast: ForecastData | null
  ensemble: EnsembleForecast | null
  realtime: RealtimeData | null
  /** OPERA radar rain over the basin (undefined = not asked, null = unavailable) */
  radar?: RadarBasinRain | null
  now: Date
  initialHours?: RibbonHours
  initialModel?: string
  initialWidth?: number
}

const RADAR_WINDOWS = ['1h', '3h', '6h', '24h'] as const

/**
 * Observed rain, just before the ribbon starts: OPERA radar over the basin
 * (last 1 / 3 / 6 / 24 h) and the next-hour nowcast. One compact row.
 */
export function ObservedRainRow({
  radar,
  nowcast,
  now,
}: {
  radar?: RadarBasinRain | null
  nowcast?: NowcastRainData | null
  now: Date
}) {
  if (radar === undefined && !nowcast) return null
  const last = parseApiDate(radar?.last_image_time ?? null)
  return (
    <div className="mt-3 grid gap-1.5 rounded-lg border border-line px-3 py-2 text-[13.5px] text-ink-2" data-testid="observed-rain">
      <p className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="inline-flex items-center gap-1 font-bold text-ink">
          Pluie observée sur le bassin (radar)
          <InfoTooltip {...TOOLTIPS.radarBasin} />
        </span>
        {radar?.available ? (
          <>
            {RADAR_WINDOWS.map((w) => {
              const win = radar.windows[w]
              return (
                <span key={w} className="whitespace-nowrap">
                  {w.replace('h', ' h')}{' '}
                  <b className="num text-[15px] text-ink">{win?.mm != null ? formatNumber(win.mm, 1) : '–'}</b>
                  {win && !win.complete && <span title="heures manquantes">*</span>}
                </span>
              )
            })}
            <span>mm</span>
            <ValueTag kind="measured" className="text-[11px]" />
            {last && (
              <span className="font-mono text-[11.5px] text-ink-3">
                image {formatParis(last, 'HH:mm')} ({formatAge(last, now.getTime())})
              </span>
            )}
          </>
        ) : radar === null ? (
          <span>indisponible.</span>
        ) : radar ? (
          <span>indisponible{radar.note ? ` : ${radar.note}` : ''}.</span>
        ) : (
          <span className="text-ink-3">chargement…</span>
        )}
      </p>
      {nowcast && (
        <p className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          <span className="inline-flex items-center gap-1 font-bold text-ink">
            Prochaine heure
            <InfoTooltip {...TOOLTIPS.timelineNowcast} />
          </span>
          <span>
            <b className="num text-[15px] text-ink">{formatNumber(nowcast.total_60min_mm, 1)}</b> mm
            {nowcast.rain_minutes > 0
              ? `, ${nowcast.rain_minutes} min de pluie, intensité max ${formatNumber(nowcast.max_intensity_mm * 60, 1)} mm/h`
              : ', pas de pluie prévue'}
          </span>
          <ValueTag kind="model" className="text-[11px]" />
          <span className="font-mono text-[11.5px] text-ink-3">{nowcast.commune}</span>
        </p>
      )}
    </div>
  )
}

/** Segmented control (aria-pressed buttons, 44 px targets on mobile). */
function Segmented<T extends string | number>({
  label,
  options,
  value,
  onChange,
}: {
  label: string
  options: { value: T; label: string; disabled?: boolean }[]
  value: T
  onChange: (v: T) => void
}) {
  return (
    <div role="group" aria-label={label} className="inline-flex overflow-hidden rounded-lg border border-line-2">
      {options.map((o) => (
        <button
          key={String(o.value)}
          type="button"
          aria-pressed={o.value === value}
          disabled={o.disabled}
          onClick={() => onChange(o.value)}
          className={`min-h-[44px] px-3 text-sm sm:min-h-[34px] ${
            o.value === value ? 'bg-accent font-bold text-accent-ink' : 'bg-surface text-ink hover:bg-surface-2'
          } disabled:cursor-not-allowed disabled:text-ink-3`}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

/**
 * 72 h / 7 days on one Paris-time axis, nights shaded: forecast score
 * (model, no band), ensemble rain (median, p10-p90, wettest member, model
 * switch), estimated high tides sized by coefficient with the Vigicrues
 * forecast at Libourne, projected Pessac (no band). Pointer or arrow keys
 * move a cursor; the values at the cursor are written under the chart.
 */
export function HorizonRibbonView({
  forecast,
  ensemble,
  realtime,
  radar,
  now,
  initialHours = 72,
  initialModel,
  initialWidth = 343,
}: HorizonRibbonViewProps) {
  const models = availableModels(ensemble)
  const [hours, setHours] = useState<RibbonHours>(initialHours)
  const [model, setModel] = useState<string>(initialModel ?? models[0] ?? 'ecmwf_ifs025')
  const [cursor, setCursor] = useState<number | null>(null)
  const [ref, width] = useElementWidth<HTMLDivElement>(initialWidth)
  const hatchId = `ribbon-hatch-${useId().replace(/:/g, '')}`

  const narrow = width < 560
  const ml = narrow ? 34 : 50
  const mr = 8
  const [t0, t1] = ribbonWindow(now, hours)
  const x = timeScale([t0, t1], [ml, width - mr])
  const { rows, height } = layoutRows()
  const [R0, R1, R2, R3] = rows

  const points = forecastPoints(forecast, t0, t1)
  const selected = points.length === 0 ? -1 : cursor !== null && cursor < points.length ? cursor : peakIndex(points)
  const sel = selected >= 0 ? points[selected] : null

  // Score row
  const scoreTop = Math.min(100, seriesMax(points.map((p) => p.score + 10), 60))
  const ys = scaleLinear().domain([0, scoreTop]).range([R0.y + R0.h, R0.y])
  const scorePath = line<(typeof points)[number]>()
    .x((p) => x(p.t))
    .y((p) => ys(p.score))(points)

  // Rain row
  const rain = rainRow(ensemble, model, t0, t1)
  const rainTop = seriesMax(rain.blocks.map((b) => b.max), 2)
  const yr = scaleLinear().domain([0, rainTop]).range([R1.y + R1.h, R1.y + 2]).nice()
  const rainTicks = yr.ticks(2)
  const lastRain = rain.blocks.length ? rain.blocks[rain.blocks.length - 1].t1 : t0
  const rainGap = rain.horizonEnd != null ? Math.max(lastRain, rain.horizonEnd) : lastRain
  const selRain = sel ? rain.blocks.find((b) => sel.t > b.t0 && sel.t <= b.t1) ?? null : null

  // Tide row
  // Only high waters still to come: the first one is the outlook's next_high_tide (verdict, tide card)
  const tides = tideMarks(forecast?.high_tides, Math.max(t0, now.getTime()), t1)
  const vigi = realtime?.river?.source === 'vigicrues' ? vigicruesMarks(realtime.river.forecast_scenarios, t0, t1) : []
  const vigiTop = vigi.reduce<(typeof vigi)[number] | null>((b, v) => (b === null || v.mean_m > b.mean_m ? v : b), null)
  const tideY = R2.y + R2.h / 2 - 4
  const nextTide = sel ? tides.find((t) => t.t >= sel.t) ?? null : null

  // Pessac row
  const pessac = realtime?.stations?.stations.find((s) => s.station_id === PESSAC_STATION_ID)
  const yellow = pessac?.threshold_yellow ?? null
  const levelTop = seriesMax(points.map((p) => p.pessac), yellow != null ? yellow + 0.5 : 1)
  const yl = scaleLinear().domain([0, levelTop]).range([R3.y + R3.h, R3.y]).nice()
  const levelPath = line<(typeof points)[number]>()
    .x((p) => x(p.t))
    .y((p) => yl(p.pessac))(points)

  const nights = nightIntervals(t0, t1)
  // Under ~60 px per day, the day labels keep only the date
  const ticks = timeTicks(t0, t1, hours, narrow, (width - ml - mr) / (hours / 24) < 60)
  const tideScale = hours === 168 && narrow ? 0.65 : 1
  const nowX = x(now.getTime())

  const step = hours === 72 ? 1 : 3
  const onKey = (e: KeyboardEvent<SVGSVGElement>) => {
    if (points.length === 0) return
    let next = selected
    if (e.key === 'ArrowRight') next = Math.min(points.length - 1, selected + step)
    else if (e.key === 'ArrowLeft') next = Math.max(0, selected - step)
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = points.length - 1
    else return
    e.preventDefault()
    setCursor(next)
  }
  const onPointer = (e: PointerEvent<SVGSVGElement>) => {
    const box = e.currentTarget.getBoundingClientRect()
    const t = x.invert(e.clientX - box.left)
    setCursor(nearestIndex(points, t))
  }

  const modelName = ENSEMBLE_MODEL_SHORT[model] ?? model
  const generated = forecast?.generated_at ? formatParis(forecast.generated_at, 'HH:mm') : '–'

  return (
    <div className="rounded-card border border-line bg-surface p-3 sm:p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Segmented
          label="Horizon"
          value={hours}
          onChange={(v) => {
            setHours(v)
            setCursor(null)
          }}
          options={[
            { value: 72 as RibbonHours, label: '72 h' },
            { value: 168 as RibbonHours, label: '7 jours' },
          ]}
        />
        <Segmented
          label="Modèle de pluie d'ensemble"
          value={model}
          onChange={setModel}
          options={(['icon_d2', 'icon_eu', 'ecmwf_ifs025'] as const).map((id) => ({
            value: id,
            label: ENSEMBLE_MODEL_SHORT[id],
            disabled: !models.includes(id),
          }))}
        />
      </div>

      <div ref={ref} className="mt-3 w-full min-w-0">
        {points.length === 0 ? (
          <p className="py-8 text-center text-ink-2">Prévision du score indisponible.</p>
        ) : (
          <svg
            width={width}
            height={height}
            role="img"
            aria-label={`Ruban des ${hours === 72 ? '72 prochaines heures' : '7 prochains jours'} : score prévu, pluie d'ensemble, pleines mers et Pessac projeté. Flèches gauche et droite pour déplacer le curseur.`}
            tabIndex={0}
            onKeyDown={onKey}
            onPointerMove={onPointer}
            onPointerDown={onPointer}
            className="block touch-pan-y select-none"
          >
            <defs>
              <pattern id={hatchId} width="7" height="7" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
                <line x1="0" y1="0" x2="0" y2="7" stroke="var(--line-2)" strokeWidth="1" />
              </pattern>
            </defs>

            {nights.map(([a, b]) => (
              <rect key={a} x={x(a)} y={18} width={Math.max(0, x(b) - x(a))} height={height - 18} fill="var(--night)" />
            ))}
            {ticks.map((t) => (
              <g key={t.t}>
                <line x1={x(t.t)} x2={x(t.t)} y1={18} y2={height} stroke="var(--line)" strokeOpacity={t.major ? 1 : 0.6} />
                {x(t.t) < width - mr - 18 && (
                  <text x={x(t.t) + 3} y={12} className={t.major ? 'fill-ink text-[12px] font-bold' : 'fill-ink-3 text-[11px]'}>
                    {t.label}
                  </text>
                )}
              </g>
            ))}

            {rows.map((r) => (
              <text key={r.key} x={ml} y={r.y - 8} className="fill-ink text-[12px] font-bold">
                {r.label}
              </text>
            ))}

            {/* Score: level strip + dotted line (model, no band) */}
            {[LEVEL_THRESHOLDS.yellow, LEVEL_THRESHOLDS.orange, LEVEL_THRESHOLDS.red]
              .filter((v) => v <= scoreTop)
              .map((v) => (
                <g key={v}>
                  <line x1={ml} x2={width - mr} y1={ys(v)} y2={ys(v)} stroke="var(--line)" />
                  <text x={ml - 4} y={ys(v) + 3.5} textAnchor="end" className="fill-ink-3 text-[11px]">
                    {v}
                  </text>
                </g>
              ))}
            {levelRuns(points).map((r) => (
              <rect key={r.t0} x={x(r.t0)} y={R0.y + R0.h - 5} width={Math.max(1, x(r.t1) - x(r.t0))} height={5} fill={LEVEL_FILL[r.level]} />
            ))}
            {scorePath && (
              <path d={scorePath} fill="none" stroke="var(--ink)" strokeWidth={2} strokeDasharray="1 3.5" strokeLinecap="round" />
            )}
            <text x={width - mr} y={R0.y - 8} textAnchor="end" className="fill-ink-3 text-[11px]">
              ~ modèle, sans bande
            </text>

            {/* Rain: p10-p90 band, median bar, wettest member */}
            {rainTicks.map((v) => (
              <g key={v}>
                <line x1={ml} x2={width - mr} y1={yr(v)} y2={yr(v)} stroke="var(--line)" />
                <text x={ml - 4} y={yr(v) + 3.5} textAnchor="end" className="fill-ink-3 text-[11px]">
                  {formatNumber(v, Number.isInteger(v) ? 0 : 1)}
                </text>
              </g>
            ))}
            {rain.blocks.map((b) => {
              const xa = x(b.t0) + 1
              const xb = x(b.t1) - 1
              const w = Math.max(1, xb - xa)
              const barW = Math.max(2, w * 0.45)
              return (
                <g key={b.t0}>
                  <title>{`${formatParis(new Date(b.t0), 'EEE d HH:mm')}-${formatParis(new Date(b.t1), 'HH:mm')} : médiane ${formatNumber(b.p50, 1)} mm, p10-p90 ${formatNumber(b.p10, 1)}-${formatNumber(b.p90, 1)} mm, membre max ${formatNumber(b.max, 1)} mm`}</title>
                  <rect x={xa} y={yr(b.p90)} width={w} height={Math.max(0, yr(b.p10) - yr(b.p90))} fill="var(--water-soft)" />
                  <rect x={xa + (w - barW) / 2} y={yr(b.p50)} width={barW} height={Math.max(0, yr(0) - yr(b.p50))} fill="var(--water)" />
                  <line x1={xa} x2={xb} y1={yr(b.max)} y2={yr(b.max)} stroke="var(--water)" strokeWidth={1.4} strokeDasharray="3 2" />
                </g>
              )
            })}
            {rainGap < t1 && (
              <g>
                <rect x={x(rainGap)} y={R1.y} width={Math.max(0, x(t1) - x(rainGap))} height={R1.h} fill={`url(#${hatchId})`} />
                {x(t1) - x(rainGap) > 110 && (
                  <text x={(x(rainGap) + x(t1)) / 2} y={R1.y + R1.h / 2 + 4} textAnchor="middle" className="fill-ink-2 text-[11px]">
                    au-delà de {modelName}
                  </text>
                )}
              </g>
            )}
            <line x1={ml} x2={width - mr} y1={yr(0)} y2={yr(0)} stroke="var(--line-2)" />
            <text x={width - mr} y={R1.y - 8} textAnchor="end" className="fill-ink-3 text-[11px]">
              {narrow ? `~ ${modelName}` : `~ ensemble ${modelLabel(ensemble, model)}, ${rain.members} membres`}
            </text>

            {/* High tides (estimated at the confluence) and Vigicrues Libourne */}
            <line x1={ml} x2={width - mr} y1={tideY} y2={tideY} stroke="var(--line-2)" />
            {tides.map((t) => (
              <g key={t.t}>
                <circle cx={x(t.t)} cy={tideY} r={t.r * tideScale} fill="var(--surface)" stroke="var(--accent)" strokeWidth={1.8} strokeDasharray="3 2" />
                {t.coefficient != null && (!narrow || hours === 72) && (
                  <text x={x(t.t)} y={tideY - t.r - 4} textAnchor="middle" className="fill-ink-3 text-[11px]">
                    ≈{t.coefficient}
                  </text>
                )}
              </g>
            ))}
            {vigi.map((v) => (
              <g key={v.t}>
                <title>{`Vigicrues Libourne ${formatParis(new Date(v.t), 'EEE d HH:mm')} : ${formatNumber(v.mean_m, 2)} m [${formatNumber(v.min_m, 2)}-${formatNumber(v.max_m, 2)}]`}</title>
                <rect x={x(v.t) - 2.5} y={tideY + 12} width={5} height={9} fill="var(--ink)" />
              </g>
            ))}
            {vigiTop && (
              <text
                x={Math.min(x(vigiTop.t) + 6, width - mr)}
                y={tideY + 20}
                textAnchor={x(vigiTop.t) + 6 > width - 150 ? 'end' : 'start'}
                className="fill-ink-2 text-[11px]"
              >
                Vigicrues Libourne {formatNumber(vigiTop.mean_m, 2)} m [{formatNumber(vigiTop.min_m, 2)}–{formatNumber(vigiTop.max_m, 2)}]
              </text>
            )}
            <text x={width - mr} y={R2.y - 8} textAnchor="end" className="fill-ink-3 text-[11px]">
              {narrow ? '≈ estimées' : '≈ estimées à la confluence, taille = coefficient'}
            </text>

            {/* Projected Pessac (model, no band) */}
            {yl.ticks(3).map((v) => (
              <g key={v}>
                <line x1={ml} x2={width - mr} y1={yl(v)} y2={yl(v)} stroke="var(--line)" />
                <text x={ml - 4} y={yl(v) + 3.5} textAnchor="end" className="fill-ink-3 text-[11px]">
                  {formatNumber(v, Number.isInteger(v) ? 0 : 1)}
                </text>
              </g>
            ))}
            {yellow != null && (
              <g>
                <line x1={ml} x2={width - mr} y1={yl(yellow)} y2={yl(yellow)} stroke="var(--lv-yellow)" strokeWidth={1.6} />
                <text x={ml + 4} y={yl(yellow) - 4} className="fill-ink-2 text-[11px]">
                  jaune {formatNumber(yellow, 1)} m
                </text>
              </g>
            )}
            {levelPath && (
              <path d={levelPath} fill="none" stroke="var(--ink)" strokeWidth={2} strokeDasharray="1 3.5" strokeLinecap="round" />
            )}
            <text x={width - mr} y={R3.y - 8} textAnchor="end" className="fill-ink-3 text-[11px]">
              ~ projection, sans bande
            </text>

            {/* Now and cursor */}
            <line x1={nowX} x2={nowX} y1={18} y2={height} stroke="var(--ink)" strokeWidth={1.5} />
            {sel && (
              <line x1={x(sel.t)} x2={x(sel.t)} y1={18} y2={height} stroke="var(--accent)" strokeWidth={1.5} strokeDasharray="4 3" />
            )}
          </svg>
        )}
      </div>

      {sel && (
        <p className="mt-2 rounded-lg bg-surface-2 px-3 py-2 text-[13.5px] text-ink-2" data-testid="ribbon-readout">
          <span className="font-mono text-[12.5px] text-ink">
            {formatParis(new Date(sel.t), 'EEE d HH:mm')} · H+{sel.hoursAhead}
          </span>
          {cursor === null && <span className="text-ink-3"> (pic du score)</span>}
          <br />
          Score <b className="text-ink">~ {formatNumber(sel.score, 0)}/100</b> ({RISK_LEVEL_LABELS[sel.level]}) · Pessac{' '}
          <b className="text-ink">~ {formatNumber(sel.pessac, 2)} m</b> · Pluie {modelName} sur 24 h{' '}
          {selRain ? (
            <b className="text-ink">
              {formatNumber(selRain.p50, 1)} mm [{formatNumber(selRain.p10, 1)}–{formatNumber(selRain.p90, 1)}], max {formatNumber(selRain.max, 1)}
            </b>
          ) : (
            'hors horizon'
          )}
          {selRain && rain.thresholds.length > 0 && (
            <>
              {' '}
              · part des membres au-delà de {rain.thresholds.map((t) => formatNumber(t, 0)).join(' / ')} mm :{' '}
              <b className="text-ink">
                {rain.thresholds.map((t) => formatNumber((selRain.prob[String(t)] ?? 0) * 100, 0)).join(' / ')} %
              </b>
            </>
          )}
          {nextTide && (
            <>
              {' '}
              · PM suivante ≈ {formatParis(new Date(nextTide.t), 'HH:mm')}
              {nextTide.coefficient != null ? ` (coef. ${nextTide.coefficient})` : ''}
            </>
          )}
        </p>
      )}

      <p className="mt-2 text-[12.5px] leading-relaxed text-ink-3">
        <ValueTag kind="model" /> Score et Pessac : prévision v{forecast?.model_version ?? '3.4'} de{' '}
        <span className="font-mono">{generated}</span>, en pointillé car <b className="text-ink-2">sans bande</b> d&apos;incertitude.
        Pluie : ensemble par fenêtres de 24 h depuis l&apos;émission, barre = médiane, bande = p10-p90, tirets = membre le plus
        pluvieux ; au curseur, part des membres au-delà des seuils 24 h <InfoTooltip {...TOOLTIPS.ensembleRain} />.{' '}
        <ValueTag kind="estimated" /> Pleines mers estimées à la confluence (marée modèle décalée), taille = coefficient ;
        carrés noirs = prévision officielle Vigicrues à Libourne. Zones grisées : nuits (20 h-7 h, heure de Paris).
        {' '}Ensembles et radar : informatifs, hors score.
        {ensemble?.rain?.attribution && <span className="block">{ensemble.rain.attribution}</span>}
      </p>

      <ObservedRainRow radar={radar} nowcast={realtime?.nowcast_rain} now={now} />
    </div>
  )
}

/** Container: shared /api/risk/forecast (168 h) and /api/forecast/ensemble. Live mode only. */
export default function HorizonRibbon({ realtime, now }: { realtime: RealtimeData | null; now: Date }) {
  const forecast = useRiskForecast()
  const ensemble = useEnsembleForecast()
  const radar = useRadarBasinRain()
  if (!forecast.data && forecast.loading) {
    return <div className="h-[420px] animate-pulse rounded-card bg-surface-2" aria-busy="true" aria-label="Chargement du ruban" />
  }
  if (!forecast.data) {
    return (
      <div role="alert" className="flex flex-wrap items-center gap-3 rounded-card border border-line bg-surface p-4 text-ink-2">
        <span className="flex-1">Prévision horaire indisponible{forecast.error ? ` : ${forecast.error}` : ''}.</span>
        <button
          type="button"
          onClick={forecast.refresh}
          className="min-h-[44px] rounded-lg border border-line-2 bg-surface px-3.5 text-sm text-ink hover:border-ink-3"
        >
          Réessayer
        </button>
      </div>
    )
  }
  return (
    <HorizonRibbonView
      forecast={forecast.data}
      ensemble={ensemble.data}
      realtime={realtime}
      radar={radar.loading && !radar.data ? undefined : radar.data}
      now={now}
    />
  )
}
