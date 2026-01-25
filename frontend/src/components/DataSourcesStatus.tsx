import { useState } from 'react'
import {
  Database,
  ChevronDown,
  ChevronUp,
  CheckCircle,
  XCircle,
  AlertCircle,
  ExternalLink,
} from 'lucide-react'

interface DataSource {
  name: string
  status: 'ok' | 'degraded' | 'error'
  description: string
  lastUpdate?: string
  details?: string
  url?: string
}

interface DataSourcesStatusProps {
  sources: DataSource[]
  loading?: boolean
}

const STATUS_ICONS = {
  ok: <CheckCircle className="w-4 h-4 text-green-500" />,
  degraded: <AlertCircle className="w-4 h-4 text-yellow-500" />,
  error: <XCircle className="w-4 h-4 text-red-500" />,
}

const STATUS_COLORS = {
  ok: 'border-green-200 bg-green-50',
  degraded: 'border-yellow-200 bg-yellow-50',
  error: 'border-red-200 bg-red-50',
}

export default function DataSourcesStatus({
  sources,
  loading,
}: DataSourcesStatusProps) {
  const [expanded, setExpanded] = useState(false)

  if (loading) {
    return (
      <div className="bg-white rounded-lg shadow p-3">
        <div className="animate-pulse flex items-center gap-2">
          <div className="h-4 w-4 bg-gray-200 rounded-full"></div>
          <div className="h-4 bg-gray-200 rounded w-32"></div>
        </div>
      </div>
    )
  }

  const okCount = sources.filter((s) => s.status === 'ok').length
  const degradedCount = sources.filter((s) => s.status === 'degraded').length
  const errorCount = sources.filter((s) => s.status === 'error').length

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
            {degradedCount > 0 && (
              <span className="flex items-center gap-1 text-xs text-yellow-600">
                <AlertCircle className="w-3 h-3" />
                {degradedCount}
              </span>
            )}
            {errorCount > 0 && (
              <span className="flex items-center gap-1 text-xs text-red-600">
                <XCircle className="w-3 h-3" />
                {errorCount}
              </span>
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
          {sources.map((source) => (
            <div
              key={source.name}
              className={`p-3 rounded border ${STATUS_COLORS[source.status]}`}
            >
              <div className="flex items-start justify-between">
                <div className="flex items-center gap-2">
                  {STATUS_ICONS[source.status]}
                  <div>
                    <div className="font-medium text-sm">{source.name}</div>
                    <div className="text-xs text-gray-600">{source.description}</div>
                  </div>
                </div>
                {source.url && (
                  <a
                    href={source.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-gray-400 hover:text-gray-600"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <ExternalLink className="w-4 h-4" />
                  </a>
                )}
              </div>
              {source.details && (
                <div className="mt-2 text-xs text-gray-500">{source.details}</div>
              )}
              {source.lastUpdate && (
                <div className="mt-1 text-xs text-gray-400">
                  Derniere maj: {source.lastUpdate}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
