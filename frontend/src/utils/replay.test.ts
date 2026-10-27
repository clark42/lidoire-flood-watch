import { describe, expect, it } from 'vitest'
import type { EpisodeLabelSet, LidoireProfileData, UnifiedTimelineData } from '../types'
import labelsFixture from '../test-fixtures/episode-labels.json'
import profileFlood from '../test-fixtures/geo-profile-2024-02-26T11.json'
import replayFeb2024 from '../test-fixtures/timeline-replay-2024-02.json'
import { DEFAULT_CONFLUENCE_PARAMS } from './lidoireProfile'
import {
  HOUR_MS,
  envelopeMax,
  hourlyTimes,
  latestAt,
  maxIn,
  medianStepHours,
  nearestIndex,
  nextPlayIndex,
  replayFrame,
  replaySeries,
  replayStory,
  replayWindow,
  type ReplaySeries,
} from './replay'

const labels = (labelsFixture as unknown as EpisodeLabelSet).episodes
const profile = profileFlood as unknown as LidoireProfileData
const ground = profile.profile.points!
const series = replaySeries(replayFeb2024 as unknown as UnifiedTimelineData)
const NOW = new Date('2026-10-26T21:40:00Z')
const iso = (d: Date) => d.toISOString()

describe('replayWindow', () => {
  it('centres a flood preset on the hour-precise label: 4 days before, 3 after', () => {
    const w = replayWindow('feb2026', labels, NOW)
    expect(iso(w.focus)).toBe('2026-02-12T16:00:00.000Z')
    expect(iso(w.start)).toBe('2026-02-08T16:00:00.000Z')
    expect(iso(w.end)).toBe('2026-02-15T16:00:00.000Z')
    expect(w.label?.outcome).toBe('flooded')
  })

  it('floors a label to the hour (2023-11-30 22:15 -> 22:00)', () => {
    const w = replayWindow('nov2023', labels, NOW)
    expect(iso(w.peak!)).toBe('2023-11-30T22:15:00.000Z')
    expect(iso(w.focus)).toBe('2023-11-30T22:00:00.000Z')
  })

  it('uses the fallback peak while the labels are not loaded', () => {
    const w = replayWindow('apr2025', null, NOW)
    expect(w.label).toBeNull()
    expect(iso(w.focus)).toBe('2025-04-21T01:00:00.000Z')
  })

  it('shows the last 10 days with the cursor on the current hour', () => {
    const w = replayWindow('now', labels, NOW)
    expect(iso(w.end)).toBe('2026-10-26T21:00:00.000Z')
    expect(iso(w.focus)).toBe(iso(w.end))
    expect(w.end.getTime() - w.start.getTime()).toBe(10 * 24 * HOUR_MS)
  })

  it('opens a custom date (?tm=) in a window clipped to now', () => {
    const w = replayWindow('custom', labels, NOW, new Date('2026-10-25T08:30:00Z'))
    expect(iso(w.focus)).toBe('2026-10-25T08:00:00.000Z')
    expect(iso(w.end)).toBe('2026-10-26T21:00:00.000Z')
    expect(iso(w.start)).toBe('2026-10-21T20:00:00.000Z')
    // A future date is pinned to now
    expect(iso(replayWindow('custom', labels, NOW, new Date('2027-01-01')).focus)).toBe('2026-10-26T21:00:00.000Z')
  })
})

describe('time index', () => {
  const times = hourlyTimes(new Date('2024-02-22T11:00:00Z'), new Date('2024-02-29T11:00:00Z'))

  it('has one entry per hour, both ends included', () => {
    expect(times).toHaveLength(7 * 24 + 1)
    expect(times[1] - times[0]).toBe(HOUR_MS)
  })

  it('finds the nearest hour, clamped to the ends', () => {
    expect(nearestIndex(times, Date.parse('2024-02-26T11:00:00Z'))).toBe(96)
    expect(nearestIndex(times, Date.parse('2024-02-26T11:29:00Z'))).toBe(96)
    expect(nearestIndex(times, Date.parse('2024-02-26T11:31:00Z'))).toBe(97)
    expect(nearestIndex(times, 0)).toBe(0)
    expect(nearestIndex(times, Date.parse('2030-01-01'))).toBe(times.length - 1)
    expect(nearestIndex([], 5)).toBe(0)
  })

  it('plays 1 h per step, 6 h under reduced motion, and stops at the end', () => {
    expect(nextPlayIndex(10, times.length, false)).toBe(11)
    expect(nextPlayIndex(10, times.length, true)).toBe(16)
    expect(nextPlayIndex(times.length - 3, times.length, true)).toBe(times.length - 1)
  })
})

describe('series at a time', () => {
  const pts = [
    { t: 0, v: 1 },
    { t: HOUR_MS, v: 5 },
    { t: 2 * HOUR_MS, v: 3 },
    { t: 20 * HOUR_MS, v: 9 },
  ]

  it('takes the max over (t - 12.5 h, t], never a later point', () => {
    expect(envelopeMax(pts, 2 * HOUR_MS, 12.5)).toBe(5)
    expect(envelopeMax(pts, 14 * HOUR_MS, 12.5)).toBe(3)
    expect(envelopeMax(pts, 19 * HOUR_MS, 12.5)).toBeNull()
  })

  it('reads the latest point within a maximum age', () => {
    expect(latestAt(pts, 2.5 * HOUR_MS, 3 * HOUR_MS)?.v).toBe(3)
    expect(latestAt(pts, 10 * HOUR_MS, 3 * HOUR_MS)).toBeNull()
  })

  it('tells a daily series from an hourly one', () => {
    expect(medianStepHours(series.pessac)).toBe(1)
    expect(medianStepHours([{ t: 0, v: 1 }, { t: 24 * HOUR_MS, v: 1 }, { t: 48 * HOUR_MS, v: 1 }])).toBe(24)
    expect(maxIn(pts, 0, 3 * HOUR_MS)?.v).toBe(5)
  })

  it('builds the frame of the labelled hour like /api/geo/profile', () => {
    const f = replayFrame(series, Date.parse(profile.at), ground, profile.mill.d_m, DEFAULT_CONFLUENCE_PARAMS)
    expect(f.zc).toBeCloseTo(profile.confluence.z_c_m_ngf!, 3)
    expect(f.reach.distanceBelowMillM).toBeCloseTo(profile.backwater.distance_below_mill_m!, 1)
    expect(f.pessac).not.toBeNull()
    expect(f.score).not.toBeNull()
    expect(f.shadowV4).toBeNull() // not served by prod for this period
  })
})

describe('replayStory', () => {
  const win = replayWindow('feb2024', labels, NOW)
  const frameAt = (t: number) => replayFrame(series, t, ground, profile.mill.d_m, DEFAULT_CONFLUENCE_PARAMS)

  it('tells a flood from the label and the series only', () => {
    const story = replayStory('feb2024', series, win, frameAt(win.focus.getTime()), null)
    expect(story).toContain('Inondation labellisée le 26/02/2024 vers 12:00, 25 cm d’eau.'.replace('’', "'"))
    expect(story).toMatch(/L'Eyraud culmine à [\d,]+ m³\/s le \d\d\/\d\d à \d\d:\d\d/)
    expect(story).toMatch(/Pessac culmine à 5,14 m le 28\/02 à \d\d:\d\d, \d+ h après l'inondation/)
    expect(story).toContain('le remous s’arrête à ≈ 2,8 km sous le moulin'.replace('’', "'"))
  })

  it('summarises a plain window without a label', () => {
    const plain: ReplaySeries = { ...series, eyraud: [] }
    const story = replayStory('custom', plain, { ...win, label: null, peak: null }, null, frameAt(win.end.getTime()))
    expect(story).toMatch(/^Du 22\/02 au 29\/02\/2024, Pessac culmine à 5,14 m/)
    expect(story).toContain('Libourne oscille avec la marée entre')
    expect(story).toMatch(/il tombe \d+ mm \(Open-Meteo\)/)
    expect(story).toMatch(/le remous s'arrête à ≈ [\d,]+ km sous le moulin\.$/)
  })
})
