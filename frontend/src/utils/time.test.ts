import { describe, expect, it } from 'vitest'
import { formatAge, formatParis, formatParisRelative, parisDayKey, parisHour, parseApiDate } from './time'

describe('parseApiDate', () => {
  it('reads naive strings as UTC and keeps explicit offsets', () => {
    expect(parseApiDate('2026-10-26T21:55:00')?.toISOString()).toBe('2026-10-26T21:55:00.000Z')
    expect(parseApiDate('2026-10-26T21:55:00Z')?.toISOString()).toBe('2026-10-26T21:55:00.000Z')
    expect(parseApiDate('2026-10-26T23:55:00+02:00')?.toISOString()).toBe('2026-10-26T21:55:00.000Z')
  })

  it('returns null for missing or invalid input', () => {
    expect(parseApiDate(null)).toBeNull()
    expect(parseApiDate('')).toBeNull()
    expect(parseApiDate('pas une date')).toBeNull()
  })
})

describe('formatParis', () => {
  it('shows Europe/Paris time across the DST change (25 Oct 2026)', () => {
    // Summer time (UTC+2) before 01:00 UTC on 25 Oct, winter time (UTC+1) after
    expect(formatParis('2026-10-24T22:30:00Z', 'dd/MM HH:mm')).toBe('25/10 00:30')
    expect(formatParis('2026-10-25T00:30:00Z', 'HH:mm')).toBe('02:30')
    expect(formatParis('2026-10-25T01:30:00Z', 'HH:mm')).toBe('02:30')
    expect(formatParis('2026-10-26T22:40:00Z', 'EEE d HH:mm')).toBe('lun. 26 23:40')
  })

  it('returns "-" for invalid input', () => {
    expect(formatParis(null, 'HH:mm')).toBe('-')
    expect(formatParis('nope', 'HH:mm')).toBe('-')
  })

  it('gives the Paris hour and day, not the UTC ones', () => {
    expect(parisHour('2026-10-26T23:30:00Z')).toBe(0)
    expect(parisDayKey('2026-10-26T23:30:00Z')).toBe('2026-10-27')
  })
})

describe('formatParisRelative', () => {
  const now = new Date('2026-10-26T22:40:00Z') // Monday 23:40 in Paris

  it('names today, tomorrow and yesterday on the Paris calendar', () => {
    expect(formatParisRelative('2026-10-26T22:50:00Z', now)).toBe("aujourd'hui à 23:50")
    // 23:10 UTC is already Tuesday in Paris
    expect(formatParisRelative('2026-10-26T23:10:00Z', now)).toBe('demain à 00:10')
    expect(formatParisRelative('2026-10-25T20:00:00Z', now)).toBe('hier à 21:00')
    expect(formatParisRelative('2026-10-28T09:00:00Z', now)).toBe('mer. 28 à 10:00')
  })

  it('has a compact form for the horizon tiles', () => {
    expect(formatParisRelative('2026-10-26T22:50:00Z', now, { compact: true })).toBe('auj. 23:50')
    expect(formatParisRelative('2026-10-27T03:00:00Z', now, { compact: true })).toBe('demain 04:00')
  })

  it('returns "-" for invalid input', () => {
    expect(formatParisRelative(null, now)).toBe('-')
  })
})

describe('formatAge', () => {
  const now = Date.parse('2026-10-26T22:40:00Z')
  it('speaks French minutes, hours and days', () => {
    expect(formatAge(new Date(now - 20_000), now)).toBe("a l'instant")
    expect(formatAge(new Date(now - 22 * 60_000), now)).toBe('il y a 22 min')
    expect(formatAge(new Date(now - 125 * 60_000), now)).toBe('il y a 2 h 05')
    expect(formatAge(new Date(now - 3 * 86_400_000), now)).toBe('il y a 3 j')
  })
})
