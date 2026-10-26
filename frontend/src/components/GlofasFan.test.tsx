import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import type { EnsembleForecast, EpisodeDriversData } from '../types'
import ensembleFixture from '../test-fixtures/forecast_ensemble.json'
import driversFixture from '../test-fixtures/episodes_drivers.json'
import {
  bandPath,
  decadeTicks,
  fanDays,
  fanDomain,
  fanSummary,
  floodBand,
  hasQuartiles,
  linePath,
  type FanDay,
} from '../utils/glofasFan'
import { linearScale, logScale } from '../utils/scale'
import { GlofasFanView } from './GlofasFan'

const ensemble = ensembleFixture as unknown as EnsembleForecast
const drivers = driversFixture as unknown as EpisodeDriversData
const glofas = ensemble.glofas
const NOW = new Date('2026-10-26T23:12:00Z')

function text(html: string): string {
  return html.replace(/<[^>]+>/g, '').replace(/&#x27;/g, "'").replace(/&amp;/g, '&')
}

const DAYS: FanDay[] = [
  { date: '2026-10-28', lead_days: 2, p10: 0.1, p25: 0.2, p50: 0.3, p75: 0.5, p90: 0.8, min: 0.05, max: 2, mean: 0.4, members: 51, prob_gt_p95: 0, prob_gt_p99: 0 },
  { date: '2026-10-27', lead_days: 1, p10: 0.1, p25: 0.15, p50: 0.2, p75: 0.3, p90: 0.4, min: 0.05, max: 0.6, mean: 0.2, members: 51, prob_gt_p95: 0, prob_gt_p99: 0 },
]

describe('fan helpers', () => {
  it('sorts the days and detects the quartiles', () => {
    const days = fanDays({ available: true, days: DAYS })
    expect(days.map((d) => d.date)).toEqual(['2026-10-27', '2026-10-28'])
    expect(hasQuartiles(days)).toBe(true)
    // The API serves p10/p50/p90/min/max only
    expect(hasQuartiles(fanDays(glofas))).toBe(false)
    expect(fanDays(glofas)).toHaveLength(30)
  })

  it('covers the fan and the climatology with whole decades', () => {
    const days = fanDays(glofas)
    const clim = glofas.climatology!
    const d = fanDomain(days, [clim.p90, clim.p99])
    expect(d[0]).toBeLessThanOrEqual(Math.min(...days.map((x) => x.p10)))
    expect(d[1]).toBeGreaterThanOrEqual(clim.p99)
    expect(Math.log10(d[0]) % 1).toBeCloseTo(0, 9)
    expect(decadeTicks([0.01, 10])).toEqual([0.01, 0.1, 1, 10])
    expect(fanDomain([], [])).toEqual([0.01, 10])
  })

  it('draws closed bands and open lines in screen space', () => {
    const days = fanDays({ available: true, days: DAYS })
    const x = linearScale([Date.parse('2026-10-27T12:00Z'), Date.parse('2026-10-28T12:00Z')], [0, 100])
    const y = logScale([0.01, 10], [200, 0])
    const outer = bandPath(days, x, y, 'p10', 'p90')
    expect(outer.startsWith('M0.0,')).toBe(true)
    expect(outer.endsWith('Z')).toBe(true)
    // p90 on top (smaller y) than p10
    expect(y(0.4)).toBeLessThan(y(0.1))
    expect(linePath(days, x, y, 'max')).toMatch(/^M0\.0,[\d.]+L100\.0,[\d.]+$/)
    // Missing quartiles: nothing drawn
    expect(bandPath(fanDays(glofas), x, y, 'p25', 'p75')).toBe('')
  })

  it('summarises the wettest member and the climatology crossing', () => {
    const s = fanSummary(fanDays({ available: true, days: DAYS }), { p50: 0.1, p90: 0.7, p95: 1, p99: 3, max: 9 })
    expect(s.wettest).toEqual({ value: 2, date: '2026-10-28' })
    expect(s.firstAboveP90).toBe('2026-10-28')
  })

  it('keeps the flood band empty until the API serves values at floods', () => {
    expect(floodBand(drivers)).toBeNull()
    const withValues = {
      episodes: drivers.episodes.map((e, i) =>
        e.outcome === 'flooded' ? { ...e, values: { ...e.values, glofas_m3s: 4 + i } } : e,
      ),
    }
    const band = floodBand(withValues)!
    expect(band.count).toBe(5)
    expect(band.min).toBeLessThan(band.max)
    expect(band.years).toEqual(['2023', '2024', '2025'])
  })
})

describe('GlofasFanView', () => {
  it('renders the fan with its honesty label and the missing parts said', () => {
    const html = renderToStaticMarkup(<GlofasFanView glofas={glofas} drivers={drivers} width={343} now={NOW} />)
    const t = text(html)
    expect(html).toContain('role="img"')
    expect(t).toContain('modèle, tendance, pas un niveau')
    expect(t).toContain('p90 climatologie (2,0)')
    expect(t).toContain('p99 climatologie (6,4)')
    expect(t).toContain('p25-p75 : non servi par l’API')
    expect(t).toContain('Valeurs simulées aux crues passées : pas encore servies')
    expect(t).toContain('51 membres')
    expect(html).toContain('class="gfan-outer"')
    expect(html).not.toContain('class="gfan-inner"')
  })

  it('says when GloFAS is unavailable', () => {
    const t = text(renderToStaticMarkup(<GlofasFanView glofas={{ available: false, note: 'run absent' }} width={343} now={NOW} />))
    expect(t).toBe('GloFAS indisponible : run absent.')
  })
})
