import { ExternalLink } from 'lucide-react'
import type { RiskLevel, VigilanceDepartment, VigilanceLevel, VigilanceStatus } from '../types'
import { useVigilance } from '../hooks/useSharedResource'
import { formatParis, formatParisRelative } from '../utils/time'
import LevelMeter from './ui/LevelMeter'

// Official levels 1-4 share the Vigicrues vocabulary of the LevelMeter
const AS_LEVEL: Record<VigilanceLevel, RiskLevel> = { 1: 'green', 2: 'yellow', 3: 'orange', 4: 'red' }
const WORD: Record<VigilanceLevel, string> = { 1: 'vert', 2: 'jaune', 3: 'orange', 4: 'rouge' }

function maxVigilance(levels: (VigilanceLevel | null)[]): VigilanceLevel | null {
  const known = levels.filter((l): l is VigilanceLevel => l !== null)
  return known.length > 0 ? (Math.max(...known) as VigilanceLevel) : null
}

/** Phenomena above green today (with their first window), then tomorrow's raises. */
function departmentDetail(dept: VigilanceDepartment): string | undefined {
  const parts: string[] = []
  for (const p of dept.phenomena) {
    if (p.level && p.level >= 2) {
      const window = p.windows.find((w) => w.level === p.level)
      parts.push(
        window
          ? `${p.label.toLowerCase()} ${formatParis(window.begin, 'HH:mm')}-${formatParis(window.end, 'HH:mm')}`
          : p.label.toLowerCase(),
      )
    } else if (p.level_tomorrow && p.level_tomorrow >= 2) {
      parts.push(`${p.label.toLowerCase()} demain (${WORD[p.level_tomorrow]})`)
    }
  }
  return parts.length > 0 ? parts.join(', ') : undefined
}

interface Row {
  key: string
  source: string
  name: string
  level: VigilanceLevel | null
  detail?: string
  href: string
}

function VigilanceRow({ row }: { row: Row }) {
  return (
    <li className="border-b border-line last:border-b-0">
      <a
        href={row.href}
        target="_blank"
        rel="noopener noreferrer"
        className="grid min-h-[44px] grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-0.5 py-1.5 hover:bg-surface-2"
      >
        <span className="min-w-0">
          <span className="block text-[12px] text-ink-3">{row.source}</span>
          <span className="block text-sm text-ink">
            {row.name}
            {row.detail && <span className="text-ink-2"> · {row.detail}</span>}
          </span>
        </span>
        <span className="inline-flex items-center gap-2">
          {row.level ? (
            <LevelMeter level={AS_LEVEL[row.level]} className="text-[12.5px]" />
          ) : (
            <span className="text-[12.5px] text-ink-3">inconnu</span>
          )}
          <ExternalLink className="h-3.5 w-3.5 flex-none text-ink-3" aria-hidden="true" />
          <span className="sr-only">(ouvre le site officiel)</span>
        </span>
      </a>
    </li>
  )
}

/**
 * Official vigilances (Vigicrues sections, Meteo-France departments), as
 * level pips + word (never the colour alone). Informative only: they do not
 * enter the risk score.
 */
export function VigilancePanel({ data, failed = false, now = new Date() }: { data: VigilanceStatus | null; failed?: boolean; now?: Date }) {
  if (!data) {
    if (!failed) return null
    return (
      <p className="rounded-card border border-line bg-surface px-4 py-3 text-sm text-ink-2" role="status">
        Vigilance officielle indisponible.
      </p>
    )
  }
  const { vigicrues, meteofrance } = data
  const rows: Row[] = [
    ...vigicrues.troncons.map((t) => ({
      key: `vc-${t.code}`,
      source: `Vigicrues · ${t.code}`,
      name: t.name,
      level: vigicrues.available ? t.level : null,
      href: t.url || vigicrues.url,
    })),
    ...meteofrance.departments.map((d) => ({
      key: `mf-${d.code}`,
      source: `Météo-France · ${d.code}`,
      name: d.name,
      level: meteofrance.available ? maxVigilance(d.phenomena.map((p) => p.level)) : null,
      detail: meteofrance.available ? departmentDetail(d) : undefined,
      href: d.url || meteofrance.url,
    })),
  ]
  // Each service publishes on its own schedule: both times are shown
  const issued = [
    { source: 'Vigicrues', at: vigicrues.issued_at },
    { source: 'Météo-France', at: meteofrance.issued_at },
  ].filter((i): i is { source: string; at: string } => !!i.at)
  return (
    <section aria-labelledby="vigilance-title" className="rounded-card border border-line bg-surface p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 id="vigilance-title" className="font-display text-[19px] font-semibold tracking-[0.01em]">
          Vigilances officielles
        </h3>
        <span className="font-mono text-[12px] text-ink-3" data-testid="vigilance-issued">
          {issued.length > 0
            ? issued.map((i) => `${i.source} ${formatParisRelative(i.at, now)}`).join(' · ')
            : 'en attente de collecte'}
        </span>
      </div>
      <ul className="mt-1.5">
        {rows.map((r) => (
          <VigilanceRow key={r.key} row={r} />
        ))}
      </ul>
      <p className="mt-2 text-[12px] text-ink-3">
        Crues, pluie-inondation, orages, vent : information officielle, hors score. Elle ne voit pas la Lidoire.
      </p>
    </section>
  )
}

/** Container (slot of the VerdictHero): GET /api/vigilance, shared, every 10 min. */
export default function VigilanceBadges() {
  const vigilance = useVigilance()
  return <VigilancePanel data={vigilance.data} failed={!!vigilance.error && !vigilance.data} />
}
