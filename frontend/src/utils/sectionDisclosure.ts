// Collapsible Dashboard sections (long, secondary ones): collapsed by
// default on narrow screens, open on desktop, and a deep link (#replay) or
// a click in the section bar always opens its section. Pure helpers plus a
// tiny window event so that the bar can open a section already in the hash.

/** Under this viewport width (px), collapsible sections start collapsed. */
export const COLLAPSE_BELOW_PX = 640

export const OPEN_SECTION_EVENT = 'lfw:open-section'

/** Initial state of a collapsible section: open when the URL targets it, else open from 640 px. */
export function initialSectionOpen(id: string, env: { wide: boolean; hash: string }): boolean {
  if (env.hash.replace(/^#/, '') === id) return true
  return env.wide
}

/** Initial state read from the browser (open when there is no window: server render). */
export function browserSectionOpen(id: string): boolean {
  if (typeof window === 'undefined') return true
  const wide = window.matchMedia
    ? window.matchMedia(`(min-width: ${COLLAPSE_BELOW_PX}px)`).matches
    : window.innerWidth >= COLLAPSE_BELOW_PX
  return initialSectionOpen(id, { wide, hash: window.location.hash })
}

/** Asks the section `id` to open (section bar click, even when the hash does not change). */
export function requestOpenSection(id: string) {
  if (typeof window === 'undefined') return
  window.dispatchEvent(new CustomEvent<string>(OPEN_SECTION_EVENT, { detail: id }))
}
