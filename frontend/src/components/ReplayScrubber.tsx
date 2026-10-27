import { format } from 'date-fns'
import { History, Pause, Play } from 'lucide-react'
import { REPLAY_PRESETS, type ReplayFrame, type ReplayPresetId } from '../utils/replay'
import { formatNumber } from '../utils/format'
import { formatParis } from '../utils/time'

export interface ReplayScrubberProps {
  presetId: ReplayPresetId
  onPreset: (id: ReplayPresetId) => void
  /** Date of the "custom" preset (?tm= deep link or date picker) */
  customAt: Date | null
  onCustomDate: (date: Date) => void
  times: readonly number[]
  index: number
  onIndex: (index: number) => void
  playing: boolean
  onTogglePlay: () => void
  reducedMotion: boolean
  frame: ReplayFrame | null
  /** true when the cursor sits on the latest hour of "ces 10 jours" */
  isLive: boolean
  story: string
  onOpenDashboard?: (date: Date) => void
}

/**
 * Controls of the replay: presets, play/pause, keyboard-operable range
 * (one step = 1 h), time readout and the factual story of the window.
 */
export default function ReplayScrubber({
  presetId,
  onPreset,
  customAt,
  onCustomDate,
  times,
  index,
  onIndex,
  playing,
  onTogglePlay,
  reducedMotion,
  frame,
  isLive,
  story,
  onOpenDashboard,
}: ReplayScrubberProps) {
  const t = times[index]
  const hasTimes = times.length > 0
  return (
    <div className="space-y-3">
      <div role="group" aria-label="Période rejouée" className="flex flex-wrap gap-1.5">
        {REPLAY_PRESETS.map((p) => (
          <button
            key={p.id}
            type="button"
            aria-pressed={presetId === p.id}
            onClick={() => onPreset(p.id)}
            className={`min-h-[44px] rounded-full border px-3 text-[13.5px] sm:min-h-[34px] ${
              presetId === p.id ? 'border-ink bg-ink text-canvas' : 'border-line-2 text-ink-2 hover:bg-surface-2 hover:text-ink'
            }`}
          >
            {p.label}
          </button>
        ))}
        <label
          className={`inline-flex min-h-[44px] items-center gap-2 rounded-full border px-3 text-[13.5px] sm:min-h-[34px] ${
            presetId === 'custom' ? 'border-ink text-ink' : 'border-line-2 text-ink-2'
          }`}
        >
          <span>Autre date</span>
          <input
            type="datetime-local"
            value={customAt ? format(customAt, "yyyy-MM-dd'T'HH:mm") : ''}
            onChange={(e) => {
              const d = new Date(e.target.value)
              if (e.target.value && !Number.isNaN(d.getTime())) onCustomDate(d)
            }}
            className="min-w-0 bg-transparent font-mono text-[12.5px] text-ink focus:outline-none"
          />
        </label>
      </div>

      <div className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-3">
        <button
          type="button"
          onClick={onTogglePlay}
          disabled={!hasTimes}
          aria-label={playing ? 'Mettre en pause le replay' : 'Lire le replay'}
          className="inline-flex min-h-[44px] items-center gap-1.5 rounded-lg bg-accent px-3 text-[14px] font-semibold text-accent-ink disabled:opacity-50 sm:min-h-[36px]"
        >
          {playing ? <Pause className="h-4 w-4" aria-hidden="true" /> : <Play className="h-4 w-4" aria-hidden="true" />}
          {playing ? 'Pause' : 'Lire'}
        </button>
        <input
          type="range"
          min={0}
          max={Math.max(0, times.length - 1)}
          step={1}
          value={Math.min(index, Math.max(0, times.length - 1))}
          disabled={!hasTimes}
          onChange={(e) => onIndex(Number(e.target.value))}
          aria-label="Instant affiché"
          aria-valuetext={t != null ? formatParis(new Date(t), 'EEE d MMMM yyyy à HH:mm') : undefined}
          className="h-11 w-full min-w-0 accent-[var(--accent)]"
        />
      </div>

      <p className="font-mono text-[12.5px] text-ink-2" aria-live="polite">
        {t != null ? formatParis(new Date(t), 'EEE dd/MM/yyyy HH:mm') : '–'}
        {isLive && ' · maintenant'}
        {frame?.pessac != null && ` · Pessac ${formatNumber(frame.pessac, 2)} m`}
        {frame?.score != null && ` · score ${formatNumber(frame.score, 0)}`}
        {frame?.shadow != null && ` (shadow ${formatNumber(frame.shadow, 0)}`}
        {frame?.shadow != null && frame.shadowV4 != null && `, 4.0a ${formatNumber(frame.shadowV4, 0)}`}
        {frame?.shadow != null && ')'}
        {reducedMotion && ' · lecture par pas de 6 h'}
      </p>

      {story && <p className="max-w-[75ch] text-[14.5px] text-ink">{story}</p>}

      {onOpenDashboard && t != null && !isLive && (
        <button
          type="button"
          onClick={() => onOpenDashboard(new Date(t))}
          className="inline-flex min-h-[44px] items-center gap-2 rounded-lg border border-line-2 px-3 text-[13.5px] text-ink hover:bg-surface-2 sm:min-h-[34px]"
        >
          <History className="h-4 w-4" aria-hidden="true" />
          Tout le tableau de bord au {formatParis(new Date(t), 'dd/MM/yyyy HH:mm')}
        </button>
      )}
    </div>
  )
}
