import type { ReactNode } from 'react'
import { RefreshCw } from 'lucide-react'
import type {
  OutlookHorizon,
  RiskLevel,
  RiskOutlook,
  SourceFreshness,
  SourceFreshnessName,
} from '../types'
import LevelMeter from './ui/LevelMeter'
import ValueTag from './ui/ValueTag'
import InfoTooltip from './InfoTooltip'
import { TOOLTIPS } from '../tooltipContent'
import { buildVerdict } from '../utils/levels'
import { CONFIDENCE_LEVEL_LABELS, confidenceItems, type ConfidenceKind } from '../utils/confidence'
import { formatNumber, RISK_LEVEL_LABELS } from '../utils/format'
import { formatParis, formatParisRelative } from '../utils/time'

// Literal class names (Tailwind only generates classes it can read)
const STRIPE: Record<RiskLevel, string> = {
  green: 'bg-lv-green',
  yellow: 'bg-lv-yellow',
  orange: 'bg-lv-orange',
  red: 'bg-lv-red',
}
const CONFIDENCE_CHIP: Record<RiskOutlook['confidence']['level'], string> = {
  good: 'bg-lv-green-soft',
  reduced: 'bg-lv-yellow-soft',
  low: 'bg-lv-red-soft',
}

export interface VerdictHeroProps {
  outlook: RiskOutlook | null
  loading?: boolean
  error?: string | null
  onRetry?: () => void
  /** Realtime score (the "now" of the verdict) */
  current?: { score: number; level: RiskLevel } | null
  /** /api/status sources (freshness of Pessac and of the stored score) */
  sources?: Partial<Record<SourceFreshnessName, SourceFreshness>> | null
  /** Official vigilances, under the confidence block */
  vigilance?: ReactNode
  now?: Date
}

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

const ICON_LABEL: Record<ConfidenceKind, string> = { ok: 'Fiable', warn: 'Attention', info: 'À savoir' }

function HorizonTile({ horizon, now }: { horizon: OutlookHorizon; now: Date }) {
  return (
    <div className="min-w-0 rounded-lg border border-line bg-canvas p-2.5">
      <div className="eyebrow text-[11.5px]">D&apos;ici {horizon.label}</div>
      <span className="num mt-0.5 block text-[30px] font-semibold leading-[1.05]">
        {formatNumber(horizon.max_score, 0)}
        <span className="text-[0.5em] text-ink-3"> /100</span>
      </span>
      <LevelMeter level={horizon.level} className="mt-1 text-xs" />
      <div className="mt-0.5 text-xs text-ink-3">
        {horizon.max_hours_ahead === 0
          ? 'max maintenant'
          : `pic ${formatParisRelative(horizon.max_at, now, { compact: true })}`}
      </div>
    </div>
  )
}

function HorizonDetails({ horizons }: { horizons: OutlookHorizon[] }) {
  return (
    <details className="mt-3 text-sm">
      <summary className="inline-flex min-h-[44px] cursor-pointer items-center text-accent underline-offset-2 hover:underline sm:min-h-0">
        Détail par horizon
      </summary>
      <div className="mt-2 grid gap-3 sm:grid-cols-3">
        {horizons.map((h) => (
          <div key={h.hours} className="min-w-0">
            <div className="eyebrow">D&apos;ici {h.label}</div>
            {h.drivers.length === 0 ? (
              <p className="mt-1 text-ink-2">Aucun facteur notable.</p>
            ) : (
              <ul className="mt-1 space-y-1">
                {h.drivers.map((d) => (
                  <li key={d.factor} className="text-ink-2">
                    <span className="font-bold text-ink">{d.label}</span>{' '}
                    <span className="num">(+{formatNumber(d.points, 0)} pts)</span>
                    <div className="text-xs">{d.text}</div>
                  </li>
                ))}
              </ul>
            )}
            {h.modifiers.length > 0 && (
              <ul className="mt-1.5 space-y-0.5 text-xs text-ink-3">
                {h.modifiers.map((m) => <li key={m}>{m}</li>)}
              </ul>
            )}
          </div>
        ))}
      </div>
    </details>
  )
}

/**
 * Top of the Dashboard: is there anything to do, when, why, how sure.
 * The verdict is the worst level between now and 72 h (/api/risk/outlook
 * plus the realtime score); the confidence is a list of checkable reasons.
 */
export default function VerdictHero({
  outlook,
  loading = false,
  error = null,
  onRetry,
  current = null,
  sources = null,
  vigilance,
  now = new Date(),
}: VerdictHeroProps) {
  const currentMeter = current && (
    <LevelMeter
      level={current.level}
      className="text-[13px]"
      label={`Maintenant : ${RISK_LEVEL_LABELS[current.level]} · score ${formatNumber(current.score, 0)}/100`}
    />
  )

  if (!outlook) {
    return (
      <div className="rounded-[14px] border border-line bg-surface p-4 sm:p-5" aria-busy={loading || undefined}>
        <div className="flex flex-wrap items-center justify-between gap-2.5">
          <span className="eyebrow">Décision</span>
          {currentMeter}
        </div>
        {loading ? (
          <div className="mt-3 space-y-3" aria-label="Chargement de la prévision">
            <div className="h-10 w-2/3 animate-pulse rounded bg-surface-2" />
            <div className="grid grid-cols-3 gap-2">
              {[0, 1, 2].map((i) => <div key={i} className="h-24 animate-pulse rounded-lg bg-surface-2" />)}
            </div>
          </div>
        ) : (
          <div role="alert" className="mt-3 flex flex-wrap items-center gap-3 text-ink-2">
            <span className="flex-1">Prévision 6 h / 24 h / 72 h indisponible{error ? ` : ${error}` : ''}.</span>
            {onRetry && (
              <button
                type="button"
                onClick={onRetry}
                className="inline-flex min-h-[44px] items-center gap-1.5 rounded-lg border border-line-2 bg-surface px-3.5 text-sm text-ink hover:border-ink-3"
              >
                <RefreshCw className="h-4 w-4" aria-hidden="true" /> Réessayer
              </button>
            )}
          </div>
        )}
      </div>
    )
  }

  const verdict = buildVerdict(outlook, current?.level ?? null, now)
  const tide = outlook.next_high_tide
  const items = confidenceItems(outlook.confidence, sources, now.getTime())
  const actions = outlook.checklist.actions.length

  return (
    <div className="grid gap-4 min-[960px]:grid-cols-[minmax(0,1.25fr)_minmax(0,1fr)] min-[960px]:items-stretch">
      <div className="relative min-w-0 overflow-hidden rounded-[14px] border border-line bg-surface p-4 pl-5 sm:p-5 sm:pl-6">
        <span aria-hidden="true" className={`absolute inset-y-0 left-0 w-1.5 ${STRIPE[verdict.level]}`} />
        <div className="flex flex-wrap items-center justify-between gap-2.5">
          <span className="inline-flex items-center gap-1.5">
            <span className="eyebrow">Décision</span>
            <InfoTooltip {...TOOLTIPS.outlook} />
          </span>
          {currentMeter}
        </div>

        <h2
          id="verdict-title"
          className="mt-2.5 font-display text-[clamp(40px,9vw,64px)] font-bold leading-[0.95] tracking-[0.005em] [text-wrap:balance]"
        >
          {verdict.title}
          <span className="sr-only"> : </span>
          <span className="mt-1.5 block text-[0.42em] font-semibold tracking-[0.01em] text-ink-2">
            {verdict.detail}
          </span>
        </h2>
        <p className="mt-3 max-w-[56ch] text-[16.5px] text-ink-2">
          {verdict.why.lead}{' '}
          {verdict.why.reasons.map((r, i) => (
            <span key={r}>
              <b className="font-bold text-ink">{r}</b>
              {i < verdict.why.reasons.length - 1 ? ' ; ' : '.'}
            </span>
          ))}
        </p>

        <div className="mt-4 grid grid-cols-3 gap-2">
          {outlook.horizons.map((h) => <HorizonTile key={h.hours} horizon={h} now={now} />)}
        </div>

        <div className="mt-3 space-y-1.5 text-[13px] text-ink-3">
          <p>
            <ValueTag kind="model" /> Max horaire du score v{outlook.model_version ?? '?'} prévu,{' '}
            <b className="font-bold text-ink-2">sans bande</b> : l&apos;incertitude du score n&apos;est pas encore calculée.
            Prévision de <span className="font-mono">{formatParis(outlook.forecast_generated_at, 'HH:mm')}</span>.
          </p>
          {tide && (
            <p>
              <ValueTag kind={tide.estimated ? 'estimated' : 'model'} /> Prochaine pleine mer estimée à la confluence :{' '}
              <span className="text-ink-2">{formatParisRelative(tide.timestamp, now)}</span>
              {tide.coefficient != null && <>, coefficient <span className="num">{tide.coefficient}</span></>}.
            </p>
          )}
          {error && (
            <p role="alert">
              Actualisation impossible ({error}) : valeurs de{' '}
              <span className="font-mono">{formatParis(outlook.generated_at, 'HH:mm')}</span>.
            </p>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-x-5">
          <HorizonDetails horizons={outlook.horizons} />
          {actions > 0 && (
            <a
              href="#agir"
              className="mt-3 inline-flex min-h-[44px] items-center text-sm text-accent underline underline-offset-2 sm:min-h-0"
            >
              Liste du moulin : {actions} action{actions > 1 ? 's' : ''}
            </a>
          )}
        </div>
      </div>

      <div className="grid min-w-0 content-start gap-3">
        <section aria-labelledby="confidence-title" className="rounded-card border border-line bg-surface p-4">
          <h3 id="confidence-title" className="flex flex-wrap items-center gap-2 font-display text-[19px] font-semibold tracking-[0.01em]">
            Confiance
            <span className={`rounded-full px-2.5 py-0.5 font-sans text-[12.5px] font-bold text-ink ${CONFIDENCE_CHIP[outlook.confidence.level]}`}>
              {CONFIDENCE_LEVEL_LABELS[outlook.confidence.level]}
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
                </span>
              </li>
            ))}
          </ul>
        </section>
        {vigilance}
      </div>
    </div>
  )
}
