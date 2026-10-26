import { useCallback, useEffect, useState } from 'react'
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { CloudRain, Radar, RefreshCw, Waves } from 'lucide-react'
import { getEnsembleForecast, getRadarBasinRain } from '../api'
import type {
  EnsembleForecast,
  EnsembleHorizon,
  GlofasDay,
  GlofasForecast,
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

/** "2026-10-27" -> "27/10" (GloFAS days are calendar days, no time zone). */
function shortDay(day: string): string {
  const [, mm, dd] = day.split('-')
  return dd && mm ? `${dd}/${mm}` : day
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

function GlofasTooltip({ active, payload }: { active?: boolean; payload?: { payload: GlofasDay }[] }) {
  if (!active || !payload || !payload.length) return null
  const d = payload[0].payload
  return (
    <div className="bg-white border rounded shadow p-2 text-xs space-y-0.5">
      <div className="font-medium">{shortDay(d.date)} (J+{d.lead_days})</div>
      <div>Mediane : {d.p50.toFixed(2)} m³/s</div>
      <div>p10-p90 : {d.p10.toFixed(2)} - {d.p90.toFixed(2)} m³/s</div>
      <div>min-max : {d.min.toFixed(2)} - {d.max.toFixed(2)} m³/s</div>
      <div>P(&gt; p95) : {pct(d.prob_gt_p95)} - P(&gt; p99) : {pct(d.prob_gt_p99)}</div>
    </div>
  )
}

function GlofasFan({ glofas }: { glofas: GlofasForecast }) {
  if (!glofas.available || !glofas.days || glofas.days.length === 0) {
    return (
      <div className="text-xs text-gray-400">
        GloFAS indisponible{glofas.note ? ` : ${glofas.note}` : ''}
      </div>
    )
  }
  const clim = glofas.climatology
  const data = glofas.days.map((d) => ({ ...d, outer: [d.min, d.max], band: [d.p10, d.p90] }))
  const dataMax = Math.max(...glofas.days.map((d) => d.max))
  const yMax = Math.max(dataMax * 1.15, clim ? clim.p95 * 1.1 : 0, 0.5)
  const peak95 = glofas.max_prob_gt_p95_10d
  return (
    <div>
      {clim && (
        <div className="text-xs text-gray-600 mb-1">
          P(debit &gt; p95 = {clim.p95.toFixed(1)} m³/s) sur 10 j :{' '}
          <span className="font-semibold">{pct(peak95?.value)}</span>
          {peak95 && peak95.value > 0 && ` (le ${shortDay(peak95.date)})`}
          {' - '}p99 = {clim.p99.toFixed(1)} m³/s : {pct(glofas.max_prob_gt_p99_10d?.value)}
        </div>
      )}
      <ResponsiveContainer width="100%" height={170}>
        <ComposedChart data={data} margin={{ top: 5, right: 10, bottom: 0, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
          <XAxis dataKey="date" tickFormatter={shortDay} tick={{ fontSize: 10 }} minTickGap={20} />
          <YAxis domain={[0, Number(yMax.toFixed(1))]} tick={{ fontSize: 10 }} width={32} />
          <Tooltip content={<GlofasTooltip />} />
          <Area type="monotone" dataKey="outer" stroke="none" fill="#99f6e4" fillOpacity={0.5} isAnimationActive={false} />
          <Area type="monotone" dataKey="band" stroke="none" fill="#2dd4bf" fillOpacity={0.5} isAnimationActive={false} />
          <Line type="monotone" dataKey="p50" stroke="#0f766e" strokeWidth={2} dot={false} isAnimationActive={false} />
          {clim && clim.p95 <= yMax && (
            <ReferenceLine y={clim.p95} stroke="#f97316" strokeDasharray="4 3" label={{ value: 'p95', fontSize: 9, position: 'right' }} />
          )}
          {clim && clim.p99 <= yMax && (
            <ReferenceLine y={clim.p99} stroke="#ef4444" strokeDasharray="4 3" label={{ value: 'p99', fontSize: 9, position: 'right' }} />
          )}
        </ComposedChart>
      </ResponsiveContainer>
      <div className="text-[10px] text-gray-400">
        Run du {glofas.run ? shortDay(glofas.run) : '?'} - {glofas.members ?? '?'} membres - m³/s
        {glofas.stale && ' (donnees anciennes)'}
      </div>
    </div>
  )
}

/** Probabilistic forecasts: observed radar rain, ensemble rain exceedance, GloFAS fan chart. */
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
          <div className="space-y-3">
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

        {forecast?.glofas && (
          <div>
            <div className="flex items-center gap-2 mb-1">
              <Waves className="w-4 h-4 text-teal-700" />
              <span className="text-sm font-medium text-gray-700">Debit Lidoire modelise (GloFAS)</span>
              <InfoTooltip {...TOOLTIPS.glofas} />
            </div>
            <GlofasFan glofas={forecast.glofas} />
          </div>
        )}
      </div>

      <div className="px-4 pb-3 text-[10px] text-gray-400 space-y-0.5">
        <div>Indicatif : ces previsions n'entrent pas dans le score de risque v3.4.</div>
        {rain?.attribution && <div>{rain.attribution}</div>}
        {forecast?.glofas?.attribution && <div>{forecast.glofas.attribution}</div>}
      </div>
    </div>
  )
}
