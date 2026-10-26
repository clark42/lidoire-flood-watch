import type { ReactNode } from 'react'
import { VALUE_KIND_HINTS, VALUE_KIND_LABELS, type ValueKind } from './valueKinds'

const WITH_LINE: ValueKind[] = ['measured', 'estimated', 'model', 'analog']

interface ValueTagProps {
  kind: ValueKind
  /** Replaces the default label */
  children?: ReactNode
  className?: string
}

export default function ValueTag({ kind, children, className = '' }: ValueTagProps) {
  return (
    <span className={`value-tag ${className}`} data-kind={kind} title={VALUE_KIND_HINTS[kind]}>
      {WITH_LINE.includes(kind) && <i aria-hidden="true" />}
      {children ?? VALUE_KIND_LABELS[kind]}
    </span>
  )
}

/** "How to read the values" strip: the whole grammar at once. */
export function ValueTagLegend({ className = '' }: { className?: string }) {
  return (
    <div
      className={`flex flex-wrap items-center gap-x-3.5 gap-y-2 rounded-lg border border-line bg-surface px-3 py-2.5 text-[12.5px] text-ink-2 ${className}`}
      aria-label="Comment lire les valeurs"
      role="note"
    >
      <span className="eyebrow">Lire les valeurs</span>
      {(['measured', 'estimated', 'model', 'analog', 'mockup'] as ValueKind[]).map((k) => (
        <ValueTag key={k} kind={k} />
      ))}
      <span>
        Une bande = incertitude (p10-p90). Sans bande = incertitude inconnue, toujours signalée.
      </span>
    </div>
  )
}
