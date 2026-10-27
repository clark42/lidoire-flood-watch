import { describe, expect, it } from 'vitest'
import type { EnsembleForecast, ForecastData, RealtimeData } from '../types'
import forecastFixture from '../test-fixtures/risk-forecast.json'
import ensembleFixture from '../test-fixtures/forecast-ensemble.json'
import realtimeFixture from '../test-fixtures/realtime.json'
import {
  forecastPoints,
  levelRuns,
  nearestIndex,
  nightIntervals,
  peakIndex,
  rainRow,
  ribbonWindow,
  tideMarks,
  tideRadius,
  timeScale,
  timeTicks,
  vigicruesMarks,
} from './ribbon'
import { formatParis } from './time'

const forecast = forecastFixture as unknown as ForecastData
const ensemble = ensembleFixture as unknown as EnsembleForecast
const realtime = realtimeFixture as unknown as RealtimeData
const NOW = new Date('2026-10-26T23:10:00Z')
const H = 3600_000

describe('ribbon time axis (Europe/Paris)', () => {
  it('starts at the current hour and spans 72 h or 7 days', () => {
    const [t0, t1] = ribbonWindow(NOW, 72)
    expect(new Date(t0).toISOString()).toBe('2026-10-26T23:00:00.000Z')
    expect((t1 - t0) / H).toBe(72)
    expect((ribbonWindow(NOW, 168)[1] - t0) / H).toBe(168)
  })

  it('maps time linearly and clamps outside the window', () => {
    const [t0, t1] = ribbonWindow(NOW, 72)
    const x = timeScale([t0, t1], [50, 770])
    expect(x(t0)).toBe(50)
    expect(x(t0 + 36 * H)).toBe(410)
    expect(x(t1 + 10 * H)).toBe(770)
    expect(x.invert(410)).toBe(t0 + 36 * H)
  })

  it('shades the nights from 20:00 to 07:00 Paris time', () => {
    const [t0, t1] = ribbonWindow(NOW, 72)
    const nights = nightIntervals(t0, t1)
    // Starts at 00:00 Paris (in the night) and ends 72 h later at 00:00
    expect(nights).toHaveLength(4)
    expect(nights[0][0]).toBe(t0)
    expect(formatParis(new Date(nights[0][1]), 'HH:mm')).toBe('07:00')
    expect(formatParis(new Date(nights[1][0]), 'HH:mm')).toBe('20:00')
    expect(nights[3][1]).toBe(t1)
  })

  it('keeps nights right across the DST change (UTC+2 to UTC+1)', () => {
    // 2026-10-25: clocks go back at 03:00 Paris
    const t0 = Date.parse('2026-10-24T12:00:00Z')
    const nights = nightIntervals(t0, t0 + 24 * H)
    expect(nights).toHaveLength(1)
    expect(formatParis(new Date(nights[0][0]), 'HH:mm')).toBe('20:00')
    expect(formatParis(new Date(nights[0][1]), 'HH:mm')).toBe('07:00')
    expect((nights[0][1] - nights[0][0]) / H).toBe(12) // 11 h on the clock + the repeated hour
  })

  it('labels Paris midnights and 6 h steps (12 h when narrow, none over 7 days)', () => {
    const [t0, t1] = ribbonWindow(NOW, 72)
    const wide = timeTicks(t0, t1, 72, false)
    expect(wide.filter((t) => t.major).map((t) => t.label)).toEqual(['mar. 27', 'mer. 28', 'jeu. 29', 'ven. 30'])
    expect(wide.filter((t) => !t.major).map((t) => t.label).slice(0, 3)).toEqual(['6h', '12h', '18h'])
    expect(timeTicks(t0, t1, 72, true).filter((t) => !t.major).every((t) => t.label === '12h')).toBe(true)
    expect(timeTicks(t0, t0 + 168 * H, 168, false).every((t) => t.major)).toBe(true)
    expect(timeTicks(t0, t0 + 168 * H, 168, true, true).map((t) => t.label).slice(0, 3)).toEqual(['27', '28', '29'])
  })
})

describe('ribbon rows from the prod payloads', () => {
  const [t0, t1] = ribbonWindow(NOW, 72)

  it('keeps the forecast hours of the window, H+0 included', () => {
    const points = forecastPoints(forecast, t0, t1)
    expect(points[0].hoursAhead).toBe(0)
    expect(points).toHaveLength(73)
    expect(points.every((p, i) => i === 0 || p.t > points[i - 1].t)).toBe(true)
    expect(points[peakIndex(points)].score).toBe(Math.max(...points.map((p) => p.score)))
    expect(nearestIndex(points, points[10].t + 20 * 60_000)).toBe(10)
  })

  it('merges consecutive hours of the same level', () => {
    const runs = levelRuns([
      { t: 0, hoursAhead: 0, score: 10, level: 'green', pessac: 0 },
      { t: H, hoursAhead: 1, score: 16, level: 'yellow', pessac: 0 },
      { t: 2 * H, hoursAhead: 2, score: 20, level: 'yellow', pessac: 0 },
    ])
    expect(runs).toEqual([
      { t0: 0, t1: H, level: 'green' },
      { t0: H, t1: 3 * H, level: 'yellow' },
    ])
  })

  it('reads the 24 h ensemble windows of the chosen model and its horizon', () => {
    const ecmwf = rainRow(ensemble, 'ecmwf_ifs025', t0, t1)
    expect(ecmwf.blocks).toHaveLength(3)
    expect(ecmwf.blocks[0]).toMatchObject({ p10: 0, p50: 0.13, p90: 0.3, max: 0.6, members: 51 })
    expect(ecmwf.members).toBe(51)
    const d2 = rainRow(ensemble, 'icon_d2', t0, t1)
    expect(d2.blocks).toHaveLength(1) // ICON-D2 covers ~2.5 days: one full window
    expect(new Date(d2.horizonEnd!).toISOString()).toBe('2026-10-28T18:00:00.000Z')
    expect(rainRow(null, 'ecmwf_ifs025', t0, t1).blocks).toEqual([])
  })

  it('sizes the high tides by coefficient', () => {
    expect(tideRadius(40)).toBe(4)
    expect(tideRadius(120)).toBe(10)
    expect(tideRadius(null)).toBe(4)
    const marks = tideMarks(forecast.high_tides, t0, t1)
    expect(marks.length).toBeGreaterThanOrEqual(5)
    expect(marks[0]).toMatchObject({ coefficient: 91 })
    expect(marks[0].r).toBeCloseTo(4 + (51 / 80) * 6)
  })

  it('keeps the Vigicrues Libourne forecast and drops malformed entries', () => {
    const marks = vigicruesMarks(realtime.river.forecast_scenarios, t0, t1)
    expect(marks.map((m) => m.mean_m)).toEqual([4.65, 4.76])
    expect(vigicruesMarks([{ timestamp: 'x' }, null], t0, t1)).toEqual([])
  })
})
