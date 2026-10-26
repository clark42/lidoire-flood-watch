import type { ReactNode } from 'react'
import type { RiskLevel } from '../../types'
import { RISK_LEVEL_LABELS } from '../../utils/format'
import { levelRank } from '../../utils/levels'

interface LevelMeterProps {
  level: RiskLevel
  /** Text after the pips; defaults to the level word (Vert, Jaune, Orange, Rouge) */
  label?: ReactNode
  className?: string
}

/**
 * A level never relies on colour alone: 4 position pips (1 to 4 filled), the
 * level colour on the filled pips, and a word in ink. Reads the same in
 * colour-blind vision, grayscale and print.
 */
export default function LevelMeter({ level, label, className = '' }: LevelMeterProps) {
  const rank = levelRank(level)
  return (
    <span className={`level-meter ${className}`} data-level={level}>
      <span className="level-meter__pips" aria-hidden="true">
        {[1, 2, 3, 4].map((i) => (
          <span key={i} data-on={i <= rank ? '' : undefined} />
        ))}
      </span>
      <span>
        {label ?? RISK_LEVEL_LABELS[level]}
        <span className="sr-only">{` (niveau ${rank} sur 4)`}</span>
      </span>
    </span>
  )
}
