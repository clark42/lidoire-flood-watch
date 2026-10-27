import type { SourceFreshnessState, SystemStatus } from '../types'
import { netatmoAuthUrl } from '../api'
import { groupSources, STATUS_LABELS, statusCounts } from '../utils/sourceHealth'
import '../styles/field.css'

/** Status shape (never colour alone): disc, triangle, square, dashed ring. */
export function StatusMark({ status }: { status: SourceFreshnessState }) {
  return (
    <svg viewBox="0 0 12 12" className="shealth-mark" aria-hidden="true">
      {status === 'ok' && <circle cx="6" cy="6" r="5" style={{ fill: 'var(--lv-green)' }} />}
      {status === 'stale' && <path d="M6,1L11.2,10.5H0.8Z" style={{ fill: 'var(--lv-orange)' }} />}
      {status === 'unavailable' && <rect x="1.5" y="1.5" width="9" height="9" style={{ fill: 'var(--lv-red)' }} />}
      {status === 'unknown' && (
        <circle cx="6" cy="6" r="4.5" fill="none" strokeWidth="1.5" strokeDasharray="2 2" style={{ stroke: 'var(--ink-3)' }} />
      )}
    </svg>
  )
}

interface SourceHealthProps {
  sources: SystemStatus['sources'] | null | undefined
  now: number
  loading?: boolean
}

/** /api/status.sources, grouped critical / model inputs / indicators / probabilistic, with freshness. */
export default function SourceHealth({ sources, now, loading = false }: SourceHealthProps) {
  if (!sources) {
    return (
      <div className="rounded-card border border-line bg-surface p-4 text-ink-2">
        {loading ? 'Chargement de l’état des sources…' : 'État des sources indisponible.'}
      </div>
    )
  }
  const groups = groupSources(sources, now)
  const counts = statusCounts(groups)
  const problems = counts.stale + counts.unavailable

  return (
    <div className="rounded-card border border-line bg-surface p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="font-display text-[19px] font-semibold tracking-[0.01em]">Sources</h3>
        <p className="text-[13px] text-ink-2" role="status">
          {(['ok', 'stale', 'unavailable', 'unknown'] as const)
            .filter((s) => counts[s] > 0)
            .map((s) => `${counts[s]} ${STATUS_LABELS[s]}`)
            .join(' · ')}
        </p>
      </div>
      {problems > 0 && (
        <p className="mt-1 text-[13px] text-ink-2">
          Une source en retard réduit la confiance affichée en haut de page ; seules les sources critiques dégradent le
          tableau de bord.
        </p>
      )}
      <div className="mt-2 grid grid-cols-1 gap-x-6 min-[960px]:grid-cols-2">
        {groups.map((g) => (
          <section key={g.id} aria-labelledby={`shealth-${g.id}`} className="mt-2 min-w-0">
            <h4 id={`shealth-${g.id}`} className="eyebrow">
              {g.title}
            </h4>
            <p className="text-[12px] text-ink-3">{g.hint}</p>
            <ul className="mt-1">
              {g.items.map((i) => (
                <li
                  key={i.key}
                  className="grid grid-cols-[14px_minmax(0,1fr)_auto] items-center gap-x-2.5 border-b border-line py-2 text-[13.5px] last:border-b-0"
                >
                  <StatusMark status={i.status} />
                  <span className="min-w-0">
                    {i.label}
                    {i.note && <span className="block text-[12px] text-ink-3">{i.note}</span>}
                    {i.connectNetatmo && (
                      <a href={netatmoAuthUrl()} className="block text-[12.5px] text-accent underline underline-offset-2">
                        Connecter Netatmo
                      </a>
                    )}
                  </span>
                  <span className="text-right text-[12.5px] text-ink-2">
                    <span className={i.status === 'ok' ? undefined : 'font-bold text-ink'}>{STATUS_LABELS[i.status]}</span>
                    <span className="block font-mono text-[11.5px] text-ink-3">{i.freshness}</span>
                  </span>
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
    </div>
  )
}
