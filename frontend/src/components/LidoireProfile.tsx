import { useId } from 'react'
import type { LidoireProfileData } from '../types'
import { formatNumber } from '../utils/format'
import {
  backwaterPolygon,
  findSteps,
  groundAt,
  pathOf,
  profileLayout,
  type Reach,
  type ReachVsMill,
} from '../utils/lidoireProfile'
import ValueTag from './ui/ValueTag'

export interface LidoireProfileProps {
  profile: LidoireProfileData
  /** Stage of the Dordogne at the confluence (m NGF, estimated) */
  zc: number | null
  reach: Reach & ReachVsMill
  /** Tested floor altitude of the mill (m NGF), null = unknown */
  floorZ: number | null
  width: number
}

/** Surface-coloured outline behind a label drawn over hatching or lines */
const HALO = { stroke: 'var(--surface)', strokeWidth: 3, strokeLinejoin: 'round', paintOrder: 'stroke' } as const

function km(m: number | null | undefined, digits = 1): string {
  return m == null ? '–' : formatNumber(m / 1000, digits)
}

/**
 * Long profile of the Lidoire, confluence (left) to ~1 km above the mill
 * (right), in m NGF: ground line, probable weirs, Dordogne stage Z_c at the
 * confluence (estimated) and the flat-water backwater it would push up the
 * Lidoire (approximation). The Lidoire's own level at the mill is not
 * measured: the chart says so instead of drawing a value.
 */
export default function LidoireProfile({ profile, zc, reach, floorZ, width }: LidoireProfileProps) {
  const uid = useId().replace(/:/g, '')
  const points = profile.profile.points ?? []
  const mill = profile.mill
  const millGround = mill.ground_z_m_ngf
  const L = profileLayout(width, points, { zc, floorZ, millGround })
  const { x, y, margin, height, narrow } = L
  const plotTop = margin.top
  const plotBottom = height - margin.bottom
  const plotLeft = margin.left
  const plotRight = width - margin.right
  const [zLo, zHi] = y.domain()

  const ground = points.map((p) => [p.d_m, p.z_m] as [number, number])
  const groundPath = pathOf(ground, L)
  const soilPath = ground.length
    ? `${groundPath}L${x(ground[ground.length - 1][0]).toFixed(1)},${plotBottom}L${x(ground[0][0]).toFixed(1)},${plotBottom}Z`
    : ''
  const steps = findSteps(points)
  const toD = reach.connected_distance_m
  const water = backwaterPolygon(points, zc, toD)
  const millX = x(mill.d_m)
  const frontX = toD != null ? x(toD) : null
  const upperX = reach.disconnected_low_points && reach.distance_m != null ? x(reach.distance_m) : null
  const gapM = reach.distanceBelowMillM
  const zTicks: number[] = []
  for (let v = Math.ceil(zLo / 2) * 2; v <= zHi; v += 2) zTicks.push(v)
  const kmTicks: number[] = []
  for (let d = 0; d <= x.domain()[1]; d += 1000) kmTicks.push(d)
  const millGroundY = y(millGround)
  const bracketY = plotTop + 6

  const label =
    `Profil en long de la Lidoire, de la confluence au moulin, en mètres NGF. ` +
    (zc != null
      ? `Dordogne à la confluence ≈ ${formatNumber(zc, 2)} m NGF (estimé) ; remous jusqu'à ${km(toD)} km de la confluence` +
        (gapM != null && gapM > 0 ? `, ${km(gapM)} km sous le moulin.` : '.')
      : 'Niveau de la Dordogne à la confluence indisponible.')

  return (
    <svg width={width} height={height} role="img" aria-label={label} className="block max-w-full select-none">
      <defs>
        <pattern id={`soil-${uid}`} width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <line x1="0" y1="0" x2="0" y2="6" stroke="var(--line-2)" strokeWidth="1.2" />
        </pattern>
        <pattern id={`water-${uid}`} width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(-45)">
          <rect width="5" height="5" fill="var(--water-soft)" />
          <line x1="0" y1="0" x2="0" y2="5" stroke="var(--water)" strokeWidth="1.3" />
        </pattern>
      </defs>

      {/* Grid and axes */}
      {zTicks.map((v) => (
        <g key={`z${v}`}>
          <line x1={plotLeft} x2={plotRight} y1={y(v)} y2={y(v)} stroke="var(--line)" strokeWidth="1" />
          <text x={plotLeft - 5} y={y(v) + 3.5} textAnchor="end" className="fill-ink-3 font-mono text-[10.5px]">
            {v}
          </text>
        </g>
      ))}
      <text x={2} y={plotTop - 12} className="fill-ink-3 font-mono text-[10.5px]">
        m NGF
      </text>
      {kmTicks.map((d) => (
        <g key={`d${d}`}>
          <line x1={x(d)} x2={x(d)} y1={plotBottom} y2={plotBottom + 4} stroke="var(--line-2)" />
          <text x={x(d)} y={plotBottom + 15} textAnchor={d === 0 ? 'start' : 'middle'} className="fill-ink-3 font-mono text-[10.5px]">
            {narrow && d + 1000 <= x.domain()[1] ? d / 1000 : `${d / 1000} km`}
          </text>
        </g>
      ))}
      <text x={plotLeft} y={plotBottom + 30} className="fill-ink-2 text-[11.5px]">
        {narrow ? 'Confluence' : 'Confluence (Dordogne)'}
      </text>
      <text x={plotRight} y={plotBottom + 30} textAnchor="end" className="fill-ink-3 text-[11px]">
        {narrow ? 'amont →' : 'distance le long de la Lidoire, vers l’amont →'}
      </text>

      {/* Ground (low point of the cross-section, IGN RGE ALTI) */}
      <path d={soilPath} fill={`url(#soil-${uid})`} />
      <path d={groundPath} fill="none" stroke="var(--ink-2)" strokeWidth="1.6" strokeLinejoin="round" />

      {/* Probable weirs: abrupt rises of the ground line */}
      {steps.map((s) => {
        const sx = x(s.toD)
        const sy = y(s.toZ)
        return (
          <g key={s.toD} data-step={s.toD}>
            <title>{`Marche de ${formatNumber(s.rise, 1)} m entre ${km(s.fromD, 2)} et ${km(s.toD, 2)} km : seuil ou ouvrage probable, à vérifier sur place`}</title>
            <line x1={sx} x2={sx} y1={sy - 4} y2={sy - 16} stroke="var(--ink-3)" strokeWidth="1" />
            <text x={sx} y={sy - 20} textAnchor="middle" className="fill-ink-2 text-[10.5px]" {...HALO}>
              seuil ?
            </text>
          </g>
        )
      })}

      {/* Flat-water backwater from the confluence (approximation) */}
      {water.length > 0 && (
        <g data-backwater="">
          <path d={pathOf(water, L, true)} fill={`url(#water-${uid})`} opacity="0.9" />
          <line
            x1={x(0)}
            x2={frontX ?? x(0)}
            y1={y(zc!)}
            y2={y(zc!)}
            stroke="var(--water)"
            strokeWidth="2.2"
            strokeDasharray="6 4"
          />
        </g>
      )}
      {upperX != null && zc != null && (
        <g>
          <title>Point bas isolé sous Z_c au-delà d’une marche : borne haute du remous, pas d’eau connectée</title>
          <line x1={upperX} x2={upperX} y1={y(zc) - 6} y2={y(zc) + 6} stroke="var(--water)" strokeWidth="1.5" strokeDasharray="2 2" />
          {!narrow && (
            <text x={upperX + 4} y={y(zc) - 8} className="fill-ink-3 font-mono text-[10px]" {...HALO}>
              borne haute
            </text>
          )}
        </g>
      )}
      {zc != null && zc >= zLo && (
        <g data-zc={zc}>
          <circle cx={x(0)} cy={y(zc)} r="5" fill="var(--surface)" stroke="var(--water)" strokeWidth="2.2" />
          <text x={x(0) + 9} y={y(zc) - 9} className="num fill-ink text-[14px] font-semibold" {...HALO}>
            ≈ {formatNumber(zc, 2)} m NGF
          </text>
          <text x={x(0) + 9} y={y(zc) - 26} className="fill-ink-2 font-mono text-[10px]" {...HALO}>
            {narrow ? 'Dordogne, estimé' : 'Dordogne à la confluence, estimé'}
          </text>
        </g>
      )}

      {/* Mill: ground 12.0 m NGF, floor unknown, Lidoire level not measured */}
      <line x1={millX} x2={millX} y1={plotTop + 14} y2={millGroundY} stroke="var(--ink)" strokeWidth="1.2" />
      <circle cx={millX} cy={millGroundY} r="4" fill="var(--ink)" stroke="var(--surface)" strokeWidth="1.5" />
      <text
        x={millX - 6}
        y={plotTop + 24}
        textAnchor="end"
        className="fill-ink text-[12px] font-semibold"
      >
        Moulin
      </text>
      <text x={millX - 6} y={millGroundY + 15} textAnchor="end" className="fill-ink-2 font-mono text-[10px]" {...HALO}>
        terrain {formatNumber(millGround, 1)}
      </text>
      {floorZ == null ? (
        <g data-floor="unknown">
          <rect
            x={millX + 6}
            y={millGroundY - 32}
            width={66}
            height={18}
            rx={9}
            fill="var(--surface)"
            stroke="var(--lv-red)"
            strokeDasharray="3 2"
          />
          <text x={millX + 39} y={millGroundY - 19} textAnchor="middle" className="fill-ink text-[11px]">
            plancher ?
          </text>
        </g>
      ) : (
        <g data-floor={floorZ}>
          <line
            x1={Math.max(plotLeft, millX - (narrow ? 40 : 70))}
            x2={Math.min(plotRight, millX + (narrow ? 40 : 70))}
            y1={y(floorZ)}
            y2={y(floorZ)}
            stroke="var(--lv-red)"
            strokeWidth="1.6"
            strokeDasharray="6 3"
          />
          <text
            x={narrow ? millX - 8 : millX + 6}
            y={y(floorZ) - 5}
            textAnchor={narrow ? 'end' : 'start'}
            className="fill-ink text-[11px]"
            {...HALO}
          >
            {narrow ? 'plancher ?' : 'plancher (hypothèse)'} {formatNumber(floorZ, 2)}
          </text>
        </g>
      )}
      <g data-lidoire-unmeasured="">
        <title>Le niveau de la Lidoire au moulin n’est pas mesuré : aucun capteur aujourd’hui (prévu).</title>
        <circle cx={millX} cy={millGroundY - 8} r="6.5" fill="none" stroke="var(--ink-3)" strokeDasharray="2 2" />
        <text x={millX} y={millGroundY - 4.5} textAnchor="middle" className="fill-ink-2 text-[9.5px] font-bold">
          ?
        </text>
      </g>

      {/* Distance between the end of the backwater and the mill */}
      {frontX != null && gapM != null && gapM > 0 && zc != null && (
        <g data-gap={gapM}>
          <line x1={frontX} x2={frontX} y1={y(zc)} y2={bracketY} stroke="var(--water)" strokeWidth="1" strokeDasharray="2 3" />
          <line x1={frontX} x2={millX} y1={bracketY} y2={bracketY} stroke="var(--ink-2)" strokeWidth="1.2" />
          <path d={`M${frontX + 5},${bracketY - 4}L${frontX},${bracketY}L${frontX + 5},${bracketY + 4}`} fill="none" stroke="var(--ink-2)" />
          <path d={`M${millX - 5},${bracketY - 4}L${millX},${bracketY}L${millX - 5},${bracketY + 4}`} fill="none" stroke="var(--ink-2)" />
          <text x={(frontX + millX) / 2} y={bracketY - 5} textAnchor="middle" className="num fill-ink text-[13px] font-semibold" {...HALO}>
            ≈ {km(gapM)} km sans remous
          </text>
        </g>
      )}
    </svg>
  )
}

/** Readout under the profile: values, their nature, and what is not known. */
export function LidoireProfileReadout({
  profile,
  zc,
  reach,
  floorZ,
}: Omit<LidoireProfileProps, 'width'>) {
  const millGround = profile.mill.ground_z_m_ngf
  const points = profile.profile.points ?? []
  const gap = reach.distanceBelowMillM
  const marginToGround = zc != null ? millGround - zc : null
  const floorGap = floorZ != null && zc != null ? floorZ - zc : null
  const groundAtFront = reach.connected_distance_m != null ? groundAt(points, reach.connected_distance_m) : null
  return (
    <div className="space-y-2 text-[14px] text-ink-2">
      <p className="text-ink">
        {zc == null ? (
          'Niveau de la Dordogne à la confluence indisponible (Pessac sans mesure).'
        ) : reach.reachesMill ? (
          <>
            Le plan d’eau de la Dordogne atteindrait le moulin <ValueTag kind="estimated">approximation</ValueTag>
          </>
        ) : (
          <>
            Le remous s’arrête à <b className="num text-[17px]">≈ {km(gap)} km</b> sous le moulin
            {reach.connected_distance_m != null && (
              <> (eau connectée jusqu’à {km(reach.connected_distance_m, 2)} km de la confluence
                {groundAtFront != null && <>, terrain {formatNumber(groundAtFront, 1)} m</>})</>
            )}
            . <ValueTag kind="estimated">approximation</ValueTag>
          </>
        )}
      </p>
      {zc != null && (
        <p>
          Dordogne à la confluence ≈ <span className="num text-ink">{formatNumber(zc, 2)} m NGF</span>{' '}
          <ValueTag kind="estimated" />, soit {formatNumber(marginToGround, 1)} m sous le terrain du moulin (
          {formatNumber(millGround, 1)} m NGF, RGE ALTI).
          {floorGap != null && (
            <>
              {' '}
              Plancher testé à {formatNumber(floorZ, 2)} m :{' '}
              {floorGap > 0
                ? `il manquerait ${formatNumber(floorGap, 2)} m au plan d’eau pour l’atteindre par le remous seul.`
                : `le plan d’eau serait ${formatNumber(-floorGap, 2)} m au-dessus, si l’eau est connectée jusqu’au moulin.`}
            </>
          )}
        </p>
      )}
      <p className="rounded-lg border border-dashed border-line-2 px-3 py-2 text-[13px]">
        <b className="text-ink">Le niveau de la Lidoire au moulin n’est pas mesuré.</b> Ce profil montre seulement
        jusqu’où remonte la Dordogne (plan d’eau horizontal depuis la confluence, sans le débit propre de la Lidoire ni la
        pente de la ligne d’eau). La crue propre de la Lidoire (ruissellement) se lit sur le plan des crues. Un capteur de
        niveau au moulin est prévu ; un seul hiver de relevés suffira à caler ce profil.
      </p>
    </div>
  )
}
