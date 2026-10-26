// Risk levels (Vigicrues vocabulary) and the verdict of the Dashboard
import type { OutlookHorizon, RiskLevel, RiskOutlook } from '../types'
import { formatParisRelative, parseApiDate } from './time'
import { formatNumber } from './format'

export const LEVEL_ORDER: readonly RiskLevel[] = ['green', 'yellow', 'orange', 'red']

/** Score thresholds of the levels (model v3.4): green < 15, yellow < 35, orange < 55, red. */
export const LEVEL_THRESHOLDS = { yellow: 15, orange: 35, red: 55 } as const

/** 1 (green) to 4 (red): number of filled pips of a LevelMeter. */
export function levelRank(level: RiskLevel): number {
  return LEVEL_ORDER.indexOf(level) + 1
}

export function isRiskLevel(value: unknown): value is RiskLevel {
  return typeof value === 'string' && (LEVEL_ORDER as readonly string[]).includes(value)
}

export function levelFromScore(score: number): RiskLevel {
  if (score < LEVEL_THRESHOLDS.yellow) return 'green'
  if (score < LEVEL_THRESHOLDS.orange) return 'yellow'
  if (score < LEVEL_THRESHOLDS.red) return 'orange'
  return 'red'
}

/** Highest of the given levels (unknown values ignored), green when none. */
export function worstLevel(levels: Iterable<RiskLevel | null | undefined>): RiskLevel {
  let worst: RiskLevel = 'green'
  for (const level of levels) {
    if (isRiskLevel(level) && levelRank(level) > levelRank(worst)) worst = level
  }
  return worst
}

export const VERDICT_TITLES: Record<RiskLevel, string> = {
  green: 'Rien à faire',
  yellow: 'Surveiller',
  orange: 'Préparer le moulin',
  red: 'Agir maintenant',
}

export interface Verdict {
  /** Worst level between now and 72 h (current score included) */
  level: RiskLevel
  title: string
  /** Second line of the title: when */
  detail: string
  /** Horizon holding the 72 h maximum (its drivers explain the verdict) */
  peak: OutlookHorizon | null
  /** "Why" sentence: lead, then the driver texts (each carries its figure) */
  why: { lead: string; reasons: string[] }
}

/** Longest horizon (72 h): its maximum is the maximum of the whole window. */
export function peakHorizon(horizons: readonly OutlookHorizon[]): OutlookHorizon | null {
  return horizons.reduce<OutlookHorizon | null>(
    (best, h) => (best === null || h.hours > best.hours ? h : best),
    null,
  )
}

/** First time the verdict level is reached in the outlook (null if not forecast). */
function firstAtLevel(horizons: readonly OutlookHorizon[], level: RiskLevel): string | null {
  const reaching = horizons
    .filter((h) => h.level === level)
    .map((h) => h.first_at_level)
    .filter((t): t is string => parseApiDate(t) !== null)
    .sort((a, b) => parseApiDate(a)!.getTime() - parseApiDate(b)!.getTime())
  return reaching[0] ?? null
}

function verdictDetail(level: RiskLevel, reachedAt: Date | null, now: Date): string {
  const future = reachedAt !== null && reachedAt.getTime() > now.getTime()
  switch (level) {
    case 'green':
      return "ni ce soir, ni d'ici 3 jours"
    case 'yellow':
      return 'et revenir dans quelques heures'
    case 'orange':
      return future ? `avant ${formatParisRelative(reachedAt, now)}` : 'dès maintenant'
    case 'red':
      return future
        ? `niveau rouge prévu ${formatParisRelative(reachedAt, now)}`
        : "l'eau peut arriver"
  }
}

function whySentence(peak: OutlookHorizon | null, now: Date): Verdict['why'] {
  if (!peak) return { lead: 'Prévision du score indisponible.', reasons: [] }
  const score = `${formatNumber(peak.max_score, 0)}/100`
  const when = peak.max_hours_ahead === 0
    ? 'dès maintenant'
    : formatParisRelative(parseApiDate(peak.max_at), now)
  const reasons = peak.drivers.map((d) => d.text)
  if (reasons.length === 0) {
    return { lead: `Aucun facteur notable : score au plus ${score} d'ici ${peak.label}.`, reasons }
  }
  return { lead: `Maximum de ${score} ${when}, surtout :`, reasons }
}

/**
 * Verdict of the Dashboard: worst level between now and 72 h (the current
 * realtime level counts too), its wording, and the "why" of the 72 h peak.
 */
export function buildVerdict(
  outlook: Pick<RiskOutlook, 'horizons'>,
  currentLevel: RiskLevel | null = null,
  now: Date = new Date(),
): Verdict {
  const level = worstLevel([currentLevel, ...outlook.horizons.map((h) => h.level)])
  const reachedAt = currentLevel === level ? now : parseApiDate(firstAtLevel(outlook.horizons, level))
  const peak = peakHorizon(outlook.horizons)
  return {
    level,
    title: VERDICT_TITLES[level],
    detail: verdictDetail(level, reachedAt, now),
    peak,
    why: whySentence(peak, now),
  }
}
