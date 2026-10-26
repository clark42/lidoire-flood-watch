import { describe, expect, it } from 'vitest'
import type { EpisodeDriversData, UnifiedTimelineData } from '../types'
import driversFixture from '../test-fixtures/episode-drivers.json'
import trailFeb2026 from '../test-fixtures/timeline-trail-2026-02.json'
import trailNow from '../test-fixtures/timeline-trail-now.json'
import {
  PHASE_Q_MAX,
  PHASE_Q_MIN,
  dailyTrail,
  dayWindow,
  episodePoints,
  floodPhaseCaption,
  phaseLayout,
  phaseReading,
  todayPoint,
  trailPath,
} from './phasePlane'
import { HOUR_MS } from './replay'

const drivers = driversFixture as unknown as EpisodeDriversData
// Daily maxima of the peak day, read from prod for the two floods whose
// 12.5 h window holds no stored Eyraud point (daily history)
const FALLBACK = { '2023-11-30': 20.6, '2026-02-12': 27.4 }

describe('phaseLayout', () => {
  it('puts the discharge on a log scale (one decade = same width)', () => {
    const L = phaseLayout(640)
    expect(L.x(10) - L.x(1)).toBeCloseTo(L.x(1) - L.x(0.1), 6)
    expect(L.x(PHASE_Q_MIN)).toBe(L.margin.left)
    expect(L.x(PHASE_Q_MAX.wide)).toBe(640 - L.margin.right)
    expect(phaseLayout(343).x.domain()[1]).toBe(PHASE_Q_MAX.narrow)
  })

  it('pins discharges below the domain to the axis instead of -Infinity', () => {
    const L = phaseLayout(640)
    expect(L.x(0)).toBe(L.margin.left)
    expect(L.x(0.001)).toBe(L.margin.left)
  })

  it('grows the Pessac axis for a record and is square-ish on a phone', () => {
    expect(phaseLayout(640, 9.2).y.domain()[1]).toBe(10)
    const phone = phaseLayout(343)
    expect(phone.narrow).toBe(true)
    expect(phone.height).toBeGreaterThanOrEqual(300)
    expect(phone.y(0)).toBe(phone.height - phone.margin.bottom)
  })
})

describe('dailyTrail', () => {
  it('dates each 1d window by the day before its stop timestamp', () => {
    const trail = dailyTrail(trailFeb2026 as unknown as UnifiedTimelineData)
    expect(trail[0].day).toBe('2026-01-29')
    const peakQ = trail.reduce((a, b) => (b.q > a.q ? b : a))
    expect(peakQ.day).toBe('2026-02-12')
    expect(peakQ.q).toBeCloseTo(27.4, 1)
    const peakP = trail.reduce((a, b) => (b.pessac > a.pessac ? b : a))
    expect(peakP.day).toBe('2026-02-14') // record of 14/02
    expect(peakP.pessac).toBeCloseTo(7.884, 3)
    expect(trail.every((p, i) => i === 0 || p.t > trail[i - 1].t)).toBe(true)
  })

  it('keeps the partial current day of the 21-day trail', () => {
    const trail = dailyTrail(trailNow as unknown as UnifiedTimelineData)
    expect(trail[trail.length - 1].day).toBe('2026-10-26')
    expect(trail.length).toBeGreaterThanOrEqual(21)
  })

  it('draws a path with one vertex per day', () => {
    const trail = dailyTrail(trailNow as unknown as UnifiedTimelineData)
    const d = trailPath(trail, phaseLayout(640))
    expect(d.startsWith('M')).toBe(true)
    expect(d.split('L')).toHaveLength(trail.length)
  })
})

describe('episodes', () => {
  it('uses the 12.5 h values of the drivers and a daily fallback when Q is null', () => {
    const bare = episodePoints(drivers)
    expect(bare.find((e) => e.key === '2026-02-12')).toMatchObject({ q: null, qBasis: 'missing' })
    const pts = episodePoints(drivers, FALLBACK)
    expect(pts).toHaveLength(drivers.episodes.length)
    expect(pts.find((e) => e.key === '2026-02-12')).toMatchObject({ q: 27.4, qBasis: 'daily', outcome: 'flooded' })
    expect(pts.find((e) => e.key === '2024-02-26')).toMatchObject({ q: 23.064, qBasis: 'envelope', pessac: 3.73 })
    expect(pts.find((e) => e.key === '2023-03-11')?.infiltration).toBe(true)
  })

  it('gives the window of the daily fallback request (UTC day of the peak)', () => {
    expect(dayWindow('2023-11-30T22:15:00Z')).toEqual({
      start: '2023-11-30T00:00:00.000Z',
      end: '2023-12-01T00:00:00.000Z',
    })
    expect(dayWindow('nope')).toBeNull()
  })

  it('reads the plane from the data: runoff separates best, Pessac does not', () => {
    const r = phaseReading(episodePoints(drivers, FALLBACK))
    expect(r.floodCount).toBe(5)
    expect(r.runoffFloods).toHaveLength(4)
    expect(r.minFloodQ?.key).toBe('2025-04-21')
    expect(r.maxDryQ?.key).toBe('2023-12-11')
    expect(r.qSeparates).toBe(false) // 19.6 vs 19.7 m3/s on the stored data
    expect(r.maxDryPessac?.key).toBe('2024-05-09')
    expect(r.pessacSeparates).toBe(false)
  })

  it('writes an honest caption: five floods only, runoff axis, Pessac as a wetness proxy', () => {
    const caption = floodPhaseCaption(episodePoints(drivers, FALLBACK))
    expect(caption).toMatch(/^5 crues labellisées seulement/)
    expect(caption).toContain('Le ruissellement est l’axe qui sépare le mieux')
    expect(caption).toContain('≥ 19,6 m³/s')
    expect(caption).toContain('chevauchement, pas de seuil net')
    expect(caption).toContain('Pessac n’est qu’un indicateur d’humidité régionale : le 09/05/2024 il était à 5,15 m')
    expect(caption).toContain('infiltration')
  })
})

describe('todayPoint', () => {
  const now = Date.parse('2026-10-26T21:00:00Z')
  const pessac = [
    { t: now - 20 * HOUR_MS, v: 2 },
    { t: now - 5 * HOUR_MS, v: 0.7 },
    { t: now - HOUR_MS, v: 0.6 },
  ]

  it('takes the 12.5 h maxima like the episode values', () => {
    const p = todayPoint(pessac, [{ t: now - 2 * HOUR_MS, v: 0.12 }], now)
    expect(p).toMatchObject({ pessac: 0.7, q: 0.12, qBasis: 'envelope' })
  })

  it('falls back to the latest Eyraud point within 36 h, then to nothing', () => {
    expect(todayPoint(pessac, [{ t: now - 30 * HOUR_MS, v: 0.2 }], now)).toMatchObject({ q: 0.2, qBasis: 'latest' })
    expect(todayPoint(pessac, [{ t: now - 40 * HOUR_MS, v: 0.2 }], now)).toMatchObject({ q: null, qBasis: 'missing' })
  })
})
