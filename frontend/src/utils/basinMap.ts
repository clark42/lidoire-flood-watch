// Model of the basin map (BasinMap): what to draw where, from the API
// payloads. Pure: the component only turns this model into SVG.
import type {
  BasinGeoJson,
  RadarFrameMeta,
  RiskAnalogsData,
  StationData,
  VigilanceLevel,
  VigilanceTroncon,
} from '../types'
import basinLines from '../geo/basinLines.json'
import {
  bboxFromArray,
  bboxOf,
  compassFrom,
  containsPoint,
  fitMap,
  imagePlacement,
  linesPath,
  padBbox,
  percentileClass,
  polygonPath,
  scaleBar,
  type Bbox,
  type LonLat,
  type ScreenRect,
} from './mapGeo'

/** Mill of the Lidoire (config.py location) */
export const MILL: LonLat = [0.0015091, 44.8759811]

/** Stations closer than this to the mill are framed; the others are listed */
export const FRAMED_STATION_KM = 25

/** Percentile key of the Eyraud analog (runoff indicator) */
export const EYRAUD_KEY = 'flow:EYRAUD'

// Indicator stations: their coordinates are in config.py
// (indicator_flow_stations, indicator_groundwater_stations) but /api/indicators
// does not serve them yet.
export const EXTRA_MARKERS = [
  {
    id: 'eyraud',
    label: 'Eyraud (analogue)',
    lonlat: [0.3624, 44.8869] as LonLat,
    shape: 'square' as const,
    title: "Eyraud à La Force : débit d'un bassin voisin semblable à la Lidoire (analogue, mesuré ailleurs)",
  },
  {
    id: 'mouliets',
    label: 'Nappe Mouliets',
    lonlat: [-0.0446, 44.8344] as LonLat,
    shape: 'triangle' as const,
    title: 'Piézomètre de Mouliets-et-Villemartin : nappe alluviale de la Dordogne (BRGM)',
  },
]

type RiverKey = 'dordogne' | 'isle' | 'dronne' | 'eyraud' | 'lechout' | 'lidoire'

/** Station (or indicator) whose percentile colours each river; null = never measured */
const RIVERS: { key: RiverKey; name: string; source: string | null; width: number }[] = [
  { key: 'dordogne', name: 'Dordogne', source: 'P555001001', width: 3.2 },
  { key: 'isle', name: 'Isle', source: 'P726151001', width: 2.4 },
  { key: 'dronne', name: 'Dronne', source: 'P846251001', width: 2 },
  { key: 'eyraud', name: 'Eyraud', source: EYRAUD_KEY, width: 1.2 },
  { key: 'lechout', name: 'Léchout', source: null, width: 1.2 },
]

/** Label anchors of the rivers (lon/lat), drawn only when inside the frame */
const RIVER_LABELS: { text: string; at: LonLat; strong?: boolean }[] = [
  { text: 'Lidoire', at: [0.13, 44.962], strong: true },
  { text: 'Dordogne', at: [0.19, 44.836] },
  { text: 'Isle', at: [-0.07, 45.03] },
  { text: 'Dronne', at: [-0.085, 45.09] },
  { text: 'Eyraud', at: [0.375, 44.95] },
]

interface Lines {
  rivers: Record<string, LonLat[][]>
  troncons: Record<string, LonLat[][]>
}
const LINES = basinLines as unknown as Lines

export interface MapRiver {
  key: string
  name: string
  d: string
  width: number
  /** 1-5 class of the sequential ramp, null = no percentile */
  seq: 1 | 2 | 3 | 4 | 5 | null
  percentile: number | null
  measured: boolean
}

export interface MapStation {
  id: string
  name: string
  short: string
  x: number
  y: number
  r: number
  tide: boolean
  seq: 1 | 2 | 3 | 4 | 5 | null
  percentile: number | null
  title: string
  labelLeft: boolean
  /** Text next to the marker (shortened on a phone) */
  label: string
}

export interface MapMarker {
  id: string
  label: string
  shape: 'square' | 'triangle'
  x: number
  y: number
  title: string
  labelLeft: boolean
}

export interface MapTroncon {
  code: string
  name: string
  level: VigilanceLevel | null
  d: string
}

export interface OffMapStation {
  name: string
  direction: string
  distanceKm: number
}

export interface MapRadar {
  rect: ScreenRect
  url: string
}

export interface BasinMapModel {
  width: number
  height: number
  basinD: string | null
  basinLabel: { x: number; y: number; text: string } | null
  lidoireD: string
  rivers: MapRiver[]
  troncons: MapTroncon[]
  stations: MapStation[]
  offMap: OffMapStation[]
  markers: MapMarker[]
  mill: { x: number; y: number }
  labels: { text: string; x: number; y: number; strong: boolean }[]
  scale: { km: number; px: number } | null
  radar: MapRadar | null
  extent: Bbox
}

export interface BasinMapInputs {
  width: number
  basin: BasinGeoJson | null
  stations: readonly StationData[]
  /** Percentile (0-100) by station id or EYRAUD_KEY */
  percentiles: Readonly<Record<string, number | null | undefined>>
  troncons: readonly Pick<VigilanceTroncon, 'code' | 'name' | 'level'>[]
  radar?: Pick<RadarFrameMeta, 'available' | 'bbox' | 'url'> | null
}

function stationPoint(s: StationData): LonLat | null {
  return s.lat != null && s.lon != null ? [s.lon, s.lat] : null
}

function basinRings(basin: BasinGeoJson | null): LonLat[][] {
  const geom = basin?.features?.[0]?.geometry
  if (!geom || geom.type !== 'Polygon') return []
  return geom.coordinates as LonLat[][]
}

/** Margin of the radar frame around the basin (config radar_frame_margin_km) */
export const RADAR_MARGIN_KM = 10

/**
 * Framed area: the basin plus RADAR_MARGIN_KM (the radar frame emprise, so
 * that the overlay is never cut), the mill, the indicator markers and the
 * stations within FRAMED_STATION_KM of the mill, plus 3 km.
 */
export function mapExtent(basin: BasinGeoJson | null, stations: readonly StationData[]): Bbox {
  const pts: LonLat[] = [MILL, ...EXTRA_MARKERS.map((m) => m.lonlat)]
  const basinBox = bboxOf(basinRings(basin).flat())
  if (basinBox) {
    const b = padBbox(basinBox, RADAR_MARGIN_KM)
    pts.push([b.west, b.south], [b.east, b.north])
  }
  for (const s of stations) {
    const p = stationPoint(s)
    if (p && s.distance_km <= FRAMED_STATION_KM) pts.push(p)
  }
  return padBbox(bboxOf(pts)!, 3)
}

function shortName(name: string): string {
  return name.replace(/-sur-Dordogne$/, '')
}

function stationTitle(s: StationData, percentile: number | null): string {
  const parts = [
    `${s.station_name} (${s.river})`,
    `${s.current_level.height_m.toFixed(2).replace('.', ',')} m, ${s.tendency}`,
    percentile != null ? `percentile ${Math.round(percentile)}` : 'percentile non servi',
  ]
  if (s.weight === 0) parts.push('marée : hors score')
  else parts.push(`poids ${Math.round(s.weight * 100)} %, délai ${s.delay_hours} h`)
  return parts.join(' · ')
}

export function buildBasinMap(inputs: BasinMapInputs): BasinMapModel {
  const extent = mapExtent(inputs.basin, inputs.stations)
  const { projection, width, height } = fitMap(extent, inputs.width)
  const at = (p: LonLat) => projection(p) ?? [NaN, NaN]
  const labelLeftOf = (x: number) => x > width - 90

  const rings = basinRings(inputs.basin)
  const basinD = rings.length ? polygonPath(projection, rings) : null
  const area = inputs.basin?.features?.[0]?.properties?.area_km2

  const rivers: MapRiver[] = RIVERS.filter((r) => LINES.rivers[r.key]?.length).map((r) => {
    const percentile = r.source ? (inputs.percentiles[r.source] ?? null) : null
    return {
      key: r.key,
      name: r.name,
      d: linesPath(projection, LINES.rivers[r.key]),
      width: r.width,
      seq: percentileClass(percentile),
      percentile,
      measured: r.source !== null,
    }
  })

  const levels = new Map(inputs.troncons.map((t) => [t.code, t]))
  const troncons: MapTroncon[] = Object.entries(LINES.troncons)
    .filter(([code]) => levels.has(code))
    .map(([code, lines]) => ({
      code,
      name: levels.get(code)!.name,
      level: levels.get(code)!.level,
      d: linesPath(projection, lines),
    }))

  const stations: MapStation[] = []
  const offMap: OffMapStation[] = []
  for (const s of inputs.stations) {
    const p = stationPoint(s)
    if (!p) continue
    if (!containsPoint(extent, p)) {
      offMap.push({ name: shortName(s.station_name), direction: compassFrom(MILL, p), distanceKm: s.distance_km })
      continue
    }
    const [x, y] = at(p)
    const percentile = inputs.percentiles[s.station_id] ?? null
    const tide = s.weight === 0
    stations.push({
      id: s.station_id,
      name: s.station_name,
      short: shortName(s.station_name),
      x,
      y,
      r: tide ? 5 : 4 + s.weight * 14,
      tide,
      seq: tide ? null : percentileClass(percentile),
      percentile,
      title: stationTitle(s, percentile),
      labelLeft: labelLeftOf(x),
      // "(marée)" only when there is room; the legend explains the dashed ring
      label: tide && width >= 480 ? `${shortName(s.station_name)} (marée)` : shortName(s.station_name),
    })
  }
  offMap.sort((a, b) => a.distanceKm - b.distanceKm)

  const markers: MapMarker[] = EXTRA_MARKERS.map((m) => {
    const [x, y] = at(m.lonlat)
    // Left of the marker near the right edge (the label would be cut)
    const labelLeft = m.shape === 'triangle' || x > width - 140
    return { id: m.id, label: m.label, shape: m.shape, x, y, title: m.title, labelLeft }
  })

  const labels = RIVER_LABELS.filter((l) => containsPoint(extent, l.at)).map((l) => {
    const [x, y] = at(l.at)
    return { text: l.text, x, y, strong: !!l.strong }
  })

  // Area under the "Lidoire" label, inside the basin
  let basinLabel: BasinMapModel['basinLabel'] = null
  const lidoireLabel = labels.find((l) => l.text === 'Lidoire')
  if (basinD && area && lidoireLabel) {
    basinLabel = { x: lidoireLabel.x, y: lidoireLabel.y + 13, text: `bassin ${Math.round(area)} km²` }
  }

  let radar: MapRadar | null = null
  const radarBbox = inputs.radar?.available ? bboxFromArray(inputs.radar.bbox) : null
  if (radarBbox && inputs.radar?.url) {
    const rect = imagePlacement(projection, radarBbox)
    if (rect) radar = { rect, url: inputs.radar.url }
  }

  const [mx, my] = at(MILL)
  return {
    width,
    height,
    basinD,
    basinLabel,
    lidoireD: LINES.rivers.lidoire ? linesPath(projection, LINES.rivers.lidoire) : '',
    rivers,
    troncons,
    stations,
    offMap,
    markers,
    mill: { x: mx, y: my },
    labels,
    scale: scaleBar(projection, { width, height }),
    radar,
    extent,
  }
}

/**
 * Percentile of each coloured river and station. Only /api/risk/analogs
 * serves level percentiles today (Pessac, Eyraud): the other stations stay
 * neutral ("percentile non servi").
 */
export function mapPercentiles(analogs: RiskAnalogsData | null): Record<string, number | null> {
  const p = analogs?.situation?.percentiles
  return { P555001001: p?.pessac ?? null, [EYRAUD_KEY]: p?.eyraud_q ?? null }
}
