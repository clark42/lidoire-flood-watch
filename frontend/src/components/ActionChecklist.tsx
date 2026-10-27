import { useState } from 'react'
import type { OutlookChecklist } from '../types'
import LevelMeter from './ui/LevelMeter'
import { formatParisRelative, parseApiDate } from '../utils/time'

const STORAGE_KEY = 'lfw_checklist'

/**
 * The owner's own wording is not written yet: the actions are the defaults
 * of backend/app/data/action_plan.json and are shown as examples. Set to
 * false once the owner's action_plan.json is deployed.
 */
export const CHECKLIST_TEXT_IS_EXAMPLE = true

type Ticks = Record<string, boolean>

function readTicks(): Ticks {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    const parsed: unknown = raw ? JSON.parse(raw) : {}
    return parsed && typeof parsed === 'object' ? (parsed as Ticks) : {}
  } catch {
    return {}
  }
}

function writeTicks(ticks: Ticks) {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(ticks))
  } catch {
    // Private mode or blocked storage: ticks stay for the page only
  }
}

/** Tick key: level and action (a new level or wording starts unticked). */
const tickKey = (level: string, action: string) => `${level}:${action}`

/**
 * Owner's checklist of the highest level forecast within 72 h
 * (/api/risk/outlook.checklist), with the deadline (first hour at that
 * level). Ticks are remembered on this device only.
 */
export default function ActionChecklist({ checklist, now = new Date() }: { checklist: OutlookChecklist; now?: Date }) {
  const [ticks, setTicks] = useState<Ticks>(() => (typeof window === 'undefined' ? {} : readTicks()))
  const actBefore = parseApiDate(checklist.act_before)
  const actNow = actBefore !== null && actBefore.getTime() <= now.getTime()
  const done = checklist.actions.filter((a) => ticks[tickKey(checklist.level, a)]).length

  const toggle = (action: string, checked: boolean) => {
    setTicks((prev) => {
      const next = { ...prev, [tickKey(checklist.level, action)]: checked }
      writeTicks(next)
      return next
    })
  }

  return (
    <section aria-labelledby="checklist-title" className="rounded-card border border-line bg-surface p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id="checklist-title" className="font-display text-[19px] font-semibold tracking-[0.01em]">
          Liste du moulin
        </h3>
        <LevelMeter level={checklist.level} className="text-xs" />
      </div>
      <p className="mt-0.5 text-sm text-ink-2">{checklist.title}</p>

      {checklist.level !== 'green' && actBefore && (
        <p className="mt-2 font-bold text-ink">
          {actNow ? 'À faire maintenant' : `À faire avant ${formatParisRelative(actBefore, now)}`}
          {checklist.actions.length > 0 && (
            <span className="ml-2 font-normal text-ink-3">
              {done}/{checklist.actions.length} fait{done > 1 ? 's' : ''}
            </span>
          )}
        </p>
      )}

      {checklist.actions.length === 0 ? (
        <p className="mt-2 text-sm text-ink-2">
          Rien à faire sur 72 h. Surveiller les prochaines prévisions en cas de forte pluie annoncée.
        </p>
      ) : (
        <ul className="mt-1.5">
          {checklist.actions.map((action, i) => {
            const id = `checklist-${i}`
            const checked = !!ticks[tickKey(checklist.level, action)]
            return (
              <li key={action} className="border-b border-line last:border-b-0">
                <label htmlFor={id} className="grid min-h-[44px] cursor-pointer grid-cols-[24px_minmax(0,1fr)] items-center gap-2 py-1.5">
                  <input
                    id={id}
                    type="checkbox"
                    className="h-[18px] w-[18px] accent-[var(--accent)]"
                    checked={checked}
                    onChange={(e) => toggle(action, e.target.checked)}
                  />
                  <span className={`text-sm ${checked ? 'text-ink-3 line-through' : 'text-ink'}`}>{action}</span>
                </label>
              </li>
            )
          })}
        </ul>
      )}

      {CHECKLIST_TEXT_IS_EXAMPLE && (
        <p className="mt-2 flex flex-wrap items-center gap-2 text-[12.5px] text-ink-3">
          <span className="rounded-full border border-dashed border-ink-3 px-2 py-px font-bold text-ink-2">exemples</span>
          Textes par défaut, à réécrire avec le propriétaire (gestes et délais réels du moulin). Cases cochées gardées sur cet
          appareil.
        </p>
      )}
    </section>
  )
}
