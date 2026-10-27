import { describe, expect, it } from 'vitest'
import type { LidoireProfileData, UnifiedTimelineData } from '../types'
import profileFlood from '../test-fixtures/geo-profile-2024-02-26T11.json'
import profileLow from '../test-fixtures/geo-profile-2026-10-26T21.json'
import replayFeb2024 from '../test-fixtures/timeline-replay-2024-02.json'
import {
  DEFAULT_CONFLUENCE_PARAMS,
  backwaterPolygon,
  backwaterReach,
  confluenceParamsFrom,
  confluenceStage,
  findSteps,
  groundAt,
  pathOf,
  profileLayout,
  reachRelativeToMill,
} from './lidoireProfile'
import { envelopeMax, replaySeries } from './replay'

// Payloads of /api/geo/profile computed by the backend (main) from prod series
const flood = profileFlood as unknown as LidoireProfileData
const low = profileLow as unknown as LidoireProfileData
const points = flood.profile.points!

describe('confluenceStage', () => {
  it('weights Pessac and Libourne in m NGF (alpha = 0.2)', () => {
    // 0.8 x (3.33 + 3.73) + 0.2 x (-1.17 + 5.63) = 6.54
    expect(confluenceStage(3.73, 5.63)).toBe(6.54)
  })

  it('falls back to Pessac alone, and never uses Libourne alone (tidal)', () => {
    expect(confluenceStage(3.73, null)).toBe(7.06)
    expect(confluenceStage(null, 5.63)).toBeNull()
  })

  it('reads the zeros and alpha exposed by the API', () => {
    expect(confluenceParamsFrom(flood)).toEqual(DEFAULT_CONFLUENCE_PARAMS)
    expect(confluenceParamsFrom(null)).toEqual(DEFAULT_CONFLUENCE_PARAMS)
  })

  it('gives the backend Z_c from the recorded hourly series (12.5 h envelope, points <= t)', () => {
    const s = replaySeries(replayFeb2024 as unknown as UnifiedTimelineData)
    const t = Date.parse(flood.at)
    const p = envelopeMax(s.pessac, t, 12.5)
    const l = envelopeMax(s.libourne, t, 12.5)
    expect(p).toBeCloseTo(flood.confluence.inputs.pessac_max_m!, 3)
    expect(l).toBeCloseTo(flood.confluence.inputs.libourne_max_m!, 3)
    expect(confluenceStage(p, l)).toBeCloseTo(flood.confluence.z_c_m_ngf!, 3)
  })
})

describe('backwaterReach', () => {
  const synthetic = [
    { d_m: 0, z_m: 2 },
    { d_m: 100, z_m: 3 },
    { d_m: 200, z_m: 6 }, // weir
    { d_m: 300, z_m: 4 }, // low point behind the weir
    { d_m: 400, z_m: 8 },
  ]

  it('is unknown without a stage, none below the confluence ground', () => {
    expect(backwaterReach(synthetic, null).status).toBe('unknown')
    expect(backwaterReach(synthetic, 1.5)).toMatchObject({ status: 'none', distance_m: 0, connected_distance_m: 0 })
  })

  it('interpolates the crossing and separates connected water from a low point behind a weir', () => {
    const r = backwaterReach(synthetic, 5)
    expect(r.status).toBe('partial')
    expect(r.connected_distance_m).toBeCloseTo(166.7, 1) // 3 -> 6 m between 100 and 200 m
    expect(r.distance_m).toBe(325) // 4 -> 8 m between 300 and 400 m
    expect(r.disconnected_low_points).toBe(true)
  })

  it('covers the whole profile when the last sample is under Z_c', () => {
    expect(backwaterReach(synthetic, 9)).toMatchObject({ status: 'whole_profile', distance_m: 400, disconnected_low_points: false })
  })

  it.each([
    ['flood of 2024-02-26 11:00', flood],
    ['low water of 2026-10-26', low],
  ])('matches the backend on the %s', (_, payload) => {
    const r = backwaterReach(payload.profile.points!, payload.confluence.z_c_m_ngf)
    const b = payload.backwater
    expect(r.status).toBe(b.status)
    expect(r.distance_m).toBeCloseTo(b.distance_m!, 1)
    expect(r.connected_distance_m).toBeCloseTo(b.connected_distance_m!, 1)
    expect(r.disconnected_low_points).toBe(b.disconnected_low_points)
    const m = reachRelativeToMill(r, payload.mill.d_m)
    expect(m.distanceBelowMillM).toBeCloseTo(b.distance_below_mill_m!, 1)
    expect(m.connectedDistanceBelowMillM).toBeCloseTo(b.connected_distance_below_mill_m!, 1)
    expect(m.reachesMill).toBe(b.reaches_mill)
  })
})

describe('findSteps', () => {
  it('finds the two abrupt rises of the Lidoire (~2.5 and ~3.05 km)', () => {
    const steps = findSteps(points)
    expect(steps.map((s) => s.toD)).toEqual([2500, 3100])
    expect(steps[1]).toMatchObject({ fromZ: 6.16, toZ: 9 })
    expect(steps.every((s) => s.rise >= 2)).toBe(true)
  })
})

describe('backwaterPolygon', () => {
  it('is a flat top at Z_c over the ground, from the confluence to the connected front', () => {
    const zc = flood.confluence.z_c_m_ngf!
    const to = flood.backwater.connected_distance_m!
    const poly = backwaterPolygon(points, zc, to)
    expect(poly[0]).toEqual([0, zc])
    expect(poly[1]).toEqual([to, zc])
    expect(poly[poly.length - 1][0]).toBe(0)
    expect(poly.every(([d, z]) => d >= 0 && d <= to && z <= zc)).toBe(true)
    expect(poly[2][1]).toBeCloseTo(Math.min(zc, groundAt(points, to)!), 6)
  })

  it('is empty without water', () => {
    expect(backwaterPolygon(points, null, 100)).toEqual([])
    expect(backwaterPolygon(points, 5, 0)).toEqual([])
  })
})

describe('profileLayout', () => {
  it('maps the confluence left, the profile end right and m NGF upwards', () => {
    const L = profileLayout(640, points, { zc: 6.54 })
    expect(L.narrow).toBe(false)
    expect(L.x(0)).toBe(L.margin.left)
    expect(L.x(points[points.length - 1].d_m)).toBe(640 - L.margin.right)
    expect(L.y(12)).toBeLessThan(L.y(6.54))
    const [lo, hi] = L.y.domain()
    expect(lo).toBeLessThanOrEqual(Math.min(...points.map((p) => p.z_m)))
    expect(hi).toBeGreaterThan(12)
  })

  it('switches to the narrow layout on a phone and keeps the tested floor in view', () => {
    const L = profileLayout(343, points, { floorZ: 14 })
    expect(L.narrow).toBe(true)
    expect(L.y.domain()[1]).toBeGreaterThanOrEqual(14)
    expect(pathOf([[0, 2.42], [100, 3]], L)).toMatch(/^M[\d.]+,[\d.]+L[\d.]+,[\d.]+$/)
  })
})
