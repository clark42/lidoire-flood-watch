import { useEffect, useState } from 'react'
import {
  THEME_LABELS,
  applyThemePreference,
  nextThemePreference,
  readThemePreference,
  storeThemePreference,
  type ThemePreference,
} from '../theme/theme'

/** Cycles the theme: système -> clair -> sombre (stored in localStorage). */
export default function ThemeToggle() {
  const [pref, setPref] = useState<ThemePreference>(() => readThemePreference())

  useEffect(() => {
    applyThemePreference(pref)
  }, [pref])

  const next = nextThemePreference(pref)
  return (
    <button
      type="button"
      onClick={() => {
        storeThemePreference(next)
        setPref(next)
      }}
      className="inline-flex items-center gap-1.5 rounded-full border border-line bg-surface px-3 min-h-[44px] sm:min-h-[32px] text-[13px] text-ink-2 hover:border-line-2 hover:text-ink"
      aria-label={`Thème : ${THEME_LABELS[pref]}. Passer au thème ${THEME_LABELS[next]}`}
    >
      <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">
        <circle cx="8" cy="8" r="6.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
        <path d="M8 1.5a6.5 6.5 0 0 1 0 13z" fill="currentColor" />
      </svg>
      <span>Thème : {THEME_LABELS[pref]}</span>
    </button>
  )
}
