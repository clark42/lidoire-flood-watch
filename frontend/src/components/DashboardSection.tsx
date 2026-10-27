import { useEffect, useState, type ReactNode } from 'react'
import { ChevronDown } from 'lucide-react'
import { sectionDef, type DashboardSectionId } from '../pages/dashboardSections'
import { browserSectionOpen, OPEN_SECTION_EVENT } from '../utils/sectionDisclosure'

/**
 * A Dashboard section: anchor of the section bar, heading, short intro.
 * A `collapsible` section (long, secondary: see dashboardSections.ts) is a
 * disclosure: its heading is a button (aria-expanded / aria-controls),
 * collapsed by default under 640 px, open on desktop; the hash (#id) and the
 * section bar open it.
 */
export default function DashboardSection({
  id,
  children,
  className = '',
  defaultOpen,
}: {
  id: DashboardSectionId
  children: ReactNode
  className?: string
  /** Initial state of a collapsible section (default: from the viewport and the hash) */
  defaultOpen?: boolean
}) {
  const def = sectionDef(id)
  const collapsible = !!def.collapsible && !!def.title
  const titleId = `${id}-title`
  const contentId = `${id}-content`
  const [open, setOpen] = useState(() => !collapsible || (defaultOpen ?? browserSectionOpen(id)))

  useEffect(() => {
    if (!collapsible) return
    const openIfTarget = (target: string) => {
      if (target !== id) return
      setOpen(true)
      // Once expanded, the content pushes the page: land on the heading again
      requestAnimationFrame(() => document.getElementById(id)?.scrollIntoView())
    }
    const onHash = () => openIfTarget(window.location.hash.slice(1))
    const onRequest = (e: Event) => openIfTarget((e as CustomEvent<string>).detail)
    window.addEventListener('hashchange', onHash)
    window.addEventListener(OPEN_SECTION_EVENT, onRequest)
    return () => {
      window.removeEventListener('hashchange', onHash)
      window.removeEventListener(OPEN_SECTION_EVENT, onRequest)
    }
  }, [collapsible, id])

  return (
    <section
      id={id}
      aria-labelledby={def.title ? titleId : undefined}
      aria-label={def.title ? undefined : def.label}
      data-collapsible={collapsible || undefined}
      data-open={collapsible ? open : undefined}
      // Leaves room for the sticky section bar when jumping to the anchor
      className={`min-w-0 scroll-mt-[68px] pt-8 ${className}`}
    >
      {def.title && (
        <header className="mb-3">
          <h2 id={titleId} className="font-display text-[26px] font-semibold leading-[1.1] tracking-[0.01em]">
            {collapsible ? (
              <button
                type="button"
                aria-expanded={open}
                aria-controls={contentId}
                onClick={() => setOpen((v) => !v)}
                className="flex min-h-[44px] w-full items-center justify-between gap-3 rounded-lg text-left hover:text-accent"
              >
                <span>{def.title}</span>
                <span
                  aria-hidden="true"
                  className="inline-flex flex-none items-center gap-1 font-sans text-[14px] font-normal tracking-normal text-accent"
                >
                  {open ? 'Replier' : 'Afficher'}
                  <ChevronDown className={`h-4 w-4 transition-transform ${open ? 'rotate-180' : ''}`} />
                </span>
              </button>
            ) : (
              def.title
            )}
          </h2>
          {def.description && <p className="mt-1 max-w-[70ch] text-ink-2">{def.description}</p>}
        </header>
      )}
      <div id={contentId} hidden={!open} className="min-w-0 space-y-4">
        {children}
      </div>
    </section>
  )
}
