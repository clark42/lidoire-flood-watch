// Time helpers shared by pages

const TZ_SUFFIX = /(Z|[+-]\d{2}:?\d{2})$/i

/**
 * Parse a backend timestamp.
 *
 * The API serializes naive UTC datetimes (datetime.utcnow()) without a
 * timezone designator; `new Date()` would read those as local time.
 * Strings without an explicit offset are therefore treated as UTC.
 */
export function parseApiDate(value: string | null | undefined): Date | null {
  if (!value) return null
  const iso = TZ_SUFFIX.test(value) ? value : `${value}Z`
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? null : date
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
