// Pure geometry of the basin map (BasinMap): projection fit, radar frame
// placement, scale bar, percentile classes.
//
// Projection: equirectangular with a standard parallel at the middle of the
// extent (x = lon x cos(lat0), y = lat). Over 0.3 degree of latitude the
// distortion is negligible, and meridians and parallels stay straight and
// axis-aligned: the OPERA radar frame (a regular lon/lat grid, north up)
// is then placed exactly by its bbox alone.
import { geoDistance, geoPath, geoProjection, type GeoProjection } from 'd3-geo'

export type LonLat = [number, number]

export interface Bbox {
  west: number
  south: number
  east: number
  north: number
}

/** Mean Earth radius (km), as d3-geo distances are in radians */
export const EARTH_RADIUS_KM = 6371.0088

/** Bounding box of a set of points (null when empty). */
export function bboxOf(points: readonly LonLat[]): Bbox | null {
  if (points.length === 0) return null
  let west = Infinity
  let south = Infinity
  let east = -Infinity
  let north = -Infinity
  for (const [lon, lat] of points) {
    if (!Number.isFinite(lon) || !Number.isFinite(lat)) continue
    west = Math.min(west, lon)
    east = Math.max(east, lon)
    south = Math.min(south, lat)
    north = Math.max(north, lat)
  }
  return Number.isFinite(west) ? { west, south, east, north } : null
}

/** Bbox grown by `km` on every side. */
export function padBbox(b: Bbox, km: number): Bbox {
  const latMid = ((b.south + b.north) / 2) * (Math.PI / 180)
  const dLat = km / 110.574
  const dLon = km / (111.32 * Math.cos(latMid))
  return { west: b.west - dLon, south: b.south - dLat, east: b.east + dLon, north: b.north + dLat }
}

/** API order [west, south, east, north] to a Bbox. */
export function bboxFromArray(a: readonly number[] | null | undefined): Bbox | null {
  if (!a || a.length !== 4 || a.some((v) => !Number.isFinite(v))) return null
  const [west, south, east, north] = a
  return west < east && south < north ? { west, south, east, north } : null
}

export function containsPoint(b: Bbox, [lon, lat]: LonLat): boolean {
  return lon >= b.west && lon <= b.east && lat >= b.south && lat <= b.north
}

function corners(b: Bbox): GeoJSON.MultiPoint {
  return {
    type: 'MultiPoint',
    coordinates: [
      [b.west, b.north],
      [b.east, b.north],
      [b.east, b.south],
      [b.west, b.south],
    ],
  }
}

/** Equirectangular projection with standard parallel `lat0Deg`. */
export function standardParallelProjection(lat0Deg: number): GeoProjection {
  const k = Math.cos((lat0Deg * Math.PI) / 180)
  const raw = (lambda: number, phi: number): [number, number] => [lambda * k, phi]
  raw.invert = (x: number, y: number): [number, number] => [x / k, y]
  return geoProjection(raw)
}

export interface FittedMap {
  projection: GeoProjection
  width: number
  height: number
  extent: Bbox
}

/**
 * Projection that maps `extent` onto [0, width] x [0, height]: the north-west
 * corner at (0, 0), the south-east one at (width, height). The height
 * follows from the extent aspect ratio.
 */
export function fitMap(extent: Bbox, width: number): FittedMap {
  const projection = standardParallelProjection((extent.south + extent.north) / 2)
  const target = corners(extent)
  projection.fitWidth(Math.max(1, width), target)
  const [[, y0], [, y1]] = geoPath(projection).bounds(target)
  return { projection, width, height: Math.max(1, Math.round(y1 - y0)), extent }
}

/** SVG path data of a list of polylines (lon/lat), "" when nothing to draw. */
export function linesPath(projection: GeoProjection, lines: readonly (readonly LonLat[])[]): string {
  const path = geoPath(projection)
  return path({ type: 'MultiLineString', coordinates: lines as LonLat[][] }) ?? ''
}

/** SVG path data of a polygon given as GeoJSON coordinates (rings). */
export function polygonPath(projection: GeoProjection, rings: readonly (readonly LonLat[])[]): string {
  // d3-geo is spherical: a ring must be clockwise to enclose the small area.
  // Draw the rings as closed lines instead, so the winding never matters.
  const path = geoPath(projection)
  const d = path({ type: 'MultiLineString', coordinates: rings as LonLat[][] }) ?? ''
  return d ? d.replace(/(M[^M]*)/g, (m) => (m.endsWith('Z') ? m : `${m}Z`)) : ''
}

export interface ScreenRect {
  x: number
  y: number
  width: number
  height: number
}

/**
 * Screen rectangle of an image on a regular lon/lat grid, north up
 * (bbox = west, south, east, north). Exact with this projection.
 */
export function imagePlacement(projection: GeoProjection, b: Bbox): ScreenRect | null {
  const nw = projection([b.west, b.north])
  const se = projection([b.east, b.south])
  if (!nw || !se) return null
  return { x: nw[0], y: nw[1], width: se[0] - nw[0], height: se[1] - nw[1] }
}

/** Kilometres per pixel along the parallel through the map centre. */
export function kmPerPixel(projection: GeoProjection, map: { width: number; height: number }): number {
  const y = map.height / 2
  const a = projection.invert?.([map.width / 2 - 50, y])
  const b = projection.invert?.([map.width / 2 + 50, y])
  if (!a || !b) return NaN
  return (geoDistance(a, b) * EARTH_RADIUS_KM) / 100
}

const SCALE_STEPS_KM = [1, 2, 5, 10, 20, 50, 100]

/** Longest round length (km) whose bar fits in `maxFraction` of the width. */
export function scaleBar(
  projection: GeoProjection,
  map: { width: number; height: number },
  maxFraction = 0.25,
): { km: number; px: number } | null {
  const kpp = kmPerPixel(projection, map)
  if (!Number.isFinite(kpp) || kpp <= 0) return null
  const maxKm = map.width * maxFraction * kpp
  const km = [...SCALE_STEPS_KM].reverse().find((s) => s <= maxKm) ?? SCALE_STEPS_KM[0]
  return { km, px: km / kpp }
}

/**
 * Class 1-5 of the sequential ramp (--seq-1..5) for a percentile:
 * < p50, p50-75, p75-90, p90-99, >= p99. null when unknown.
 */
export function percentileClass(p: number | null | undefined): 1 | 2 | 3 | 4 | 5 | null {
  if (p == null || !Number.isFinite(p)) return null
  if (p < 50) return 1
  if (p < 75) return 2
  if (p < 90) return 3
  if (p < 99) return 4
  return 5
}

export const PERCENTILE_CLASS_LABELS: Record<1 | 2 | 3 | 4 | 5, string> = {
  1: '< p50',
  2: 'p50-75',
  3: 'p75-90',
  4: 'p90-99',
  5: '≥ p99',
}

/** Compass direction (8 points, French) from `from` to `to`. */
export function compassFrom(from: LonLat, to: LonLat): string {
  const k = Math.cos((from[1] * Math.PI) / 180)
  const dx = (to[0] - from[0]) * k
  const dy = to[1] - from[1]
  const deg = (Math.atan2(dx, dy) * 180) / Math.PI
  const names = ['N', 'NE', 'E', 'SE', 'S', 'SO', 'O', 'NO']
  return names[(Math.round(((deg + 360) % 360) / 45) % 8)]
}
