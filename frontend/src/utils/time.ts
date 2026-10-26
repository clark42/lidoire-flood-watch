// Time helpers shared by pages

/** Every date of the UI is shown in this zone, whatever the browser's zone. */
export const DISPLAY_TIME_ZONE = 'Europe/Paris'

const TZ_SUFFIX = /(Z|[+-]\d{2}:?\d{2})$/i

/**
 * Parse a backend timestamp.
 *
 * The API serialises UTC with an explicit offset ("...Z"). A few legacy
 * dict payloads still send naive UTC strings without a designator, which
 * `new Date()` would read as local time: strings without an explicit offset
 * are therefore treated as UTC.
 */
export function parseApiDate(value: string | null | undefined): Date | null {
  if (!value) return null
  const iso = TZ_SUFFIX.test(value) ? value : `${value}Z`
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? null : date
}

type ParisParts = {
  yyyy: string
  MM: string
  MMM: string
  MMMM: string
  dd: string
  d: string
  HH: string
  mm: string
  ss: string
  EEE: string
}

const NUMERIC_FORMAT = new Intl.DateTimeFormat('fr-FR', {
  timeZone: DISPLAY_TIME_ZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
})
const MONTH_FORMAT = new Intl.DateTimeFormat('fr-FR', { timeZone: DISPLAY_TIME_ZONE, month: 'short' })
const LONG_MONTH_FORMAT = new Intl.DateTimeFormat('fr-FR', { timeZone: DISPLAY_TIME_ZONE, month: 'long' })
const WEEKDAY_FORMAT = new Intl.DateTimeFormat('fr-FR', { timeZone: DISPLAY_TIME_ZONE, weekday: 'short' })

function parisParts(date: Date): ParisParts {
  const parts: Record<string, string> = {}
  for (const p of NUMERIC_FORMAT.formatToParts(date)) parts[p.type] = p.value
  return {
    yyyy: parts.year,
    MM: parts.month,
    MMM: MONTH_FORMAT.format(date),
    MMMM: LONG_MONTH_FORMAT.format(date),
    dd: parts.day,
    d: String(Number(parts.day)),
    HH: parts.hour,
    mm: parts.minute,
    ss: parts.second,
    EEE: WEEKDAY_FORMAT.format(date),
  }
}

const TOKENS = /yyyy|MMMM|MMM|MM|dd|d|HH|mm|ss|EEE/g

/**
 * Format a date in Europe/Paris with a small date-fns-like pattern
 * (tokens: yyyy MMMM MMM MM dd d HH mm ss EEE; anything else is copied).
 * Strings are parsed with parseApiDate. Returns '-' for missing/invalid input.
 */
export function formatParis(value: Date | string | null | undefined, pattern: string): string {
  const date = typeof value === 'string' ? parseApiDate(value) : value
  if (!date || Number.isNaN(date.getTime())) return '-'
  const parts = parisParts(date)
  return pattern.replace(TOKENS, (token) => parts[token as keyof ParisParts])
}

/** Hour of the day (0-23) in Europe/Paris. */
export function parisHour(value: Date | string): number {
  const formatted = formatParis(value, 'HH')
  return formatted === '-' ? 0 : Number(formatted)
}

/** Human readable age in French, e.g. "il y a 3 min". */
export function formatAge(from: Date, now: number = Date.now()): string {
  const seconds = Math.max(0, Math.round((now - from.getTime()) / 1000))
  if (seconds < 60) return "a l'instant"
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `il y a ${minutes} min`
  const hours = Math.floor(minutes / 60)
  if (hours < 48) {
    const rest = minutes % 60
    return rest > 0
      ? `il y a ${hours} h ${rest.toString().padStart(2, '0')}`
      : `il y a ${hours} h`
  }
  return `il y a ${Math.floor(hours / 24)} j`
}
