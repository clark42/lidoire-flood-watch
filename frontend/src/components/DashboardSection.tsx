import type { ReactNode } from 'react'
import { sectionDef, type DashboardSectionId } from '../pages/dashboardSections'

/** A Dashboard section: anchor of the section bar, heading, short intro. */
export default function DashboardSection({
  id,
  children,
  className = '',
}: {
  id: DashboardSectionId
  children: ReactNode
  className?: string
}) {
  const def = sectionDef(id)
  const titleId = `${id}-title`
  return (
    <section
      id={id}
      aria-labelledby={def.title ? titleId : undefined}
      aria-label={def.title ? undefined : def.label}
      // Leaves room for the sticky section bar when jumping to the anchor
      className={`min-w-0 scroll-mt-[68px] pt-8 ${className}`}
    >
      {def.title && (
        <header className="mb-3">
          <h2 id={titleId} className="font-display text-[26px] font-semibold leading-[1.1] tracking-[0.01em]">
            {def.title}
          </h2>
          {def.description && <p className="mt-1 max-w-[70ch] text-ink-2">{def.description}</p>}
        </header>
      )}
      <div className="min-w-0 space-y-4">{children}</div>
    </section>
  )
}
