// Lidoire long profile (pure): confluence stage Z_c, backwater reach on the
// profile, steps of the ground line, and the geometry of the profile chart.
//
// The stage and reach functions mirror backend/app/services/lidoire_profile.py
// (confluence_stage, backwater_reach, reach_relative_to_mill) so that the
// replay can animate every hour from the timeline series without one API
// call per frame; the API value replaces the local one once fetched.
// lidoireProfile.test.ts checks both agree on recorded payloads.

import { scaleLinear, type ScaleLinear } from 'd3-scale'
import type { BackwaterReach, LidoireProfileData, LidoireProfilePoint } from '../types'

export type ProfilePoint = Pick<LidoireProfilePoint, 'd_m' | 'z_m'>

export interface ConfluenceParams {
  /** NGF zero of the upstream (Pessac) and downstream (Libourne) stations, m */
  upstreamZero: number
  downstreamZero: number
  /** Weight of the downstream station (alpha) */
  alpha: number
  /** Envelope window (max over (t - hours, t]) */
  envelopeHours: number
}

/** Values of backend/app/config.py, used until /api/geo/profile answers. */
export const DEFAULT_CONFLUENCE_PARAMS: ConfluenceParams = {
  upstreamZero: 3.33,
  downstreamZero: -1.17,
  alpha: 0.2,
  envelopeHours: 12.5,
}

export function confluenceParamsFrom(profile: LidoireProfileData | null | undefined): ConfluenceParams {
  const c = profile?.confluence
  if (!c) return DEFAULT_CONFLUENCE_PARAMS
  const up = c.ngf_zeros_m?.[c.upstream_station]
  const down = c.ngf_zeros_m?.[c.downstream_station]
  return {
    upstreamZero: up ?? DEFAULT_CONFLUENCE_PARAMS.upstreamZero,
    downstreamZero: down ?? DEFAULT_CONFLUENCE_PARAMS.downstreamZero,
    alpha: c.alpha_downstream_weight ?? DEFAULT_CONFLUENCE_PARAMS.alpha,
    envelopeHours: c.envelope_hours ?? DEFAULT_CONFLUENCE_PARAMS.envelopeHours,
  }
}

function round(value: number, digits: number): number {
  const f = 10 ** digits
  return Math.round(value * f) / f
}

/**
 * Z_c (m NGF, ESTIMATED) from the Pessac and Libourne heights (station datum).
 * Libourne missing: Pessac alone. Pessac missing: null (Libourne alone is tidal).
 */
export function confluenceStage(
  pessacH: number | null | undefined,
  libourneH: number | null | undefined,
  params: ConfluenceParams = DEFAULT_CONFLUENCE_PARAMS,
): number | null {
  if (pessacH == null) return null
  const zPessac = params.upstreamZero + pessacH
  if (libourneH == null) return round(zPessac, 3)
  return round((1 - params.alpha) * zPessac + params.alpha * (params.downstreamZero + libourneH), 3)
}

export type Reach = Pick<
  BackwaterReach,
  'status' | 'distance_m' | 'connected_distance_m' | 'disconnected_low_points' | 'z_m'
>

/** Ground crossing of Z_c just upstream of sample i (linear interpolation). */
function crossing(pts: ProfilePoint[], i: number, zc: number): number {
  const p = pts[i]
  if (i === pts.length - 1) return round(p.d_m, 1)
  const q = pts[i + 1]
  const dz = q.z_m - p.z_m
  const u = dz <= 0 ? 0 : Math.max(0, Math.min(1, (zc - p.z_m) / dz))
  return round(p.d_m + u * (q.d_m - p.d_m), 1)
}

/**
 * Flat-water backwater reach (APPROXIMATION) of a stage Z_c on the profile
 * (points sorted by d_m, 0 = confluence): `distance_m` = furthest point with
 * ground <= Z_c (upper bound, may jump a weir), `connected_distance_m` =
 * stops at the first sample above Z_c walking up from the confluence.
 */
export function backwaterReach(points: readonly ProfilePoint[], zc: number | null | undefined): Reach {
  const pts = points.filter((p) => p.z_m != null)
  const unknown: Reach = {
    status: 'unknown',
    distance_m: null,
    connected_distance_m: null,
    disconnected_low_points: null,
    z_m: null,
  }
  if (zc == null || pts.length === 0) return unknown
  let last = -1
  pts.forEach((p, i) => {
    if (p.z_m <= zc) last = i
  })
  if (last < 0) {
    return { status: 'none', distance_m: 0, connected_distance_m: 0, disconnected_low_points: false, z_m: null }
  }
  let j = pts[0].z_m <= zc ? 0 : -1
  while (j >= 0 && j + 1 < pts.length && pts[j + 1].z_m <= zc) j += 1
  const connected = j < 0 ? 0 : crossing(pts, j, zc)
  const distance = crossing(pts, last, zc)
  return {
    status: last === pts.length - 1 ? 'whole_profile' : 'partial',
    distance_m: distance,
    connected_distance_m: connected,
    disconnected_low_points: distance > connected + 1e-6,
    z_m: last === pts.length - 1 ? pts[last].z_m : round(zc, 3),
  }
}

export interface ReachVsMill {
  /** Distance between the furthest backwater point and the mill (> 0: below the mill) */
  distanceBelowMillM: number | null
  connectedDistanceBelowMillM: number | null
  reachesMill: boolean | null
}

export function reachRelativeToMill(reach: Reach, millD: number): ReachVsMill {
  const d = reach.distance_m
  const c = reach.connected_distance_m
  return {
    distanceBelowMillM: d == null ? null : round(millD - d, 1),
    connectedDistanceBelowMillM: c == null ? null : round(millD - c, 1),
    reachesMill: d == null ? null : d >= millD,
  }
}

export interface ProfileStep {
  /** Downstream sample of the rise */
  fromD: number
  toD: number
  fromZ: number
  toZ: number
  rise: number
}

/**
 * Abrupt rises of the ground line (>= minRise metres between two consecutive
 * samples): probable weirs or structures, to be checked on site.
 */
export function findSteps(points: readonly ProfilePoint[], minRise = 2): ProfileStep[] {
  const steps: ProfileStep[] = []
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1]
    const b = points[i]
    const rise = b.z_m - a.z_m
    if (rise >= minRise) {
      steps.push({ fromD: a.d_m, toD: b.d_m, fromZ: a.z_m, toZ: b.z_m, rise: round(rise, 2) })
    }
  }
  return steps
}

/** Ground z at distance d (linear interpolation, clamped to the ends). */
export function groundAt(points: readonly ProfilePoint[], d: number): number | null {
  if (points.length === 0) return null
  if (d <= points[0].d_m) return points[0].z_m
  for (let i = 1; i < points.length; i++) {
    const b = points[i]
    if (d <= b.d_m) {
      const a = points[i - 1]
      const span = b.d_m - a.d_m
      return span === 0 ? b.z_m : a.z_m + ((d - a.d_m) / span) * (b.z_m - a.z_m)
    }
  }
  return points[points.length - 1].z_m
}

/**
 * Outline of the backwater water body: the flat surface at Z_c from the
 * confluence to `toD`, then back along the ground. Empty when no water.
 * Points are [d_m, z_m]; every ground point inside is <= Z_c by construction
 * of the connected reach.
 */
export function backwaterPolygon(
  points: readonly ProfilePoint[],
  zc: number | null | undefined,
  toD: number | null | undefined,
): [number, number][] {
  if (zc == null || toD == null || toD <= 0 || points.length === 0) return []
  const start = Math.max(0, points[0].d_m)
  const ground: [number, number][] = [[toD, Math.min(zc, groundAt(points, toD) ?? zc)]]
  for (let i = points.length - 1; i >= 0; i--) {
    const p = points[i]
    if (p.d_m < toD && p.d_m >= start) ground.push([p.d_m, Math.min(zc, p.z_m)])
  }
  if (ground[ground.length - 1][0] > start) ground.push([start, Math.min(zc, points[0].z_m)])
  return [[start, zc], [toD, zc], ...ground]
}

export interface ProfileLayout {
  width: number
  height: number
  narrow: boolean
  margin: { top: number; right: number; bottom: number; left: number }
  x: ScaleLinear<number, number>
  y: ScaleLinear<number, number>
}

/**
 * Scales of the profile chart: distance from the confluence (m) left to
 * right, m NGF bottom to top. The vertical domain covers the ground, the
 * stage and the tested floor, in whole metres.
 */
export function profileLayout(
  width: number,
  points: readonly ProfilePoint[],
  { zc = null, floorZ = null, millGround = 12 }: { zc?: number | null; floorZ?: number | null; millGround?: number } = {},
): ProfileLayout {
  const narrow = width < 560
  const height = narrow ? 300 : 340
  const margin = narrow ? { top: 26, right: 12, bottom: 40, left: 34 } : { top: 28, right: 20, bottom: 42, left: 46 }
  const length = points.length ? points[points.length - 1].d_m : 6800
  const zs = points.map((p) => p.z_m)
  const lo = Math.floor(Math.min(...zs, zc ?? Infinity, 2) - 1)
  const hi = Math.ceil(Math.max(...zs, zc ?? -Infinity, floorZ ?? -Infinity, millGround) + 1.5)
  return {
    width,
    height,
    narrow,
    margin,
    x: scaleLinear().domain([0, length]).range([margin.left, width - margin.right]),
    y: scaleLinear().domain([lo, hi]).range([height - margin.bottom, margin.top]),
  }
}

/** SVG path of [d, z] pairs in a layout ("M..L..Z" when closed). */
export function pathOf(pairs: readonly [number, number][], layout: ProfileLayout, close = false): string {
  if (pairs.length === 0) return ''
  const d = pairs
    .map(([dm, z], i) => `${i ? 'L' : 'M'}${layout.x(dm).toFixed(1)},${layout.y(z).toFixed(1)}`)
    .join('')
  return close ? `${d}Z` : d
}
