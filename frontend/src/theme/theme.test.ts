import { describe, expect, it } from 'vitest'
import {
  THEME_STORAGE_KEY,
  nextThemePreference,
  readThemePreference,
  storeThemePreference,
} from './theme'

function memoryStorage(initial: Record<string, string> = {}) {
  const data = new Map(Object.entries(initial))
  return {
    data,
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => void data.set(k, v),
    removeItem: (k: string) => void data.delete(k),
  }
}

const throwing = {
  getItem: () => {
    throw new Error('SecurityError')
  },
  setItem: () => {
    throw new Error('QuotaExceededError')
  },
  removeItem: () => {
    throw new Error('SecurityError')
  },
}

describe('theme preference', () => {
  it('cycles système -> clair -> sombre -> système', () => {
    expect(nextThemePreference('system')).toBe('light')
    expect(nextThemePreference('light')).toBe('dark')
    expect(nextThemePreference('dark')).toBe('system')
  })

  it('reads a stored preference and ignores invalid values', () => {
    expect(readThemePreference(memoryStorage({ [THEME_STORAGE_KEY]: 'dark' }))).toBe('dark')
    expect(readThemePreference(memoryStorage({ [THEME_STORAGE_KEY]: 'sepia' }))).toBe('system')
    expect(readThemePreference(memoryStorage())).toBe('system')
    expect(readThemePreference(null)).toBe('system')
  })

  it('stores light/dark and clears the key for system', () => {
    const s = memoryStorage()
    storeThemePreference('dark', s)
    expect(s.data.get(THEME_STORAGE_KEY)).toBe('dark')
    storeThemePreference('system', s)
    expect(s.data.has(THEME_STORAGE_KEY)).toBe(false)
  })

  it('never throws when storage is blocked', () => {
    expect(readThemePreference(throwing)).toBe('system')
    expect(() => storeThemePreference('light', throwing)).not.toThrow()
  })
})
