import { format } from 'date-fns'
import { Clock, History, Play, Loader2 } from 'lucide-react'
import type { FloodEpisode } from '../types'
import { formatParis, parseApiDate } from '../utils/time'

function episodeDate(episode: FloodEpisode): Date {
  return parseApiDate(episode.peak_time || episode.start_time) ?? new Date(NaN)
}

interface TimeMachineBannerProps {
  active: boolean
  onToggle: () => void
  selectedDate: Date | null
  onDateChange: (date: Date) => void
  episodes: FloodEpisode[]
  loading?: boolean
}

function getSeverityDotClass(severity: number): string {
  if (severity >= 4) return 'bg-red-500'
  if (severity === 3) return 'bg-orange-500'
  return 'bg-yellow-400'
}

export default function TimeMachineBanner({
  active,
  onToggle,
  selectedDate,
  onDateChange,
  episodes,
  loading = false,
}: TimeMachineBannerProps) {
  if (!active) {
    return (
      <div className="rounded-lg bg-gray-100 p-2 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Clock className="w-4 h-4 text-gray-500" />
          <span className="text-sm text-gray-600">Dashboard en direct</span>
        </div>
        <button
          onClick={onToggle}
          className="flex items-center gap-1.5 px-3 py-1 text-sm bg-amber-100 text-amber-700 rounded-full hover:bg-amber-200 transition"
        >
          <History className="w-3.5 h-3.5" />
          TimeMachine
        </button>
      </div>
    )
  }

  const sortedEpisodes = [...episodes]
    .sort((a, b) => (parseApiDate(b.start_time)?.getTime() ?? 0) - (parseApiDate(a.start_time)?.getTime() ?? 0))
  const displayedEpisodes = sortedEpisodes.slice(0, 6)
  const hasMore = sortedEpisodes.length > 6

  return (
    <div className="time-machine-active rounded-xl p-4 space-y-3">
      {/* Top row */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <History className="w-5 h-5 text-amber-700" />
          <span className="font-bold text-amber-800">TimeMachine</span>
          {loading && (
            <Loader2 className="w-4 h-4 text-amber-600 animate-spin" />
          )}
        </div>
        <button
          onClick={onToggle}
          className="flex items-center gap-1.5 bg-white text-amber-700 border border-amber-300 rounded-lg px-3 py-1 text-sm hover:bg-amber-50 transition"
        >
          <Play className="w-3.5 h-3.5" />
          Retour au direct
        </button>
      </div>

      {/* Date/time input */}
      <div className="flex items-center gap-3">
        <label className="text-sm text-amber-700 whitespace-nowrap">
          Voir le dashboard au :
        </label>
        <input
          type="datetime-local"
          value={selectedDate ? format(selectedDate, "yyyy-MM-dd'T'HH:mm") : ''}
          onChange={(e) => {
            if (e.target.value) {
              onDateChange(new Date(e.target.value))
            }
          }}
          className="border border-amber-300 rounded-lg px-3 py-2 text-sm bg-white focus:ring-2 focus:ring-amber-400 focus:outline-none"
        />
      </div>

      {/* Episode shortcuts */}
      {episodes.length > 0 && (
        <div className="space-y-1.5">
          <span className="text-xs text-amber-600 font-medium">
            Épisodes connus :
          </span>
          <div className="flex flex-wrap gap-2">
            {displayedEpisodes.map((episode) => (
              <button
                key={episode.id ?? episode.start_time}
                onClick={() => onDateChange(episodeDate(episode))}
                className="flex items-center gap-1.5 px-2 py-1 text-xs rounded-full bg-white border border-amber-200 hover:bg-amber-50 transition"
              >
                <span
                  className={`w-2 h-2 rounded-full inline-block ${getSeverityDotClass(episode.severity)}`}
                />
                {formatParis(episodeDate(episode), 'dd/MM/yyyy HH:mm')}
              </button>
            ))}
            {hasMore && (
              <span className="flex items-center px-2 py-1 text-xs text-amber-500">
                ...
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
