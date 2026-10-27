import { describe, expect, it } from 'vitest'
import {
  compareToFloods,
  comparisonSentence,
  EPISODE_GAP_PX,
  layoutRail,
  railDomain,
  RAIL_MARGIN,
  type RailEpisode,
  type RailSpec,
} from './rail'

const ep = (key: string, value: number, outcome: RailEpisode['outcome'] = 'flooded'): RailEpisode => ({
  key,
  value,
  outcome,
  title: key,
})

function spec(over: Partial<RailSpec> = {}): RailSpec {
  return {
    domain: [0, 10],
    value: 5,
    valueLabel: 'maint. 5',
    valueKind: 'measured',
    episodes: [],
    ticks: [],
    ariaLabel: 'rail',
    ...over,
  }
}

describe('railDomain', () => {
  it('rounds a log domain to whole decades around every value', () => {
    expect(railDomain([0.066, 26.5, 31.1, 13.1], { log: true })).toEqual([0.01, 100])
  })

  it('ignores missing and non-positive values on a log axis', () => {
    expect(railDomain([null, 0, -1, 2], { log: true })).toEqual([1, 10])
  })

  it('pads a linear domain from the floor and honours a minimum span', () => {
    const [d0, d1] = railDomain([0.61, 5.2, 3.8], { floor: 0 })
    expect(d0).toBe(0)
    expect(d1).toBeCloseTo(5.2 * 1.08)
    expect(railDomain([1], { floor: 0, minSpan: 4 })[1]).toBeCloseTo(4 * 1.08)
  })

  it('falls back to a default domain without values', () => {
    expect(railDomain([], { log: true })).toEqual([0.01, 100])
    expect(railDomain([null], { floor: 0, minSpan: 6 })).toEqual([0, 6])
  })
})

describe('layoutRail', () => {
  it('maps the domain onto the width minus the margins', () => {
    const L = layoutRail(spec({ value: 0 }), 300)
    expect(L.x(0)).toBe(RAIL_MARGIN.left)
    expect(L.x(10)).toBe(300 - RAIL_MARGIN.right)
    expect(L.valueX).toBe(RAIL_MARGIN.left)
  })

  it('uses log10 positions on a log rail (one decade = same width)', () => {
    const L = layoutRail(spec({ domain: [0.01, 100], log: true, value: 1 }), 420)
    const decade = (L.x(100) - L.x(0.01)) / 4
    expect(L.x(0.1) - L.x(0.01)).toBeCloseTo(decade)
    expect(L.valueX).toBeCloseTo(L.x(0.01) + 2 * decade)
  })

  it('clamps an out-of-domain value to the edge and says so', () => {
    const above = layoutRail(spec({ value: 14 }), 300)
    expect(above.valueX).toBe(300 - RAIL_MARGIN.right)
    expect(above.valueOutside).toBe('above')
    expect(layoutRail(spec({ value: null }), 300).valueX).toBeNull()
  })

  it('stacks episode dots that would overlap into rows above the track', () => {
    const L = layoutRail(spec({ episodes: [ep('a', 5), ep('b', 5.05), ep('c', 9)] }), 300)
    const [a, b, c] = L.episodes
    expect(a.y).toBeLessThan(L.trackY)
    expect(b.y).toBeLessThan(a.y) // second row
    expect(c.y).toBe(a.y) // far away: first row again
    expect(Math.abs(c.x - a.x)).toBeGreaterThanOrEqual(EPISODE_GAP_PX)
  })

  it('moves a colliding tick label one line down, hides a third one', () => {
    const L = layoutRail(
      spec({
        ticks: [
          { value: 3, label: 'jaune' },
          { value: 3.2, label: 'orange' },
          { value: 3.3, label: 'x' },
          { value: 8, label: 'moulin' },
        ],
      }),
      300,
    )
    expect(L.ticks.map((t) => [t.showLabel, t.labelRow])).toEqual([
      [true, 0],
      [true, 1],
      [false, 1],
      [true, 0],
    ])
    expect(L.height).toBeGreaterThan(layoutRail(spec({ ticks: [{ value: 3, label: 'jaune' }] }), 300).height)
  })

  it('places an unknown-threshold band and grows for a marker label', () => {
    const plain = layoutRail(spec(), 300)
    const L = layoutRail(spec({ band: { from: 5.2, to: 8.67, label: 'seuil inconnu' }, marker: { value: 2, label: 'prévue' } }), 300)
    expect(L.band!.x0).toBeLessThan(L.band!.x1)
    expect(L.marker!.x).toBeCloseTo(plain.x(2))
    expect(L.height).toBeGreaterThan(plain.height)
  })
})

describe('comparison with past floods', () => {
  const episodes = [ep('a', 26.5), ep('b', 31.1), ep('c', 2.2, 'false_positive'), ep('d', 20.5, 'near_miss')]
  const q = (v: number) => `${v} m³/s`

  it('counts only the floods reached by the value now', () => {
    expect(compareToFloods(28, episodes)).toEqual({ floods: 2, reached: 1, lowestFlood: 26.5, highestFlood: 31.1 })
  })

  it('writes the place of the value among the floods', () => {
    expect(comparisonSentence(0.07, compareToFloods(0.07, episodes), q)).toBe(
      'Sous les 2 crues labellisées (la plus faible : 26.5 m³/s).',
    )
    expect(comparisonSentence(28, compareToFloods(28, episodes), q)).toBe('Au niveau de 1 crue labellisée sur 2.')
    expect(comparisonSentence(40, compareToFloods(40, episodes), q)).toBe('Au niveau ou au-dessus des 2 crues labellisées.')
    expect(comparisonSentence(null, compareToFloods(null, episodes), q)).toBe('Valeur du moment indisponible.')
    expect(comparisonSentence(1, compareToFloods(1, []), q)).toBe('Aucune crue labellisée sur cet axe.')
  })
})
