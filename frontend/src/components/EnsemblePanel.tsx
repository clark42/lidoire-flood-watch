import { useCallback, useEffect, useState } from 'react'
import { CloudRain, Radar, RefreshCw } from 'lucide-react'
import { getEnsembleForecast, getRadarBasinRain } from '../api'
import type {
  EnsembleForecast,
  EnsembleHorizon,
  RadarBasinRain,
} from '../types'
import InfoTooltip from './InfoTooltip'
import { TOOLTIPS } from '../tooltipContent'
import { formatAge, formatParis, parseApiDate } from '../utils/time'

const REFRESH_MS = 15 * 60 * 1000
const RADAR_WINDOWS = ['1h', '3h', '6h', '24h']
const MAX_ROWS: Record<string, number> = { '24h': 5, '72h': 3 }

/** Probability cell colour (share of ensemble members above the threshold). */
function probClass(p: number): string {
  if (p < 0.05) return 'bg-gray-50 text-gray-400'
  if (p < 0.2) return 'bg-yellow-50 text-yellow-800'
  if (p < 0.5) return 'bg-orange-100 text-orange-800'
  return 'bg-red-100 text-red-800 font-semibold'
}

function pct(p: number | null | undefined): string {
  return p == null ? '-' : `${Math.round(p * 100)} %`
}

function modelDetail(row: EnsembleHorizon, threshold: string, labels: Record<string, string>): string {
  return Object.entries(row.models)
    .map(([id, s]) => `${labels[id] ?? id} : ${pct(s.prob[threshold])} (mediane ${s.p50.toFixed(1)} mm)`)
    .join('\n')
}

function RainTable({
  windowKey,
  rows,
  thresholds,
  labels,
}: {
  windowKey: string
  rows: EnsembleHorizon[]
  thresholds: number[]
  labels: Record<string, string>
}) {
  const shown = rows.slice(0, MAX_ROWS[windowKey] ?? rows.length)
  if (shown.length === 0) return null
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-gray-500">
          <th className="text-left font-normal py-1">Pluie {windowKey}</th>
          {thresholds.map((t) => (
            <th key={t} className="font-normal py-1 text-center">&gt; {t} mm</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {shown.map((row) => (
          <tr key={row.start} className="border-t border-gray-100">
            <td className="py-1 pr-2 whitespace-nowrap">
              <div className="text-gray-700">
                {row.lead_start_h}-{row.lead_end_h} h
              </div>
              <div className="text-[10px] text-gray-400">
                des {formatParis(row.start, 'EEE d HH')}h
              </div>
            </td>
            {thresholds.map((t) => {
              const key = String(t)
              const p = row.blend.prob[key] ?? 0
              return (
                <td key={t} className="py-1 px-0.5">
                  <div
                    className={`rounded text-center py-1 ${probClass(p)}`}
                    title={modelDetail(row, key, labels)}
                  >
                    {pct(p)}
                  </div>
                </td>
              )
            })}
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function RadarStrip({ radar }: { radar: RadarBasinRain | null }) {
  if (!radar) return null
  const last = parseApiDate(radar.last_image_time)
  return (
    <div>
      <div className="flex items-center gap-2 mb-2">
        <Radar className="w-4 h-4 text-cyan-600" />
        <span className="text-sm font-medium text-gray-700">Pluie observee sur le bassin (radar)</span>
        <InfoTooltip {...TOOLTIPS.radarBasin} />
      </div>
      {radar.available ? (
        <>
          <div className="grid grid-cols-4 gap-2">
            {RADAR_WINDOWS.map((w) => {
              const win = radar.windows[w]
              return (
                <div key={w} className="bg-cyan-50 rounded p-2 text-center">
                  <div className="text-[10px] text-gray-500">{w}</div>
                  <div className="text-sm font-semibold text-cyan-900">
                    {win?.mm != null ? `${win.mm.toFixed(1)} mm` : '-'}
                    {win && !win.complete && <span title="heures manquantes">*</span>}
                  </div>
                </div>
              )
            })}
          </div>
          <div className="text-[10px] text-gray-400 mt-1">
            Derniere image {formatParis(last, 'HH:mm')}
            {last && ` (${formatAge(last)})`} - {radar.attribution}
          </div>
        </>
      ) : (
        <div className="text-xs text-gray-400">
          Radar indisponible{radar.note ? ` : ${radar.note}` : ''}
        </div>
      )}
    </div>
  )
}

/** Probabilistic forecasts: observed radar rain, ensemble rain exceedance (the GloFAS fan is GlofasFan). */
export default function EnsemblePanel() {
  const [forecast, setForecast] = useState<EnsembleForecast | null>(null)
  const [radar, setRadar] = useState<RadarBasinRain | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const fetchData = useCallback(async () => {
    setLoading(true)
    const [fc, rd] = await Promise.allSettled([getEnsembleForecast(), getRadarBasinRain()])
    if (fc.status === 'fulfilled') {
      setForecast(fc.value)
      setError(null)
    } else {
      setError(fc.reason instanceof Error ? fc.reason.message : 'Erreur de chargement')
    }
    if (rd.status === 'fulfilled') setRadar(rd.value)
    setLoading(false)
  }, [])

  useEffect(() => {
    fetchData()
    const interval = setInterval(() => {
      if (!document.hidden) fetchData()
    }, REFRESH_MS)
    return () => clearInterval(interval)
  }, [fetchData])

  const rain = forecast?.rain
  const labels: Record<string, string> = Object.fromEntries(
    (rain?.models ?? []).map((m) => [m.id, m.label]),
  )

  return (
    <div className="bg-white rounded-lg shadow">
      <div className="p-4 border-b flex items-center justify-between">
        <div className="flex items-center gap-2">
          <CloudRain className="w-5 h-5 text-teal-600" />
          <h3 className="font-medium text-gray-700">Previsions probabilistes</h3>
          <InfoTooltip {...TOOLTIPS.ensembleRain} />
        </div>
        <button
          onClick={fetchData}
          disabled={loading}
          className="p-1 rounded hover:bg-gray-100 disabled:opacity-50"
          title="Rafraichir"
        >
          <RefreshCw className={`w-4 h-4 text-gray-500 ${loading ? 'animate-spin' : ''}`} />
        </button>
      </div>

      <div className="p-4 space-y-4">
        <RadarStrip radar={radar} />

        {error && !forecast && <div className="text-sm text-red-600">{error}</div>}
        {loading && !forecast && <div className="h-24 bg-gray-100 rounded animate-pulse" />}

        {rain && (
          <div className="space-y-3 overflow-x-auto">
            {rain.available && rain.horizons && rain.thresholds ? (
              ['24h', '72h'].map((w) => (
                <RainTable
                  key={w}
                  windowKey={w}
                  rows={rain.horizons?.[w] ?? []}
                  thresholds={rain.thresholds?.[w] ?? []}
                  labels={labels}
                />
              ))
            ) : (
              <div className="text-xs text-gray-400">
                Pluie d'ensemble indisponible{rain.note ? ` : ${rain.note}` : ''}
              </div>
            )}
            {rain.models && rain.models.length > 0 && (
              <div className="text-[10px] text-gray-400">
                Modeles :{' '}
                {rain.models
                  .map((m) => (m.available ? `${m.label} (${m.members})` : `${m.label} (indisponible)`))
                  .join(', ')}
              </div>
            )}
          </div>
        )}

      </div>

      <div className="px-4 pb-3 text-[10px] text-gray-400 space-y-0.5">
        <div>Indicatif : ces previsions n'entrent pas dans le score de risque v3.4.</div>
        {rain?.attribution && <div>{rain.attribution}</div>}
        <div>Débit GloFAS sur 30 jours : section « 30 jours ».</div>
      </div>
    </div>
  )
}
