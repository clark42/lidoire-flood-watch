import { useMemo, useState } from 'react'
import { getBasinGeoJson, getRadarFrameMeta, getRiskAnalogs, getVigilance } from '../api'
import type { BasinGeoJson, RadarFrameMeta, RiskAnalogsData, StationData, VigilanceLevel, VigilanceStatus } from '../types'
import { useApiResource } from '../hooks/useApiResource'
import { useElementWidth } from '../hooks/useElementWidth'
import { buildBasinMap, mapPercentiles, type BasinMapModel } from '../utils/basinMap'
import { PERCENTILE_CLASS_LABELS } from '../utils/mapGeo'
import { formatAge, formatParis, parseApiDate } from '../utils/time'
import { formatNumber } from '../utils/format'
import ValueTag from './ui/ValueTag'
import '../styles/field.css'

const VIGILANCE_VAR: Record<VigilanceLevel, string> = {
  1: 'var(--lv-green)',
  2: 'var(--lv-yellow)',
  3: 'var(--lv-orange)',
  4: 'var(--lv-red)',
}
const VIGILANCE_WORD: Record<VigilanceLevel, string> = { 1: 'vert', 2: 'jaune', 3: 'orange', 4: 'rouge' }

const seqVar = (c: number) => `var(--seq-${c})`

function Swatch({ color, round = false, dashed = false }: { color: string; round?: boolean; dashed?: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={`inline-block h-2.5 w-3.5 flex-none border ${round ? 'rounded-full' : 'rounded-sm'} ${dashed ? 'border-dashed border-ink-3' : 'border-line-2'}`}
      style={{ background: color }}
    />
  )
}

/** Shape of an indicator marker, shared by the map and its legend */
function MarkerShape({ shape, x, y }: { shape: 'square' | 'triangle'; x: number; y: number }) {
  return shape === 'square' ? (
    <rect x={x - 4} y={y - 4} width={8} height={8} className="bmap-marker" />
  ) : (
    <path d={`M${x},${y - 5}L${x + 5},${y + 4}L${x - 5},${y + 4}Z`} className="bmap-marker" />
  )
}

export interface BasinMapViewProps {
  model: BasinMapModel
  /** Radar overlay requested by the user */
  radarOn: boolean
  radarMeta?: RadarFrameMeta | null
  radarError?: string | null
  /** Basis of the percentiles (period), from /api/risk/analogs */
  percentileBasis?: string | null
  now: Date
}

/** SVG of the basin, then its legend (pure: everything comes from the model). */
export function BasinMapView({ model, radarOn, radarMeta, radarError, percentileBasis, now }: BasinMapViewProps) {
  const m = model
  const radarTime = parseApiDate(radarMeta?.time)
  const levelsShown = Array.from(
    new Set(m.troncons.map((t) => t.level).filter((l): l is VigilanceLevel => l != null)),
  ).sort()
  const hasUnknownPercentile = m.rivers.some((r) => r.measured && r.seq === null)

  return (
    <div className="bmap">
      <svg
        width={m.width}
        height={m.height}
        viewBox={`0 0 ${m.width} ${m.height}`}
        role="img"
        aria-label={`Carte du bassin de la Lidoire : moulin, rivières, ${m.stations.length} stations, tronçons Vigicrues${radarOn ? ', radar de pluie' : ''}`}
      >
        <defs>
          <pattern id="bmap-hatch" width="7" height="7" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <line x1="0" y1="0" x2="0" y2="7" className="bmap-hatch-line" />
          </pattern>
        </defs>
        <rect x={0} y={0} width={m.width} height={m.height} className="bmap-bg" />

        {m.basinD && (
          <>
            <path d={m.basinD} className="bmap-basin" />
            <path d={m.basinD} fill="url(#bmap-hatch)" />
          </>
        )}

        {radarOn && m.radar && (
          <g>
            <image
              href={m.radar.url}
              x={m.radar.rect.x}
              y={m.radar.rect.y}
              width={m.radar.rect.width}
              height={m.radar.rect.height}
              preserveAspectRatio="none"
              className="bmap-radar"
            />
            <rect
              x={m.radar.rect.x}
              y={m.radar.rect.y}
              width={m.radar.rect.width}
              height={m.radar.rect.height}
              className="bmap-radar-frame"
            />
          </g>
        )}

        {/* Vigicrues reaches: wide translucent casing under the river */}
        {m.troncons.map((t) => (
          <path
            key={t.code}
            d={t.d}
            className="bmap-troncon"
            style={{ stroke: t.level ? VIGILANCE_VAR[t.level] : 'var(--ink-3)' }}
          >
            <title>{`Tronçon Vigicrues ${t.code} ${t.name} : ${t.level ? VIGILANCE_WORD[t.level] : 'niveau inconnu'}`}</title>
          </path>
        ))}

        {m.rivers.map((r) => (
          <g key={r.key}>
            <path d={r.d} className="bmap-river-halo" style={{ strokeWidth: r.width + 2.5 }} />
            <path
              d={r.d}
              className="bmap-river"
              style={{ stroke: r.seq ? seqVar(r.seq) : 'var(--water)', strokeWidth: r.width }}
            >
              <title>
                {r.measured
                  ? `${r.name} : ${r.percentile != null ? `percentile ${Math.round(r.percentile)}` : 'percentile non servi'}`
                  : `${r.name} : non mesuré`}
              </title>
            </path>
            {/* Pale low classes stay visible on the light surface */}
            {r.seq === 1 && <path d={r.d} className="bmap-river-core" />}
          </g>
        ))}

        {m.lidoireD && (
          <g>
            <path d={m.lidoireD} className="bmap-lidoire" />
            <path d={m.lidoireD} className="bmap-lidoire-flow" />
            <title>Lidoire : aucune station, niveau jamais mesuré</title>
          </g>
        )}

        {m.basinLabel && (
          <text x={m.basinLabel.x} y={m.basinLabel.y} className="bmap-ax">
            {m.basinLabel.text}
          </text>
        )}
        {m.labels.map((l) => (
          <text key={l.text} x={l.x} y={l.y} className={l.strong ? 'bmap-lab' : undefined}>
            {l.text}
          </text>
        ))}

        {m.markers.map((mk) => (
          <g key={mk.id}>
            <title>{mk.title}</title>
            <MarkerShape shape={mk.shape} x={mk.x} y={mk.y} />
            <text
              x={mk.labelLeft ? mk.x - 7 : mk.x + 7}
              y={mk.y + 12}
              textAnchor={mk.labelLeft ? 'end' : 'start'}
              className="bmap-ax"
            >
              {mk.label}
            </text>
          </g>
        ))}

        {m.stations.map((s) => (
          <g key={s.id}>
            <title>{s.title}</title>
            <circle
              cx={s.x}
              cy={s.y}
              r={s.r}
              className={s.tide ? 'bmap-station bmap-station--tide' : 'bmap-station'}
              style={s.tide ? undefined : { fill: s.seq ? seqVar(s.seq) : 'var(--surface)' }}
            />
            <text
              x={s.labelLeft ? s.x - s.r - 3 : s.x + s.r + 3}
              y={s.y - s.r + 1}
              textAnchor={s.labelLeft ? 'end' : 'start'}
              className="bmap-ax"
            >
              {s.label}
            </text>
          </g>
        ))}

        <g>
          <title>Moulin de la Lidoire</title>
          <circle cx={m.mill.x} cy={m.mill.y} r={6} className="bmap-pulse" />
          <rect
            x={m.mill.x - 5.5}
            y={m.mill.y - 5.5}
            width={11}
            height={11}
            transform={`rotate(45 ${m.mill.x} ${m.mill.y})`}
            className="bmap-mill"
          />
          <text x={m.mill.x - 10} y={m.mill.y + 18} textAnchor="end" className="bmap-lab">
            Moulin
          </text>
        </g>

        {m.scale && (
          <g aria-hidden="true">
            <line
              x1={m.width - 12 - m.scale.px}
              x2={m.width - 12}
              y1={m.height - 12}
              y2={m.height - 12}
              className="bmap-scale"
            />
            <line x1={m.width - 12 - m.scale.px} x2={m.width - 12 - m.scale.px} y1={m.height - 16} y2={m.height - 8} className="bmap-scale" />
            <line x1={m.width - 12} x2={m.width - 12} y1={m.height - 16} y2={m.height - 8} className="bmap-scale" />
            <text x={m.width - 12 - m.scale.px / 2} y={m.height - 18} textAnchor="middle" className="bmap-ax">
              {`${m.scale.km} km`}
            </text>
          </g>
        )}
      </svg>

      {radarOn && (
        <p className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[12.5px] text-ink-2" role="status">
          <ValueTag kind="measured">radar</ValueTag>
          {radarError ? (
            <span>Radar indisponible : {radarError}</span>
          ) : !radarMeta ? (
            <span>Chargement de l’image radar…</span>
          ) : !radarMeta.available || !m.radar ? (
            <span>Aucune image radar récente.</span>
          ) : (
            <>
              <span>
                Cumul {radarMeta.accumulation_minutes} min finissant à{' '}
                <span className="font-mono">{formatParis(radarTime, 'HH:mm')}</span>
                {radarTime && <> ({formatAge(radarTime, now.getTime())})</>}
                {' · '}
                {radarMeta.rain_pixels > 0
                  ? `max ${formatNumber(radarMeta.max_mm, 1)} mm sur l’emprise`
                  : 'aucune pluie détectée sur l’emprise'}
              </span>
              <span className="flex flex-wrap items-center gap-1" aria-label="Échelle du radar en mm par heure">
                {radarMeta.legend.map((step) => (
                  <span key={step.min_mm} className="inline-flex items-center gap-0.5 font-mono text-[11px]">
                    <Swatch color={`rgba(${step.rgba[0]},${step.rgba[1]},${step.rgba[2]},${(step.rgba[3] / 255).toFixed(2)})`} />
                    {formatNumber(step.min_mm, step.min_mm < 1 ? 1 : 0)}
                  </span>
                ))}
                <span className="text-[11px]">mm/h</span>
              </span>
              <a href={radarMeta.licence_url} className="underline" target="_blank" rel="noreferrer">
                {radarMeta.attribution}
              </a>
            </>
          )}
        </p>
      )}

      <div className="mt-2 flex flex-wrap gap-x-3.5 gap-y-1.5 text-[12px] text-ink-2" aria-label="Légende de la carte">
        <span className="inline-flex items-center gap-1.5">Percentile du niveau :</span>
        {([1, 2, 3, 4, 5] as const).map((c) => (
          <span key={c} className="inline-flex items-center gap-1.5">
            <Swatch color={seqVar(c)} />
            {PERCENTILE_CLASS_LABELS[c]}
          </span>
        ))}
        {hasUnknownPercentile && (
          <span className="inline-flex items-center gap-1.5">
            <Swatch color="var(--water)" />
            percentile non servi
          </span>
        )}
        {levelsShown.map((l) => (
          <span key={l} className="inline-flex items-center gap-1.5">
            <span aria-hidden="true" className="inline-block h-2 w-[18px] rounded opacity-40" style={{ background: VIGILANCE_VAR[l] }} />
            tronçon Vigicrues ({VIGILANCE_WORD[l]})
          </span>
        ))}
        <span className="inline-flex items-center gap-1.5">
          <span aria-hidden="true" className="inline-block h-0 w-[18px] border-t-2 border-dashed border-accent" />
          Lidoire, non mesurée
        </span>
        <span className="inline-flex items-center gap-1.5">
          <Swatch color="var(--surface)" round dashed />
          Libourne : marée, hors score
        </span>
        <span className="inline-flex items-center gap-1.5">
          <svg width="12" height="12" aria-hidden="true">
            <MarkerShape shape="square" x={6} y={6} />
          </svg>
          analogue
        </span>
        <span className="inline-flex items-center gap-1.5">
          <svg width="12" height="12" aria-hidden="true">
            <MarkerShape shape="triangle" x={6} y={7} />
          </svg>
          nappe
        </span>
      </div>
      {(percentileBasis || m.offMap.length > 0) && (
        <p className="mt-1.5 text-[12px] text-ink-3">
          {percentileBasis && <>Percentiles : {percentileBasis}. </>}
          {m.offMap.length > 0 && (
            <>
              Hors cadre :{' '}
              {m.offMap.map((s) => `${s.name} (${s.direction}, ${Math.round(s.distanceKm)} km)`).join(', ')}.
            </>
          )}
        </p>
      )}
    </div>
  )
}

const TEN_MIN = 10 * 60_000

/**
 * Basin map in SVG (d3-geo, no tiles, never captures the page scroll):
 * BD TOPAGE basin, rivers coloured by the percentile of their station,
 * Vigicrues reaches, stations, indicator markers, the mill, an optional
 * OPERA radar frame.
 */
export default function BasinMap({
  stations,
  now,
  at,
}: {
  stations: readonly StationData[]
  now: Date
  /** Past date (TimeMachine, ISO): percentiles at that date, no live vigilance nor radar */
  at?: string
}) {
  const [ref, width] = useElementWidth<HTMLDivElement>()
  const [radarToggle, setRadarOn] = useState(false)
  const live = !at
  const radarOn = live && radarToggle
  const basin = useApiResource<BasinGeoJson>('geo/basin', getBasinGeoJson)
  const vigilance = useApiResource<VigilanceStatus>(live ? 'vigilance' : null, getVigilance, TEN_MIN)
  const analogs = useApiResource<RiskAnalogsData>(`risk/analogs@${at ?? 'now'}`, () => getRiskAnalogs(at), TEN_MIN)
  const radar = useApiResource<RadarFrameMeta>(radarOn ? 'radar/frame' : null, () => getRadarFrameMeta(), 5 * 60_000)

  const model = useMemo(
    () =>
      buildBasinMap({
        width,
        basin: basin.data,
        stations,
        percentiles: mapPercentiles(analogs.data),
        troncons: live ? (vigilance.data?.vigicrues?.troncons ?? []) : [],
        radar: radarOn ? radar.data : null,
      }),
    [width, basin.data, stations, analogs.data, vigilance.data, radar.data, live, radarOn],
  )

  return (
    <div className="rounded-card border border-line bg-surface p-2">
      <div className="flex flex-wrap items-center justify-between gap-2 px-1 pb-2">
        <p className="text-[12.5px] text-ink-3">
          {basin.error
            ? `Contour du bassin indisponible : ${basin.error}`
            : basin.data?.features?.[0]?.properties?.attribution
              ? `Bassin : ${basin.data.features[0].properties.attribution} · rivières : BD TOPAGE simplifiée · Vigicrues`
              : 'Chargement du bassin…'}
        </p>
        <button
          type="button"
          aria-pressed={radarOn}
          disabled={!live}
          title={live ? undefined : 'Radar et vigilance en direct uniquement'}
          onClick={() => setRadarOn((v) => !v)}
          className={`min-h-[44px] rounded-full border px-3 text-[13px] sm:min-h-[32px] ${
            radarOn ? 'border-ink font-bold text-ink' : 'border-line-2 text-ink-2'
          } disabled:opacity-50`}
        >
          Radar pluie
        </button>
      </div>
      <div ref={ref} className="min-w-0">
        <BasinMapView
          model={model}
          radarOn={radarOn}
          radarMeta={radar.data}
          radarError={radar.error}
          percentileBasis={analogs.data?.percentile_basis?.pessac ?? null}
          now={now}
        />
      </div>
    </div>
  )
}
