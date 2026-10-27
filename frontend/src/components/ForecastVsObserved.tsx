import { useState } from 'react'
import { getForecastSkill } from '../api'
import type { ForecastSkill } from '../types'
import { useSharedResource } from '../hooks/useSharedResource'
import { useElementWidth } from '../hooks/useElementWidth'
import { defaultLead, pairsForLead, scoreDomain, SHORT_ARCHIVE_PAIRS, skillRows, skillState } from '../utils/forecastSkill'
import { linearScale, niceTicks } from '../utils/scale'
import { formatNumber } from '../utils/format'
import { formatParis } from '../utils/time'
import ValueTag from './ui/ValueTag'
import '../styles/field.css'

function Chart({ skill, lead, width }: { skill: ForecastSkill; lead: number; width: number }) {
  const pts = pairsForLead(skill, lead)
  const H = 190
  const m = { l: 30, r: 10, t: 12, b: 26 }
  if (pts.length === 0) {
    return (
      <div className="hatched grid h-[120px] place-items-center rounded-lg border border-dashed border-line-2 p-3 text-center text-[13px] text-ink-2">
        Aucune paire à {lead} h : la première échéance n’est pas encore atteinte.
      </div>
    )
  }
  const t0 = pts[0].t
  const t1 = pts[pts.length - 1].t
  const x = linearScale([t0, t1 > t0 ? t1 : t0 + 3600_000], [m.l, width - m.r])
  const y = linearScale(scoreDomain(pts, skill.threshold), [H - m.b, m.t])
  const line = (key: 'forecast' | 'actual') =>
    pts.length > 1 ? `M${pts.map((p) => `${x(p.t).toFixed(1)},${y(p[key]).toFixed(1)}`).join('L')}` : ''
  const ticks = niceTicks(y.domain[0], y.domain[1], 4)
  const days = Math.max(1, Math.round((t1 - t0) / 86_400_000))
  const xTicks = [t0, t0 + (t1 - t0) / 2, t1].filter((t, i, a) => i === 0 || t > a[i - 1])

  return (
    <div className="fchart">
      <svg
        width={width}
        height={H}
        viewBox={`0 0 ${width} ${H}`}
        role="img"
        aria-label={`Score prévu à ${lead} h contre score réalisé, ${pts.length} paires sur ${days} jour${days > 1 ? 's' : ''}`}
      >
        {ticks.map((v) => (
          <g key={v}>
            <line x1={m.l} x2={width - m.r} y1={y(v)} y2={y(v)} className="fchart-grid" />
            <text x={m.l - 4} y={y(v) + 3} textAnchor="end" className="fchart-ax">
              {v}
            </text>
          </g>
        ))}
        <line x1={m.l} x2={width - m.r} y1={y(skill.threshold)} y2={y(skill.threshold)} className="fvo-threshold" />
        <text x={width - m.r - 2} y={y(skill.threshold) - 4} textAnchor="end" className="fchart-ax">
          {`seuil ${skill.threshold_level} ${formatNumber(skill.threshold, 0)}`}
        </text>
        <path d={line('actual')} className="fvo-actual" />
        <path d={line('forecast')} className="fvo-forecast" />
        {pts.length === 1 &&
          pts.map((p) => (
            <g key={p.t}>
              <circle cx={x(p.t)} cy={y(p.actual)} r={3} style={{ fill: 'var(--ink)' }} />
              <circle cx={x(p.t)} cy={y(p.forecast)} r={3.5} className="fvo-dot" />
            </g>
          ))}
        {xTicks.map((t) => (
          <text key={t} x={x(t)} y={H - m.b + 15} textAnchor="middle" className="fchart-ax">
            {formatParis(new Date(t), days > 2 ? 'd MMM' : 'HH:mm')}
          </text>
        ))}
        <line x1={m.l} x2={width - m.r} y1={H - m.b} y2={H - m.b} className="fchart-base" />
      </svg>
      <div className="mt-1.5 flex flex-wrap gap-x-3.5 text-[12px] text-ink-2">
        <span className="inline-flex items-center gap-1.5">
          <svg width="20" height="8" aria-hidden="true">
            <line x1="1" x2="19" y1="4" y2="4" className="fvo-actual" />
          </svg>
          score réalisé (stocké)
        </span>
        <span className="inline-flex items-center gap-1.5">
          <svg width="20" height="8" aria-hidden="true">
            <line x1="1" x2="19" y1="4" y2="4" className="fvo-forecast" />
          </svg>
          {`prévu ${lead} h avant`}
        </span>
      </div>
    </div>
  )
}

export interface ForecastVsObservedViewProps {
  skill: ForecastSkill | null
  error?: string | null
  width: number
}

/** Archived forecast score at a lead vs the realised score, MAE and contingency. */
export function ForecastVsObservedView({ skill, error, width }: ForecastVsObservedViewProps) {
  const [chosen, setChosen] = useState<number | null>(null)
  if (!skill) {
    return <p className="text-ink-2">{error ? `Suivi indisponible : ${error}` : 'Chargement du suivi des prévisions…'}</p>
  }
  const state = skillState(skill)
  const lead = chosen ?? defaultLead(skill)
  const rows = skillRows(skill)
  const since = skill.first_run_at ? formatParis(skill.first_run_at, 'd MMM HH:mm') : null

  if (state === 'empty') {
    return (
      <div className="hatched rounded-lg border border-dashed border-line-2 p-4 text-[13.5px] text-ink-2" role="status">
        <p>
          <b className="text-ink">Pas encore de quoi juger.</b>{' '}
          {skill.n_runs > 0
            ? `${skill.n_runs} prévision${skill.n_runs > 1 ? 's' : ''} archivée${skill.n_runs > 1 ? 's' : ''}${since ? ` depuis le ${since}` : ''}, mais aucune échéance (${skill.lead_hours.join(' / ')} h) n’est encore atteinte avec un score réalisé.`
            : 'Aucune prévision archivée pour l’instant.'}
        </p>
        <p className="mt-1">
          La courbe « prévu il y a 24 h », l’écart moyen (MAE) et la réussite au seuil {skill.threshold_level} apparaîtront
          d’elles-mêmes avec l’archive.
        </p>
        {skill.notes.length > 0 && <p className="mt-1 text-[12px] text-ink-3">{skill.notes.join(' ')}</p>}
      </div>
    )
  }

  return (
    <div className="space-y-3">
      {state === 'short' && (
        <p className="text-[13px] text-ink-2" role="status">
          <b className="text-ink">Archive courte</b> ({skill.n_runs} runs{since ? ` depuis le ${since}` : ''}) : moins de{' '}
          {SHORT_ARCHIVE_PAIRS} paires par échéance, ces chiffres bougeront.
        </p>
      )}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="inline-flex flex-wrap gap-0.5 rounded-full border border-line bg-surface-2 p-0.5" role="group" aria-label="Échéance">
          {skill.lead_hours.map((h) => (
            <button
              key={h}
              type="button"
              aria-pressed={h === lead}
              onClick={() => setChosen(h)}
              className={`min-h-[44px] rounded-full px-3 text-[13px] sm:min-h-[30px] ${
                h === lead ? 'bg-surface font-bold text-ink shadow-card' : 'text-ink-2'
              }`}
            >
              {h} h
            </button>
          ))}
        </div>
        <ValueTag kind="model">score v{skill.model_version}</ValueTag>
      </div>
      {lead != null && <Chart skill={skill} lead={lead} width={width} />}
      <div className="overflow-x-auto">
        <table className="w-full min-w-[420px] border-collapse text-[13px]">
          <caption className="sr-only">Qualité des prévisions par échéance</caption>
          <thead>
            <tr className="border-b border-line text-ink-2">
              <th scope="col" className="px-2 py-1.5 text-left font-bold">Échéance</th>
              <th scope="col" className="px-2 py-1.5 text-right font-bold">Paires</th>
              <th scope="col" className="px-2 py-1.5 text-right font-bold">MAE</th>
              <th scope="col" className="px-2 py-1.5 text-right font-bold">Biais</th>
              <th scope="col" className="px-2 py-1.5 text-right font-bold" title="réussites / manqués / fausses alertes / rejets corrects">
                R / M / FA / RC
              </th>
              <th scope="col" className="px-2 py-1.5 text-right font-bold">POD</th>
              <th scope="col" className="px-2 py-1.5 text-right font-bold">FAR</th>
              <th scope="col" className="px-2 py-1.5 text-right font-bold">CSI</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.lead} className="border-b border-line tabular-nums">
                <th scope="row" className="px-2 py-1.5 text-left font-normal">{r.lead} h</th>
                <td className="px-2 py-1.5 text-right">{r.n}</td>
                <td className="px-2 py-1.5 text-right">{r.mae}</td>
                <td className="px-2 py-1.5 text-right">{r.bias}</td>
                <td className="px-2 py-1.5 text-right">{r.contingency.join(' / ')}</td>
                <td className="px-2 py-1.5 text-right">{r.pod}</td>
                <td className="px-2 py-1.5 text-right">{r.far}</td>
                <td className="px-2 py-1.5 text-right">{r.csi}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-[12px] text-ink-3">
        MAE et biais en points de score (prévu - réalisé). Contingence au seuil {skill.threshold_level} (
        {formatNumber(skill.threshold, 0)}) sur {skill.days} jours, tolérance {skill.tolerance_minutes} min.
        {skill.notes.length > 0 && ` ${skill.notes.join(' ')}`}
      </p>
    </div>
  )
}

const THIRTY_MIN = 30 * 60_000

export default function ForecastVsObserved() {
  const [ref, width] = useElementWidth<HTMLDivElement>()
  const skill = useSharedResource<ForecastSkill>('risk/forecast-skill', () => getForecastSkill(), THIRTY_MIN)
  return (
    <div className="min-w-0 rounded-card border border-line bg-surface p-4">
      <h3 className="font-display text-[19px] font-semibold tracking-[0.01em]">Score prévu vs score réalisé</h3>
      <p className="mb-2 text-[13px] text-ink-3">Prévisions archivées à chaque run, comparées au score temps réel stocké.</p>
      <div ref={ref} className="min-w-0">
        <ForecastVsObservedView skill={skill.data} error={skill.error} width={width} />
      </div>
    </div>
  )
}
