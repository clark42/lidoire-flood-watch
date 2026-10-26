import { describe, expect, it } from 'vitest'
import { MISSING_VALUE, formatDistanceKm, formatNumber, formatQuantity, riskLevelLabel } from './format'

// fr-FR groups thousands with a narrow no-break space
const NNBSP = ' '

describe('formatNumber', () => {
  it('uses a decimal comma and fixed decimals', () => {
    expect(formatNumber(1.05, 2)).toBe('1,05')
    expect(formatNumber(23.1, 0)).toBe('23')
    expect(formatNumber(4.1)).toBe('4,1')
    expect(formatNumber(1234.5, 0)).toBe(`1${NNBSP}235`)
  })

  it('never prints a negative zero', () => {
    expect(formatNumber(-0.01, 1)).toBe('0,0')
    expect(formatNumber(-0.04, 0)).toBe('0')
  })

  it('shows a dash for missing values', () => {
    expect(formatNumber(null)).toBe(MISSING_VALUE)
    expect(formatNumber(undefined)).toBe(MISSING_VALUE)
    expect(formatNumber(Number.NaN)).toBe(MISSING_VALUE)
  })
})

describe('formatQuantity', () => {
  it('follows the magnitude', () => {
    expect(formatQuantity(0.083)).toBe('0,083')
    expect(formatQuantity(0.48)).toBe('0,48')
    expect(formatQuantity(4.84)).toBe('4,8')
    expect(formatQuantity(25.5)).toBe('26')
    expect(formatQuantity(null)).toBe(MISSING_VALUE)
  })
})

describe('existing helpers', () => {
  it('labels levels in French', () => {
    expect(riskLevelLabel('orange')).toBe('Orange')
    expect(riskLevelLabel(null)).toBe('-')
  })

  it('formats distances', () => {
    expect(formatDistanceKm(7.62)).toBe('7.6 km')
    expect(formatDistanceKm(19.4)).toBe('19 km')
  })
})
