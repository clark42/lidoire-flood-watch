import { useState, useEffect, useCallback } from 'react'
import { AlertTriangle, CheckSquare, Clock, RefreshCw, Waves } from 'lucide-react'
import { getRiskOutlook, errorMessage } from '../api'
import type { OutlookConfidence, OutlookHorizon, RiskLevel, RiskOutlook } from '../types'
import InfoTooltip from './InfoTooltip'
import { TOOLTIPS } from '../tooltipContent'
import { formatParis, parseApiDate } from '../utils/time'
import { riskLevelLabel } from '../utils/format'

const REFRESH_MS = 10 * 60 * 1000

const LEVEL_CARD: Record<RiskLevel, string> = {
  green: 'bg-green-50 border-green-300',
  yellow: 'bg-yellow-50 border-yellow-300',
  orange: 'bg-orange-50 border-orange-300',
  red: 'bg-red-50 border-red-300',
}

const LEVEL_BADGE: Record<RiskLevel, string> = {
  green: 'bg-green-600 text-white',
  yellow: 'bg-yellow-400 text-yellow-900',
  orange: 'bg-orange-500 text-white',
  red: 'bg-red-600 text-white',
}

const CONFIDENCE_LABELS: Record<OutlookConfidence['level'], { label: string; className: string }> = {
  good: { label: 'bonne', className: 'text-green-700' },
  reduced: { label: 'reduite', className: 'text-yellow-700' },
  low: { label: 'faible', className: 'text-red-700' },
}

function when(value: string | null | undefined): string {
  return formatParis(value, 'EEE d HH:mm')
}

function HorizonCard({ horizon }: { horizon: OutlookHorizon }) {
  const reachedEarlier =
    horizon.level !== 'green' && horizon.first_at_level !== horizon.max_at
  return (
    <div className={`rounded-lg border-2 p-3 ${LEVEL_CARD[horizon.level]}`}>
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium text-gray-600">Dans {horizon.label}</span>
        <span className={`px-2 py-0.5 rounded text-xs font-semibold ${LEVEL_BADGE[horizon.level]}`}>
          {riskLevelLabel(horizon.level)}
        </span>
      </div>
      <div className="mt-2 flex items-baseline gap-2">
        <span className="text-2xl font-bold">{horizon.max_score.toFixed(0)}</span>
        <span className="text-sm text-gray-500">/100 max</span>
      </div>
      <div className="text-xs text-gray-600 mt-1">
        {horizon.max_hours_ahead === 0 ? 'Maintenant' : `Pic ${when(horizon.max_at)}`}
        {reachedEarlier && <> · niveau atteint {when(horizon.first_at_level)}</>}
      </div>
      <div className="mt-3">
        <div className="text-xs font-semibold text-gray-500 uppercase">Pourquoi</div>
        {horizon.drivers.length === 0 ? (
          <p className="text-sm text-gray-600 mt-1">Aucun facteur notable.</p>
        ) : (
          <ul className="mt-1 space-y-1 text-sm">
            {horizon.drivers.map((d) => (
              <li key={d.factor}>
                <span className="font-medium">{d.label}</span>
                <span className="text-gray-500"> (+{d.points.toFixed(0)} pts)</span>
                <div className="text-gray-600 text-xs">{d.text}</div>
              </li>
            ))}
          </ul>
        )}
        {horizon.modifiers.length > 0 && (
          <ul className="mt-2 space-y-0.5 text-xs text-gray-500">
            {horizon.modifiers.map((m) => (
              <li key={m}>{m}</li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

export default function OutlookPanel() {
  const [data, setData] = useState<RiskOutlook | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<Record<string, boolean>>({})

  const fetchData = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      setData(await getRiskOutlook())
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchData()
    const interval = setInterval(() => {
      if (!document.hidden) fetchData()
    }, REFRESH_MS)
    return () => clearInterval(interval)
  }, [fetchData])

  if (loading && !data) {
    return (
      <div className="bg-white rounded-lg shadow p-4">
        <div className="animate-pulse h-4 bg-gray-200 rounded w-48" />
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mt-4">
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-32 bg-gray-100 rounded" />
          ))}
        </div>
      </div>
    )
  }

  if (!data) {
    return (
      <div role="alert" className="bg-white rounded-lg shadow p-4 flex items-center gap-2 text-red-600 text-sm">
        <AlertTriangle className="w-5 h-5 flex-shrink-0" />
        <span className="flex-1">Prevision 6 h / 24 h / 72 h indisponible : {error}</span>
        <button onClick={fetchData} className="text-blue-600 hover:underline flex items-center gap-1">
          <RefreshCw className="w-4 h-4" /> Reessayer
        </button>
      </div>
    )
  }

  const { checklist, confidence, next_high_tide: tide } = data
  const actBefore = parseApiDate(checklist.act_before)
  const actNow = actBefore !== null && actBefore.getTime() <= Date.now()
  const conf = CONFIDENCE_LABELS[confidence.level] ?? CONFIDENCE_LABELS.reduced

  return (
    <div className="bg-white rounded-lg shadow">
      <div className="p-4 border-b flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Clock className="w-5 h-5 text-blue-500" />
          <h2 className="font-medium text-gray-700">Faut-il agir, et quand ?</h2>
          <InfoTooltip {...TOOLTIPS.outlook} />
        </div>
        <div className="flex items-center gap-3">
          <span className="text-xs text-gray-400">
            Prevision du {formatParis(data.forecast_generated_at, 'HH:mm')}
            {data.model_version ? ` · modele v${data.model_version}` : ''}
          </span>
          <button
            onClick={fetchData}
            disabled={loading}
            className="p-1 rounded hover:bg-gray-100 disabled:opacity-50"
            title="Rafraichir"
          >
            <RefreshCw className={`w-4 h-4 text-gray-500 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>
      </div>

      <div className="p-4 space-y-4">
        {error && (
          <p role="alert" className="text-xs text-red-600">
            Actualisation impossible ({error}) : valeurs de {formatParis(data.generated_at, 'HH:mm')}.
          </p>
        )}

        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          {data.horizons.map((h) => (
            <HorizonCard key={h.hours} horizon={h} />
          ))}
        </div>

        <div className={`rounded-lg border p-3 ${LEVEL_CARD[checklist.level]}`}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2 font-medium">
              <CheckSquare className="w-4 h-4" />
              {checklist.title}
            </div>
            {checklist.level !== 'green' && actBefore && (
              <span className="text-sm font-semibold">
                {actNow ? 'A faire maintenant' : `A faire avant ${when(checklist.act_before)}`}
              </span>
            )}
          </div>
          {checklist.actions.length === 0 ? (
            <p className="text-sm text-gray-600 mt-2">
              Rien a faire sur 72 h. Surveiller les prochaines previsions en cas de forte pluie annoncee.
            </p>
          ) : (
            <ul className="mt-2 space-y-1">
              {checklist.actions.map((action) => (
                <li key={action}>
                  <label className="flex items-start gap-2 text-sm cursor-pointer">
                    <input
                      type="checkbox"
                      className="mt-1"
                      checked={!!done[action]}
                      onChange={(e) => setDone((prev) => ({ ...prev, [action]: e.target.checked }))}
                    />
                    <span className={done[action] ? 'line-through text-gray-500' : ''}>{action}</span>
                  </label>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
          <div className="flex items-start gap-2 text-gray-700">
            <Waves className="w-4 h-4 mt-0.5 text-blue-500 flex-shrink-0" />
            {tide ? (
              <span>
                Prochaine pleine mer estimee a la confluence : <strong>{when(tide.timestamp)}</strong>
                {' '}(dans {tide.hours_from_now.toFixed(1)} h
                {tide.coefficient != null ? `, coef ${tide.coefficient}` : ''}
                {tide.estimated ? ', maree estimee' : ''})
              </span>
            ) : (
              <span className="text-gray-500">Prochaine pleine mer inconnue (predictions de maree absentes).</span>
            )}
          </div>
          <div>
            <div className="text-gray-700">
              Confiance des donnees : <strong className={conf.className}>{conf.label}</strong>
            </div>
            {confidence.notes.length > 0 && (
              <ul className="mt-1 space-y-0.5 text-xs text-gray-500 list-disc list-inside">
                {confidence.notes.map((n) => (
                  <li key={n}>{n}</li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
