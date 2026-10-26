// Theme preference: follow the OS (default), or force light / dark.
// Stored in localStorage; public/theme-init.js applies it before the first
// paint (same key and attribute), this module keeps it in sync afterwards.

export type ThemePreference = 'system' | 'light' | 'dark'

export const THEME_STORAGE_KEY = 'lfw_theme'
export const THEME_ORDER: ThemePreference[] = ['system', 'light', 'dark']
export const THEME_LABELS: Record<ThemePreference, string> = {
  system: 'système',
  light: 'clair',
  dark: 'sombre',
}

export function isThemePreference(value: unknown): value is ThemePreference {
  return value === 'system' || value === 'light' || value === 'dark'
}

/** Stored preference; 'system' when absent, invalid or storage is blocked. */
export function readThemePreference(storage: Pick<Storage, 'getItem'> | null = safeStorage()): ThemePreference {
  try {
    const value = storage?.getItem(THEME_STORAGE_KEY)
    return isThemePreference(value) ? value : 'system'
  } catch {
    return 'system'
  }
}

/** Persist the preference ('system' clears it). Never throws. */
export function storeThemePreference(
  pref: ThemePreference,
  storage: Pick<Storage, 'setItem' | 'removeItem'> | null = safeStorage(),
): void {
  try {
    if (pref === 'system') storage?.removeItem(THEME_STORAGE_KEY)
    else storage?.setItem(THEME_STORAGE_KEY, pref)
  } catch {
    // Private mode or blocked storage: the choice lasts for this page only
  }
}

/** data-theme on <html>: absent for 'system' (CSS follows prefers-color-scheme). */
export function applyThemePreference(pref: ThemePreference, root: HTMLElement = document.documentElement): void {
  if (pref === 'system') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', pref)
}

export function nextThemePreference(pref: ThemePreference): ThemePreference {
  return THEME_ORDER[(THEME_ORDER.indexOf(pref) + 1) % THEME_ORDER.length]
}

function safeStorage(): Storage | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage
  } catch {
    return null
  }
}
