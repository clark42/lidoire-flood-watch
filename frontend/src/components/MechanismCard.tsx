import { MECHANISM_STATE_LABELS, type MechanismState, type MechanismView } from '../utils/mechanisms'
import PercentileRail from './ui/PercentileRail'
import ValueTag from './ui/ValueTag'

/** State of a mechanism: shape + word (circle calm, triangle watch, square active). */
export function StateChip({ state, rule }: { state: MechanismState; rule?: string }) {
  const icon =
    state === 'calm' ? (
      <circle cx="5" cy="5" r="3.8" fill="none" stroke="currentColor" strokeWidth="1.6" />
    ) : state === 'watch' ? (
      <path d="M5 1l4.2 8H.8z" fill="var(--lv-yellow)" stroke="var(--ink)" strokeWidth="0.8" strokeLinejoin="round" />
    ) : state === 'active' ? (
      <rect x="1" y="1" width="8" height="8" fill="var(--lv-red)" stroke="var(--ink)" strokeWidth="0.8" />
    ) : (
      <circle cx="5" cy="5" r="3.8" fill="none" stroke="currentColor" strokeWidth="1.3" strokeDasharray="1.8 1.4" />
    )
  return (
    <span
      className="inline-flex flex-none items-center gap-1.5 rounded-full border border-line-2 bg-surface px-2.5 py-0.5 text-[12.5px] font-bold text-ink"
      title={rule}
      data-state={state}
    >
      <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true" className="text-ink-3">
        {icon}
      </svg>
      {MECHANISM_STATE_LABELS[state]}
    </span>
  )
}

/**
 * One way the mill floods: the value now (with its honesty tag), a rail
 * placing it among the past labelled episodes, the physical reading and the
 * figures behind it.
 */
export default function MechanismCard({ view, primary = false }: { view: MechanismView; primary?: boolean }) {
  const titleId = `mechanism-${view.id}`
  return (
    <article
      aria-labelledby={titleId}
      className={`flex min-w-0 flex-col gap-3 rounded-card border bg-surface p-4 ${primary ? 'border-ink-3' : 'border-line'}`}
      data-mechanism={view.id}
    >
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="eyebrow">{view.role}</div>
          <h3 id={titleId} className="font-display text-[20px] font-semibold leading-[1.1] tracking-[0.01em]">
            {view.title}
          </h3>
        </div>
        <StateChip state={view.state} rule={view.stateRule} />
      </header>

      <div>
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          <span className="num text-[38px] font-semibold leading-none">{view.valueText}</span>
          <span className="font-display text-[18px] text-ink-2">{view.unit}</span>
          <ValueTag kind={view.kind} />
        </div>
        <p className="mt-1 text-[13px] text-ink-3">{view.valueNote}</p>
      </div>

      <p className="text-[14.5px] text-ink-2">{view.lead}</p>

      {view.rails.map((r) => (
        <figure key={r.title} className="m-0 min-w-0">
          <figcaption className="text-[13px] font-bold text-ink">{r.title}</figcaption>
          <PercentileRail spec={r.spec} />
          <p className="text-[13px] text-ink-2">{r.comparison}</p>
          <p className="text-[12px] text-ink-3">{r.basis}</p>
        </figure>
      ))}

      <p className="text-[12.5px] text-ink-3">
        <span className="font-bold text-ink-2">Règle : </span>
        {view.stateRule}
      </p>

      {view.facts.length > 0 && (
        <dl className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-x-3 gap-y-1.5 border-t border-line pt-2 text-[13px]">
          {view.facts.map((f) => (
            <div key={f.label} className="contents">
              <dt className="text-ink-2">
                {f.label}
                {f.kind && <ValueTag kind={f.kind} className="ml-1.5 align-middle text-[11px]" />}
              </dt>
              <dd className="m-0 text-right text-ink">
                <span className="num text-[14px]">{f.value}</span>
              </dd>
            </div>
          ))}
        </dl>
      )}

      <p className="mt-auto font-mono text-[11.5px] leading-snug text-ink-3">{view.source}</p>
    </article>
  )
}
