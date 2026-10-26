import { useEffect, useState } from 'react'
import { Lightbulb } from 'lucide-react'
import {
  confirmEpisode,
  errorMessage,
  getEpisodeObservations,
  toActionError,
  type ActionErrorState,
} from '../api'
import type { EpisodeObservations, FloodEpisode } from '../types'
import { formatParis } from '../utils/time'
import ActionError from './ActionError'
import { ObservationItem } from './ObservationList'

const SUGGESTED_LABELS: Record<string, string> = {
  flooded: 'Inondé',
  near_miss: 'Presque',
}

/**
 * Observations inside the episode window. For an episode without outcome,
 * a flooded / near_miss observation is offered as its outcome: applied only
 * when the user clicks (POST /api/episodes/confirm/{id}).
 */
export default function EpisodeObservationsPanel({
  episode,
  onApplied,
}: {
  episode: FloodEpisode
  onApplied: (updated: FloodEpisode) => void
}) {
  const [data, setData] = useState<EpisodeObservations | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [applying, setApplying] = useState(false)
  const [actionError, setActionError] = useState<ActionErrorState | null>(null)
  const episodeId = episode.id
  const outcome = episode.outcome

  useEffect(() => {
    if (episodeId == null) return
    let cancelled = false
    getEpisodeObservations(episodeId)
      .then((d) => {
        if (!cancelled) {
          setData(d)
          setError(null)
        }
      })
      .catch((err) => {
        if (!cancelled) setError(errorMessage(err))
      })
    return () => {
      cancelled = true
    }
  }, [episodeId, outcome])

  if (error) return <p className="text-xs text-red-600">Observations indisponibles : {error}</p>
  if (!data || data.episode_id !== episodeId || data.observations.length === 0) return null

  const suggestion = data.suggested_outcome
  const apply = async () => {
    if (!suggestion || episodeId == null) return
    setApplying(true)
    setActionError(null)
    try {
      const result = await confirmEpisode(episodeId, suggestion.outcome, {
        observedAt: suggestion.observed_at,
        waterHeightCm: suggestion.water_height_cm,
        source: 'observation',
      })
      onApplied(result.episode)
    } catch (err) {
      setActionError(toActionError(err, "Application de l'issue impossible"))
    } finally {
      setApplying(false)
    }
  }

  return (
    <div>
      <h4 className="font-medium mb-1 text-sm text-gray-600">Observations au moulin</h4>
      <p className="text-xs text-gray-400 mb-1">
        Fenêtre {formatParis(data.window_start, 'dd/MM HH:mm')} - {formatParis(data.window_end, 'dd/MM HH:mm')}
      </p>
      <ul className="divide-y">
        {data.observations.map((o) => (
          <ObservationItem key={o.id} observation={o} />
        ))}
      </ul>
      {suggestion && (
        <div className="mt-2 bg-blue-50 border border-blue-200 rounded-lg p-3 text-sm text-blue-900">
          <p className="flex items-start gap-2">
            <Lightbulb className="w-4 h-4 mt-0.5 flex-shrink-0" />
            <span>
              Issue proposée : <strong>{SUGGESTED_LABELS[suggestion.outcome] ?? suggestion.outcome}</strong>{' '}
              (observation du {formatParis(suggestion.observed_at, 'dd MMM HH:mm')}
              {suggestion.water_height_cm != null ? `, ${suggestion.water_height_cm} cm` : ''}).
            </span>
          </p>
          <button
            type="button"
            onClick={apply}
            disabled={applying}
            className="mt-2 px-3 py-1.5 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
          >
            {applying ? 'Application...' : "Appliquer à l'épisode"}
          </button>
          <ActionError error={actionError} onDismiss={() => setActionError(null)} className="mt-2" />
        </div>
      )}
    </div>
  )
}
