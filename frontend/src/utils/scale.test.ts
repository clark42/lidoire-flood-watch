import { describe, expect, it } from 'vitest'
import { clamp, linearScale, logScale, niceTicks } from './scale'

describe('linearScale', () => {
  it('maps and inverts a domain onto a range', () => {
    const x = linearScale([0, 100], [10, 210])
    expect(x(0)).toBe(10)
    expect(x(50)).toBe(110)
    expect(x(100)).toBe(210)
    expect(x.invert(110)).toBe(50)
  })

  it('supports inverted ranges (SVG y axis)', () => {
    const y = linearScale([0, 8], [200, 0])
    expect(y(0)).toBe(200)
    expect(y(2)).toBe(150)
    expect(y.invert(150)).toBe(2)
  })

  it('does not divide by zero on a flat domain', () => {
    expect(Number.isFinite(linearScale([3, 3], [0, 100])(3))).toBe(true)
  })
})

describe('logScale', () => {
  it('spaces decades evenly and pins values below the domain', () => {
    const q = logScale([0.01, 100], [0, 400])
    expect(q(0.01)).toBeCloseTo(0)
    expect(q(0.1)).toBeCloseTo(100)
    expect(q(1)).toBeCloseTo(200)
    expect(q(100)).toBeCloseTo(400)
    expect(q(0)).toBeCloseTo(0)
    expect(q.invert(200)).toBeCloseTo(1)
  })

  it('rejects a non-positive domain', () => {
    expect(() => logScale([0, 10], [0, 1])).toThrow(RangeError)
  })
})

describe('niceTicks', () => {
  it('picks round steps', () => {
    expect(niceTicks(0, 100, 5)).toEqual([0, 20, 40, 60, 80, 100])
    expect(niceTicks(0, 8, 4)).toEqual([0, 2, 4, 6, 8])
    expect(niceTicks(0.3, 1.2, 4)).toEqual([0.5, 0.75, 1])
    expect(niceTicks(0, 1, 4)).toEqual([0, 0.25, 0.5, 0.75, 1])
  })

  it('handles degenerate input', () => {
    expect(niceTicks(5, 5)).toEqual([5])
    expect(niceTicks(Number.NaN, 1)).toEqual([])
    expect(niceTicks(10, 0, 5)).toEqual([0, 2, 4, 6, 8, 10])
  })

  it('clamps', () => {
    expect(clamp(-1, 0, 1)).toBe(0)
    expect(clamp(0.5, 0, 1)).toBe(0.5)
    expect(clamp(3, 0, 1)).toBe(1)
  })
})
