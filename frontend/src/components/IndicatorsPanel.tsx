import { useState, useEffect, useCallback } from 'react'
import type { ReactNode } from 'react'
import { Gauge, TrendingUp, TrendingDown, Minus } from 'lucide-react'
import { getIndicators, errorMessage } from '../api'
import type {
  FlowIndicator,
  GroundwaterIndicator,
  IndicatorsData,
  IndicatorThresholdStatus,
  IndicatorTrend,
  TideObservedIndicator,
} from '../types'
import InfoTooltip from './InfoTooltip'
import { TOOLTIPS } from '../tooltipContent'
import { formatParis } from '../utils/time'

// Hydrological indicators (/api/indicators): display only, never a v3.4 input.

interface IndicatorsPanelProps {
  refreshInterval?: number // ms
}

const THRESHOLD_LABELS: Record<string, string> = {
  watch: 'vigilance',
  alert: 'alerte',
}

function fmt(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return '-'
  return value.toLocaleString('fr-FR', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

/** Digits adapted to the order of magnitude (0.066 m3/s vs 1 140 m3/s). */
function fmtFlow(value: number | null | undefined): string {
  if (value === null || value === undefined) return '-'
  const abs = Math.abs(value)
  return fmt(value, abs >= 100 ? 0 : abs >= 10 ? 1 : abs >= 1 ? 2 : 3)
}

function PercentileBadge({ label, value }: { label: string | null; value: number | null }) {
  if (!label || value === null) {
    return <span className="text-xs text-gray-400 w-12 text-right">p-</span>
  }
  const color =
    value >= 99 ? 'bg-red-100 text-red-700'
    : value >= 95 ? 'bg-orange-100 text-orange-700'
    : value >= 90 ? 'bg-yellow-100 text-yellow-800'
    : 'bg-gray-100 text-gray-600'
  return (
    <span className={`px-1.5 py-0.5 rounded text-xs font-medium tabular-nums w-12 text-center ${color}`}>
      {label}
    </span>
  )
}

function TrendIcon({ trend }: { trend: IndicatorTrend | null }) {
  if (trend === 'rising') return <TrendingUp className="w-3.5 h-3.5 text-red-500" aria-label="en hausse" />
  if (trend === 'falling') return <TrendingDown className="w-3.5 h-3.5 text-green-600" aria-label="en baisse" />
  if (trend === 'stable') return <Minus className="w-3.5 h-3.5 text-gray-400" aria-label="stable" />
  return <span className="w-3.5" />
}

function thresholdText(status: IndicatorThresholdStatus, unit: string): string | null {
  if (status.next && status.next_value !== null && status.ratio_to_next !== null) {
    const name = THRESHOLD_LABELS[status.next] ?? status.next
    return `seuil ${name} ${fmt(status.next_value, status.next_value >= 100 ? 0 : 1)} ${unit} : ${Math.round(status.ratio_to_next * 100)} %`
  }
  if (status.exceeded.length > 0) {
    const name = THRESHOLD_LABELS[status.exceeded[status.exceeded.length - 1]] ?? status.exceeded[status.exceeded.length - 1]
    return `au-dessus du seuil ${name}`
  }
  return null
}

interface RowProps {
  name: string
  value: string
  unit: string
  detail: (string | null)[]
  trend: IndicatorTrend | null
  percentile: number | null
  percentileLabel: string | null
  available: boolean
  stale: boolean | null
  lastTimestamp: string | null
  alert?: boolean
}

function Row({ name, value, unit, detail, trend, percentile, percentileLabel, available, stale, lastTimestamp, alert }: RowProps) {
  const details = detail.filter((d): d is string => !!d)
  return (
    <div className="py-1.5">
      <div className="flex items-center gap-2">
        <span className="text-sm text-gray-700 flex-1 truncate" title={name}>{name}</span>
        {!available ? (
          <span className="text-xs text-gray-400">aucune donnee</span>
        ) : (
          <>
            {stale && (
              <span
                className="text-[10px] px-1 rounded bg-gray-100 text-gray-500"
                title={`Derniere valeur : ${formatParis(lastTimestamp, 'dd/MM HH:mm')}`}
              >
                ancien
              </span>
            )}
            <span className={`text-sm font-semibold tabular-nums ${alert ? 'text-red-600' : 'text-gray-900'}`}>
              {value}
              <span className="text-xs font-normal text-gray-500 ml-0.5">{unit}</span>
            </span>
            <TrendIcon trend={trend} />
          </>
        )}
        <PercentileBadge label={percentileLabel} value={percentile} />
      </div>
      {available && details.length > 0 && (
        <div className="text-xs text-gray-500 mt-0.5">{details.join(' · ')}</div>
      )}
    </div>
  )
}

function FlowRow({ f }: { f: FlowIndicator }) {
  return (
    <Row
      name={f.name}
      value={fmtFlow(f.q_m3s)}
      unit="m³/s"
      detail={[
        f.q_mean_24h_m3s !== null ? `moy. 24 h ${fmtFlow(f.q_mean_24h_m3s)}` : null,
        thresholdText(f.threshold, 'm³/s'),
      ]}
      trend={f.trend}
      percentile={f.percentile}
      percentileLabel={f.percentile_label}
      available={f.available}
      stale={f.stale}
      lastTimestamp={f.last_timestamp}
      alert={f.threshold.exceeded.includes('alert')}
    />
  )
}

function GroundwaterRow({ g }: { g: GroundwaterIndicator }) {
  const change = g.change_24h_m
  return (
    <Row
      name={g.name}
      value={fmt(g.level_m_ngf, 2)}
      unit="m NGF"
      detail={[
        change !== null ? `24 h ${change > 0 ? '+' : ''}${fmt(change, 2)} m` : null,
        thresholdText(g.threshold, 'm'),
      ]}
      trend={g.trend}
      percentile={g.percentile}
      percentileLabel={g.percentile_label}
      available={g.available}
      stale={g.stale}
      lastTimestamp={g.last_timestamp}
      alert={g.threshold.exceeded.length > 0}
    />
  )
}

function TideRow({ t }: { t: TideObservedIndicator }) {
  return (
    <Row
      name={t.name}
      value={fmt(t.height_m, 2)}
      unit="m"
      detail={[t.max_24h_m !== null ? `max 24 h ${fmt(t.max_24h_m, 2)} m` : null]}
      trend={null}
      percentile={t.percentile}
      percentileLabel={t.percentile_label}
      available={t.available}
      stale={t.stale}
      lastTimestamp={t.last_timestamp}
    />
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="px-4 py-2">
      <div className="text-[11px] font-semibold text-gray-500 uppercase tracking-wide">{title}</div>
      <div className="divide-y divide-gray-50">{children}</div>
    </div>
  )
}

export default function IndicatorsPanel({ refreshInterval = 5 * 60 * 1000 }: IndicatorsPanelProps) {
  const [data, setData] = useState<IndicatorsData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const fetchData = useCallback(async () => {
    try {
      const result = await getIndicators()
      setData(result)
      setError(null)
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchData()
    // Skip refreshes while the tab is hidden
    const interval = setInterval(() => {
      if (!document.hidden) fetchData()
    }, refreshInterval)
    return () => clearInterval(interval)
  }, [fetchData, refreshInterval])

  if (loading && !data) {
    return (
      <div className="bg-white rounded-xl border border-gray-200 p-4 animate-pulse">
        <div className="h-4 bg-gray-200 rounded w-1/2 mb-4" />
        <div className="space-y-2">
          <div className="h-3 bg-gray-200 rounded" />
          <div className="h-3 bg-gray-200 rounded w-3/4" />
        </div>
      </div>
    )
  }

  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <div className="px-4 py-3 border-b border-gray-100 flex items-center gap-2">
        <Gauge className="w-5 h-5 text-teal-600" />
        <h3 className="font-medium">Indicateurs hydrologiques</h3>
        <InfoTooltip {...TOOLTIPS.indicatorsHeader} />
        <span className="ml-auto text-[11px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-500 flex items-center gap-1">
          hors modele v3.4
          <InfoTooltip {...TOOLTIPS.indicatorsPercentile} />
        </span>
      </div>

      {error && !data && (
        <div className="px-4 py-3 text-sm text-gray-500">Indicateurs indisponibles : {error}</div>
      )}

      {data && (
        <div className="divide-y divide-gray-100">
          {data.runoff.length > 0 && (
            <Section title="Ruissellement (analogues Lidoire)">
              {data.runoff.map((f) => <FlowRow key={f.id} f={f} />)}
            </Section>
          )}
          {data.dordogne.length > 0 && (
            <Section title="Dordogne (debit)">
              {data.dordogne.map((f) => <FlowRow key={f.id} f={f} />)}
            </Section>
          )}
          {data.groundwater.length > 0 && (
            <Section title="Nappe alluviale">
              {data.groundwater.map((g) => <GroundwaterRow key={g.id} g={g} />)}
            </Section>
          )}
          {data.tide_observed.length > 0 && (
            <Section title="Maree observee (surcote comprise)">
              {data.tide_observed.map((t) => <TideRow key={t.id} t={t} />)}
            </Section>
          )}
        </div>
      )}
    </div>
  )
}
