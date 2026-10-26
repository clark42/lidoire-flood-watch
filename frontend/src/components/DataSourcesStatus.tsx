import { useState } from 'react'
import {
  Database,
  ChevronDown,
  ChevronUp,
  CheckCircle,
  XCircle,
  AlertCircle,
  HelpCircle,
} from 'lucide-react'
import type { SourceFreshness, SourceFreshnessName, SourceFreshnessState } from '../types'
import { formatAge, formatParis, parseApiDate } from '../utils/time'

// Single source of truth: /api/status.sources (freshness of the last stored point)
interface DataSourcesStatusProps {
  sources?: Partial<Record<SourceFreshnessName, SourceFreshness>> | null
  /** Next high water at the confluence (/api/realtime tide), for the tide row */
  nextHighTide?: string | null
  now?: number
  loading?: boolean
}

const STATUS_ICONS: Record<SourceFreshnessState, JSX.Element> = {
  ok: <CheckCircle className="w-4 h-4 text-green-500" />,
  stale: <AlertCircle className="w-4 h-4 text-yellow-500" />,
  unavailable: <XCircle className="w-4 h-4 text-red-500" />,
  unknown: <HelpCircle className="w-4 h-4 text-gray-400" />,
}

const STATUS_COLORS: Record<SourceFreshnessState, string> = {
  ok: 'border-green-200 bg-green-50',
  stale: 'border-yellow-200 bg-yellow-50',
  unavailable: 'border-red-200 bg-red-50',
  unknown: 'border-gray-200 bg-gray-50',
}

const STATUS_LABELS: Record<SourceFreshnessState, string> = {
  ok: 'a jour',
  stale: 'perime',
  unavailable: 'indisponible',
  unknown: 'inconnu',
}

/** "dans 1 h 32" (future) */
function formatUntil(target: Date, now: number): string {
  const minutes = Math.max(0, Math.round((target.getTime() - now) / 60_000))
  if (minutes < 60) return `dans ${minutes} min`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest > 0 ? `dans ${hours} h ${rest.toString().padStart(2, '0')}` : `dans ${hours} h`
}

/**
 * Freshness text. The tide source stores future extremes (its age is negative):
 * show the time to the next high water instead of an age.
 */
function freshnessText(
  name: string,
  source: SourceFreshness,
  nextHighTide: string | null | undefined,
  now: number,
): string {
  if (name === 'tide') {
    const pm = parseApiDate(nextHighTide)
    if (pm && pm.getTime() > now) {
      return `prochaine PM ${formatUntil(pm, now)} (${formatParis(pm, 'HH:mm')})`
    }
  }
  const last = parseApiDate(source.last_timestamp)
  if (!last) return source.status === 'unknown' ? 'non verifie' : 'aucune donnee (30 j)'
  if (last.getTime() > now) return `donnees jusqu'au ${formatParis(last, 'dd/MM HH:mm')}`
  return `derniere donnee ${formatAge(last, now)}`
}

export default function DataSourcesStatus({
  sources,
  nextHighTide,
  now = Date.now(),
  loading,
}: DataSourcesStatusProps) {
  const [expanded, setExpanded] = useState(false)

  if (loading && !sources) {
    return (
      <div className="bg-white rounded-lg shadow p-3">
        <div className="animate-pulse flex items-center gap-2">
          <div className="h-4 w-4 bg-gray-200 rounded-full"></div>
          <div className="h-4 bg-gray-200 rounded w-32"></div>
        </div>
      </div>
    )
  }

  const entries = Object.entries(sources ?? {}).filter(
    (e): e is [string, SourceFreshness] => e[1] != null
  )
  const count = (state: SourceFreshnessState) => entries.filter(([, s]) => s.status === state).length
  const okCount = count('ok')
  const staleCount = count('stale')
  const errorCount = count('unavailable')
  const unknownCount = count('unknown')

  return (
    <div className="bg-white rounded-lg shadow overflow-hidden">
      {/* Compact header */}
      <div
        className="flex items-center justify-between p-3 cursor-pointer hover:bg-gray-50"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex items-center gap-3">
          <Database className="w-4 h-4 text-gray-500" />
          <span className="text-sm font-medium text-gray-700">Sources de donnees</span>
          <div className="flex items-center gap-2">
            {okCount > 0 && (
              <span className="flex items-center gap-1 text-xs text-green-600">
                <CheckCircle className="w-3 h-3" />
                {okCount}
              </span>
            )}
            {staleCount > 0 && (
              <span className="flex items-center gap-1 text-xs text-yellow-600">
                <AlertCircle className="w-3 h-3" />
                {staleCount}
              </span>
            )}
            {errorCount > 0 && (
              <span className="flex items-center gap-1 text-xs text-red-600">
                <XCircle className="w-3 h-3" />
                {errorCount}
              </span>
            )}
            {unknownCount > 0 && (
              <span className="flex items-center gap-1 text-xs text-gray-500">
                <HelpCircle className="w-3 h-3" />
                {unknownCount}
              </span>
            )}
            {entries.length === 0 && (
              <span className="text-xs text-gray-400">statut indisponible</span>
            )}
          </div>
        </div>
        {expanded ? (
          <ChevronUp className="w-4 h-4 text-gray-400" />
        ) : (
          <ChevronDown className="w-4 h-4 text-gray-400" />
        )}
      </div>

      {/* Expanded details */}
      {expanded && (
        <div className="border-t p-3 space-y-2">
          {entries.map(([name, source]) => (
            <div
              key={name}
              className={`p-3 rounded border ${STATUS_COLORS[source.status] ?? STATUS_COLORS.unknown}`}
            >
              <div className="flex items-start justify-between gap-2">
                <div className="flex items-center gap-2">
                  {STATUS_ICONS[source.status] ?? STATUS_ICONS.unknown}
                  <div>
                    <div className="font-medium text-sm">
                      {source.label}
                      {source.critical && (
                        <span className="ml-1 text-xs text-gray-400">(critique)</span>
                      )}
                    </div>
                    <div className="text-xs text-gray-600">
                      {freshnessText(name, source, nextHighTide, now)}
                    </div>
                  </div>
                </div>
                <span className="text-xs text-gray-500 shrink-0">
                  {STATUS_LABELS[source.status] ?? source.status}
                </span>
              </div>
              {(source.error ?? source.note) && (
                <div className={`mt-2 text-xs ${source.error ? 'text-red-600' : 'text-gray-500'}`}>
                  {source.error ?? source.note}
                </div>
              )}
              {source.max_age_hours > 0 && (
                <div className="mt-1 text-xs text-gray-400">
                  Perime au-dela de {source.max_age_hours} h
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
