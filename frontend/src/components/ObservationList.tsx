import { useCallback, useEffect, useState } from 'react'
import { Camera, RefreshCw, Trash2 } from 'lucide-react'
import {
  deleteObservation,
  errorMessage,
  getObservationPhoto,
  getObservations,
  toActionError,
  type ActionErrorState,
} from '../api'
import type { Observation } from '../types'
import { formatParis } from '../utils/time'
import { OBSERVATION_KIND_BADGES, OBSERVATION_KIND_LABELS } from '../utils/observations'
import ActionError from './ActionError'

/** Private photo: fetched with the API key, shown through an object URL. */
function ObservationPhoto({ url }: { url: string }) {
  const [src, setSrc] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => () => {
    if (src) URL.revokeObjectURL(src)
  }, [src])

  if (src) {
    return <img src={src} alt="Photo de l'observation" className="mt-2 max-h-64 rounded border object-contain" />
  }
  return (
    <span className="inline-flex items-center gap-2">
      <button
        type="button"
        disabled={loading}
        onClick={async () => {
          setLoading(true)
          setError(null)
          try {
            setSrc(URL.createObjectURL(await getObservationPhoto(url)))
          } catch (err) {
            setError(errorMessage(err))
          } finally {
            setLoading(false)
          }
        }}
        className="inline-flex items-center gap-1 text-xs text-blue-600 hover:underline disabled:opacity-50"
      >
        <Camera className="w-3 h-3" />
        {loading ? 'Chargement...' : 'Voir la photo'}
      </button>
      {error && <span className="text-xs text-red-600">{error}</span>}
    </span>
  )
}

export function ObservationItem({
  observation,
  onDelete,
  deleting,
}: {
  observation: Observation
  onDelete?: (o: Observation) => void
  deleting?: boolean
}) {
  const o = observation
  return (
    <li className="py-2 flex items-start gap-3">
      <span className={`px-2 py-0.5 rounded text-xs font-medium flex-shrink-0 ${OBSERVATION_KIND_BADGES[o.kind]}`}>
        {OBSERVATION_KIND_LABELS[o.kind]}
      </span>
      <div className="flex-1 min-w-0 text-sm">
        <div className="text-gray-700">
          {formatParis(o.observed_at, 'dd MMM yyyy HH:mm')}
          {o.water_height_cm != null && (
            <span className="text-gray-500">
              {' '}· {o.water_height_cm} cm{o.reference ? ` / ${o.reference}` : ''}
            </span>
          )}
        </div>
        {o.note && <p className="text-xs text-gray-600 whitespace-pre-wrap break-words">{o.note}</p>}
        {o.has_photo && o.photo_url && <ObservationPhoto url={o.photo_url} />}
      </div>
      {onDelete && (
        <button
          type="button"
          onClick={() => onDelete(o)}
          disabled={deleting}
          className="text-gray-400 hover:text-red-600 disabled:opacity-50"
          aria-label="Supprimer l'observation"
        >
          <Trash2 className="w-4 h-4" />
        </button>
      )}
    </li>
  )
}

/** Field observations reported at the mill, newest first (Episodes page). */
export default function ObservationList() {
  const [observations, setObservations] = useState<Observation[]>([])
  const [loading, setLoading] = useState(true)
  const [listError, setListError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<ActionErrorState | null>(null)
  const [deletingId, setDeletingId] = useState<number | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setListError(null)
    try {
      setObservations(await getObservations())
    } catch (err) {
      setListError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const handleDelete = async (o: Observation) => {
    if (!window.confirm('Supprimer cette observation (et sa photo) ?')) return
    setDeletingId(o.id)
    setActionError(null)
    try {
      await deleteObservation(o.id)
      setObservations((prev) => prev.filter((x) => x.id !== o.id))
    } catch (err) {
      setActionError(toActionError(err, 'Suppression impossible'))
    } finally {
      setDeletingId(null)
    }
  }

  return (
    <section className="bg-white rounded-lg shadow">
      <div className="p-4 border-b bg-gray-50 flex items-center justify-between">
        <h3 className="font-medium">Observations au moulin</h3>
        <button
          type="button"
          onClick={load}
          className="text-gray-500 hover:text-gray-700"
          aria-label="Rafraichir les observations"
        >
          <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
        </button>
      </div>
      <div className="p-4">
        <ActionError error={actionError} onDismiss={() => setActionError(null)} className="mb-3" />
        {listError ? (
          <p className="text-sm text-red-600">Chargement des observations impossible : {listError}</p>
        ) : !loading && observations.length === 0 ? (
          <p className="text-sm text-gray-500">
            Aucune observation. Le formulaire « Observation au moulin » du tableau de bord en ajoute.
          </p>
        ) : (
          <ul className="divide-y">
            {observations.map((o) => (
              <ObservationItem key={o.id} observation={o} onDelete={handleDelete} deleting={deletingId === o.id} />
            ))}
          </ul>
        )}
      </div>
    </section>
  )
}
