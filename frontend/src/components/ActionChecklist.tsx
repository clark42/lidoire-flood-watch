import { useState } from 'react'
import type { OutlookChecklist } from '../types'
import LevelMeter from './ui/LevelMeter'
import { formatParisRelative, parseApiDate } from '../utils/time'

/**
 * Owner's checklist of the highest level forecast within 72 h
 * (/api/risk/outlook.checklist, backend/app/data/action_plan.json).
 * Ticks are kept for the page only.
 */
export default function ActionChecklist({ checklist, now = new Date() }: { checklist: OutlookChecklist; now?: Date }) {
  const [done, setDone] = useState<Record<string, boolean>>({})
  const actBefore = parseApiDate(checklist.act_before)
  const actNow = actBefore !== null && actBefore.getTime() <= now.getTime()

  return (
    <div className="rounded-card border border-line bg-surface p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-display text-[19px] font-semibold tracking-[0.01em]">{checklist.title}</h3>
        <LevelMeter level={checklist.level} className="text-xs" />
      </div>
      {checklist.level !== 'green' && actBefore && (
        <p className="mt-1 text-sm font-bold text-ink">
          {actNow ? 'À faire maintenant' : `À faire avant ${formatParisRelative(actBefore, now)}`}
        </p>
      )}
      {checklist.actions.length === 0 ? (
        <p className="mt-2 text-sm text-ink-2">
          Rien à faire sur 72 h. Surveiller les prochaines prévisions en cas de forte pluie annoncée.
        </p>
      ) : (
        <ul className="mt-2">
          {checklist.actions.map((action, i) => {
            const id = `checklist-${i}`
            return (
              <li key={action} className="grid grid-cols-[22px_minmax(0,1fr)] items-start gap-2 border-b border-line py-2 last:border-b-0">
                <input
                  id={id}
                  type="checkbox"
                  className="mt-0.5 h-[18px] w-[18px] accent-[var(--accent)]"
                  checked={!!done[action]}
                  onChange={(e) => setDone((prev) => ({ ...prev, [action]: e.target.checked }))}
                />
                <label htmlFor={id} className={`cursor-pointer text-sm ${done[action] ? 'text-ink-3 line-through' : 'text-ink'}`}>
                  {action}
                </label>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
