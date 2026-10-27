import type { EnsembleForecast, OutlookConfidence, SourceFreshness, SourceFreshnessName } from '../types'
import { netatmoAuthUrl } from '../api'
import { CONFIDENCE_LEVEL_LABELS, confidenceItems, type ConfidenceKind } from '../utils/confidence'

const CONFIDENCE_CHIP: Record<OutlookConfidence['level'], string> = {
  good: 'bg-lv-green-soft',
  reduced: 'bg-lv-yellow-soft',
  low: 'bg-lv-red-soft',
}

const ICON_LABEL: Record<ConfidenceKind, string> = { ok: 'Fiable', warn: 'Attention', info: 'À savoir' }

function ConfidenceIcon({ kind }: { kind: ConfidenceKind }) {
  if (kind === 'ok') {
    return (
      <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true" className="mt-[3px] text-lv-green">
        <path d="M2.5 7.5l3 3 6-7" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    )
  }
  if (kind === 'warn') {
    return (
      <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true" className="mt-[3px] text-lv-orange">
        <path d="M7 1.5l6 11H1z" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
        <path d="M7 5.5v3.2M7 10.3v.4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
      </svg>
    )
  }
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true" className="mt-[3px] text-ink-3">
      <circle cx="7" cy="7" r="5.8" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path d="M7 6.2v4M7 3.9v.3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  )
}

export interface ConfidencePanelProps {
  confidence: OutlookConfidence
  /** /api/status sources (freshness of Pessac and of the stored score) */
  sources?: Partial<Record<SourceFreshnessName, SourceFreshness>> | null
  /** Rain ensembles: undefined = still loading (no line), null = unavailable */
  ensemble?: EnsembleForecast | null
  now: Date
}

/**
 * How sure the verdict is: a list of checkable reasons (fresh Pessac,
 * agreement of the rain ensembles, late sources, "tuned on 5 floods"),
 * never a percentage. The chip is the outlook's own level.
 */
export default function ConfidencePanel({ confidence, sources = null, ensemble, now }: ConfidencePanelProps) {
  const items = confidenceItems(confidence, sources, now.getTime(), ensemble)
  return (
    <section aria-labelledby="confidence-title" className="rounded-card border border-line bg-surface p-4">
      <h3 id="confidence-title" className="flex flex-wrap items-center gap-2 font-display text-[19px] font-semibold tracking-[0.01em]">
        Confiance
        <span className={`rounded-full px-2.5 py-0.5 font-sans text-[12.5px] font-bold text-ink ${CONFIDENCE_CHIP[confidence.level]}`}>
          {CONFIDENCE_LEVEL_LABELS[confidence.level]}
        </span>
      </h3>
      <ul className="mt-2.5 grid gap-2">
        {items.map((item) => (
          <li key={item.lead + item.text} className="grid grid-cols-[18px_minmax(0,1fr)] gap-2 text-sm text-ink-2">
            <ConfidenceIcon kind={item.kind} />
            <span>
              <span className="sr-only">{ICON_LABEL[item.kind]} : </span>
              {item.lead && <b className="font-bold text-ink">{item.lead}</b>}
              {item.lead && ' : '}
              {item.text}
              {item.connectNetatmo && (
                <>
                  {' '}
                  <a href={netatmoAuthUrl()} className="text-accent underline underline-offset-2">
                    Connecter Netatmo
                  </a>
                </>
              )}
            </span>
          </li>
        ))}
      </ul>
    </section>
  )
}
