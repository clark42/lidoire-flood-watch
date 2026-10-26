// Confidence of the verdict: a list of checkable reasons, never a percentage
import type { OutlookConfidence, SourceFreshness, SourceFreshnessName } from '../types'
import { formatAge, parseApiDate } from './time'

export type ConfidenceKind = 'ok' | 'warn' | 'info'

export interface ConfidenceItem {
  kind: ConfidenceKind
  /** Bold lead (may be empty) */
  lead: string
  text: string
}

export const CONFIDENCE_LEVEL_LABELS: Record<OutlookConfidence['level'], string> = {
  good: 'bonne',
  reduced: 'réduite',
  low: 'faible',
}

/** Number of labelled floods the model was tuned on (episodes_labels.json). */
export const LABELLED_FLOODS = 5

// Notes of backend/app/services/outlook.py that report a degraded input
const WARNING_NOTE = /^(Source |Facteurs indisponibles|Marées estimées|Donnée de prévision manquante)/

function splitNote(note: string): { lead: string; text: string } {
  const at = note.indexOf(' : ')
  return at > 0 ? { lead: note.slice(0, at), text: note.slice(at + 3) } : { lead: '', text: note }
}

/**
 * Reasons shown in the confidence block: fresh primary station and score
 * (from /api/status), the outlook notes (degraded inputs first), and the
 * standing reminder that the model is tuned on few floods.
 */
export function confidenceItems(
  confidence: Pick<OutlookConfidence, 'notes'>,
  sources: Partial<Record<SourceFreshnessName, SourceFreshness>> | null | undefined,
  now: number = Date.now(),
): ConfidenceItem[] {
  const items: ConfidenceItem[] = []

  const pessac = sources?.river_primary
  const pessacAt = pessac?.status === 'ok' ? parseApiDate(pessac.last_timestamp) : null
  if (pessacAt) {
    const score = sources?.risk_score
    const scoreAt = score?.status === 'ok' ? parseApiDate(score.last_timestamp) : null
    items.push({
      kind: 'ok',
      lead: 'Pessac à jour',
      text:
        `dernière mesure ${formatAge(pessacAt, now)}` +
        (scoreAt ? `, score recalculé ${formatAge(scoreAt, now)}.` : '.'),
    })
  }

  const warnings: ConfidenceItem[] = []
  const infos: ConfidenceItem[] = []
  for (const note of confidence.notes) {
    if (WARNING_NOTE.test(note)) warnings.push({ kind: 'warn', ...splitNote(note) })
    else infos.push({ kind: 'info', lead: '', text: note })
  }
  items.push(...warnings, ...infos)

  items.push({
    kind: 'info',
    lead: `Modèle calé sur ${LABELLED_FLOODS} crues`,
    text: "utile pour classer les situations, pas une probabilité. Le moulin lui-même n'est pas mesuré.",
  })
  return items
}
