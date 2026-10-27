import { useEffect, useRef, useState } from 'react'
import type { DashboardSectionDef } from '../pages/dashboardSections'
import { requestOpenSection } from '../utils/sectionDisclosure'

/**
 * Sticky, horizontally scrollable bar of the Dashboard sections. The link
 * of the section crossing the middle of the viewport is highlighted and
 * scrolled into view inside the bar (never the page).
 */
export default function SectionNav({ sections }: { sections: readonly DashboardSectionDef[] }) {
  const [active, setActive] = useState(sections[0]?.id)
  const navRef = useRef<HTMLElement>(null)

  // Deep link (/#agir): the sections render after the data, too late for
  // the browser's own jump to the anchor
  useEffect(() => {
    const id = window.location.hash.slice(1)
    if (id && sections.some((s) => s.id === id)) document.getElementById(id)?.scrollIntoView()
  }, [sections])

  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined') return
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) if (entry.isIntersecting) setActive(entry.target.id)
      },
      { rootMargin: '-45% 0px -50% 0px' },
    )
    for (const s of sections) {
      const el = document.getElementById(s.id)
      if (el) observer.observe(el)
    }
    return () => observer.disconnect()
  }, [sections])

  useEffect(() => {
    const nav = navRef.current
    const link = nav?.querySelector<HTMLAnchorElement>(`a[href="#${active}"]`)
    if (!nav || !link || typeof nav.scrollTo !== 'function') return
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    nav.scrollTo({ left: link.offsetLeft - 16, behavior: reduce ? 'auto' : 'smooth' })
  }, [active])

  return (
    <nav
      ref={navRef}
      aria-label="Sections du tableau de bord"
      className="section-nav page-gutter sticky top-0 z-30 flex gap-1.5 overflow-x-auto border-b border-line py-2"
    >
      {sections.map((s) => {
        const on = s.id === active
        return (
          <a
            key={s.id}
            href={`#${s.id}`}
            aria-current={on ? 'location' : undefined}
            onClick={() => {
              setActive(s.id)
              // A collapsed section opens (also when the hash is already #id)
              requestOpenSection(s.id)
            }}
            className={`inline-flex min-h-[44px] flex-none items-center rounded-full px-3 text-[13.5px] sm:min-h-[34px] ${
              on ? 'bg-ink text-canvas' : 'text-ink-2 hover:bg-surface-2 hover:text-ink'
            }`}
          >
            {s.label}
          </a>
        )
      })}
    </nav>
  )
}
