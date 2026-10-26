import { useEffect, useState } from 'react'
import { ExternalLink, ShieldAlert } from 'lucide-react'
import { getVigilance } from '../api'
import type { VigilanceDepartment, VigilanceLevel, VigilanceStatus } from '../types'
import { formatParis } from '../utils/time'

const REFRESH_MS = 10 * 60 * 1000

// Official colour semantics (Vigicrues and Meteo-France vigilance maps)
const LEVELS: Record<VigilanceLevel, { label: string; bg: string; fg: string }> = {
  1: { label: 'Vert', bg: '#31AA35', fg: '#ffffff' },
  2: { label: 'Jaune', bg: '#FFF600', fg: '#1f2937' },
  3: { label: 'Orange', bg: '#FFB82B', fg: '#1f2937' },
  4: { label: 'Rouge', bg: '#CC0000', fg: '#ffffff' },
}
const UNKNOWN = { label: 'Inconnu', bg: '#e5e7eb', fg: '#4b5563' }

function levelStyle(level: VigilanceLevel | null) {
  return level ? LEVELS[level] : UNKNOWN
}

function maxLevel(levels: (VigilanceLevel | null)[]): VigilanceLevel | null {
  const known = levels.filter((l): l is VigilanceLevel => l !== null)
  return known.length > 0 ? (Math.max(...known) as VigilanceLevel) : null
}

function Badge({
  title,
  level,
  detail,
  href,
}: {
  title: string
  level: VigilanceLevel | null
  detail?: string
  href: string
}) {
  const style = levelStyle(level)
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="flex items-center gap-2 rounded-lg border border-gray-200 bg-white px-2 py-1.5 text-xs hover:bg-gray-50 min-w-0"
      title={`${title} : vigilance ${style.label.toLowerCase()}${detail ? ` (${detail})` : ''}`}
    >
      <span
        className="rounded px-1.5 py-0.5 font-semibold flex-shrink-0"
        style={{ backgroundColor: style.bg, color: style.fg }}
      >
        {style.label}
      </span>
      <span className="text-gray-700 truncate">
        {title}
        {detail && <span className="text-gray-500"> · {detail}</span>}
      </span>
      <ExternalLink className="w-3 h-3 text-gray-400 flex-shrink-0" />
    </a>
  )
}

/** Phenomena above green today (with their first window), then tomorrow's raise. */
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
      parts.push(`${p.label.toLowerCase()} demain (${LEVELS[p.level_tomorrow].label.toLowerCase()})`)
    }
  }
  return parts.length > 0 ? parts.join(', ') : undefined
}

/**
 * Official vigilance badges (Vigicrues troncons, Meteo-France departements),
 * read from GET /api/vigilance (backend cache, refreshed every 30 min).
 * Informative only: the colours do not enter the risk score.
 */
export default function VigilanceBadges() {
  const [data, setData] = useState<VigilanceStatus | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let cancelled = false
    const load = () => {
      getVigilance()
        .then((d) => {
          if (!cancelled) {
            setData(d)
            setFailed(false)
          }
        })
        .catch(() => {
          if (!cancelled) setFailed(true)
        })
    }
    load()
    const timer = setInterval(load, REFRESH_MS)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [])

  if (!data) {
    if (!failed) return null
    return (
      <div className="bg-white rounded-lg shadow px-3 py-2 text-xs text-gray-500">
        Vigilance officielle indisponible.
      </div>
    )
  }

  const { vigicrues, meteofrance } = data
  return (
    <section className="bg-white rounded-lg shadow p-3" aria-label="Vigilance officielle">
      <div className="flex items-center gap-2 mb-2">
        <ShieldAlert className="w-4 h-4 text-gray-500" />
        <span className="text-sm font-medium text-gray-700">Vigilance officielle</span>
        <span className="text-xs text-gray-400 ml-auto">
          {meteofrance.issued_at || vigicrues.issued_at
            ? `maj ${formatParis((vigicrues.issued_at ?? meteofrance.issued_at) as string, 'dd/MM HH:mm')}`
            : 'en attente de collecte'}
        </span>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-2">
        {vigicrues.troncons.map((t) => (
          <Badge
            key={t.code}
            title={`Vigicrues ${t.name}`}
            level={vigicrues.available ? t.level : null}
            detail={t.code}
            href={t.url || vigicrues.url}
          />
        ))}
        {meteofrance.departments.map((d) => (
          <Badge
            key={d.code}
            title={`Météo-France ${d.name} (${d.code})`}
            level={meteofrance.available ? maxLevel(d.phenomena.map((p) => p.level)) : null}
            detail={meteofrance.available ? departmentDetail(d) : undefined}
            href={d.url || meteofrance.url}
          />
        ))}
      </div>
      <p className="mt-2 text-[11px] text-gray-400">
        Information officielle (crues, pluie-inondation, orages, vent) : elle n'entre pas dans le score de risque.
      </p>
    </section>
  )
}
