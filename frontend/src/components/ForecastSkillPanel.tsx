import { useState, useEffect, useCallback } from 'react'
import { RefreshCw, Target } from 'lucide-react'
import { getForecastSkill, errorMessage } from '../api'
import type { ForecastSkill } from '../types'
import InfoTooltip from './InfoTooltip'
import { TOOLTIPS } from '../tooltipContent'
import { formatParis } from '../utils/time'

const PERIODS = [7, 30, 90]

function num(value: number | null, digits = 1): string {
  return value == null ? '-' : value.toFixed(digits)
}

function pct(value: number | null): string {
  return value == null ? '-' : `${Math.round(value * 100)} %`
}

export default function ForecastSkillPanel() {
  const [days, setDays] = useState(30)
  const [data, setData] = useState<ForecastSkill | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const fetchData = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      setData(await getForecastSkill([6, 24, 72], days))
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setLoading(false)
    }
  }, [days])

  useEffect(() => {
    fetchData()
  }, [fetchData])

  return (
    <div className="bg-white rounded-lg shadow">
      <div className="p-4 border-b flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-medium flex items-center gap-2">
          <Target className="w-5 h-5" />
          Prevu vs observe
          <InfoTooltip {...TOOLTIPS.forecastSkill} />
        </h3>
        <div className="flex items-center gap-2">
          <select
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="border rounded px-2 py-1 text-sm"
            aria-label="Periode"
          >
            {PERIODS.map((d) => (
              <option key={d} value={d}>{d} jours</option>
            ))}
          </select>
          <button onClick={fetchData} disabled={loading} className="p-2 hover:bg-gray-100 rounded disabled:opacity-50">
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>
      </div>

      <div className="p-4 text-sm">
        {error && (
          <div role="alert" className="mb-3 bg-red-50 border border-red-200 text-red-800 rounded px-3 py-2">
            {error}
          </div>
        )}
        {data && (
          <>
            <p className="text-gray-600 mb-3">
              {data.n_runs} prevision(s) archivee(s) (modele v{data.model_version})
              {data.first_run_at && <> du {formatParis(data.first_run_at, 'dd/MM HH:mm')} au {formatParis(data.last_run_at, 'dd/MM HH:mm')}</>}
              . Seuil : {data.threshold_level} ({data.threshold.toFixed(0)}).
            </p>
            {data.notes.map((n) => (
              <p key={n} className="text-gray-500 mb-2">{n}</p>
            ))}
            <div className="overflow-x-auto">
              <table className="w-full text-left">
                <thead className="text-xs text-gray-500 border-b">
                  <tr>
                    <th className="py-1 pr-3">Echeance</th>
                    <th className="py-1 pr-3">Paires</th>
                    <th className="py-1 pr-3">MAE</th>
                    <th className="py-1 pr-3">Biais</th>
                    <th className="py-1 pr-3">Detections</th>
                    <th className="py-1 pr-3">Manques</th>
                    <th className="py-1 pr-3">Fausses alertes</th>
                    <th className="py-1 pr-3">POD</th>
                    <th className="py-1 pr-3">FAR</th>
                    <th className="py-1 pr-3">CSI</th>
                  </tr>
                </thead>
                <tbody>
                  {data.by_lead.map((s) => (
                    <tr key={s.lead_hours} className="border-b last:border-0">
                      <td className="py-1 pr-3 font-medium">{s.lead_hours} h</td>
                      <td className="py-1 pr-3">{s.n}</td>
                      <td className="py-1 pr-3">{num(s.mae)}</td>
                      <td className="py-1 pr-3">{num(s.bias)}</td>
                      <td className="py-1 pr-3">{s.hits}</td>
                      <td className="py-1 pr-3">{s.misses}</td>
                      <td className="py-1 pr-3">{s.false_alarms}</td>
                      <td className="py-1 pr-3">{pct(s.pod)}</td>
                      <td className="py-1 pr-3">{pct(s.far)}</td>
                      <td className="py-1 pr-3">{pct(s.csi)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="text-xs text-gray-400 mt-2">
              Paires horaires (score prevu a l'echeance vs score temps reel stocke a +/- {data.tolerance_minutes} min), pas des evenements.
            </p>
          </>
        )}
      </div>
    </div>
  )
}
