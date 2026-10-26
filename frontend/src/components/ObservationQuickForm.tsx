import { useRef, useState } from 'react'
import { Camera, CheckCircle, Droplets, Eye, Waves } from 'lucide-react'
import { createObservation, toActionError, type ActionErrorState } from '../api'
import type { ObservationKind } from '../types'
import ActionError from './ActionError'
import { OBSERVATION_KIND_LABELS } from '../utils/observations'

const MAX_PHOTO_BYTES = 8 * 1024 * 1024
const PHOTO_ACCEPT = 'image/jpeg,image/png,image/webp,image/heic,image/heif'
const REFERENCES = ['plancher', 'terrasse', 'seuil du moulin']

const KIND_BUTTONS: { kind: ObservationKind; icon: typeof Waves; idle: string; active: string }[] = [
  { kind: 'flooded', icon: Waves, idle: 'border-red-300 text-red-700', active: 'bg-red-600 border-red-600 text-white' },
  { kind: 'water_rising', icon: Droplets, idle: 'border-orange-300 text-orange-700', active: 'bg-orange-500 border-orange-500 text-white' },
  { kind: 'normal', icon: Eye, idle: 'border-green-300 text-green-700', active: 'bg-green-600 border-green-600 text-white' },
]

/** Local "YYYY-MM-DDTHH:mm" for a datetime-local input. */
function localInputValue(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/**
 * Field observation at the mill in two taps: what is seen (Inondé / L'eau
 * monte / RAS), then optional height, reference, note and photo.
 * Saved with POST /api/observations (API key required).
 */
export default function ObservationQuickForm({ onSaved }: { onSaved?: () => void }) {
  const [kind, setKind] = useState<ObservationKind | null>(null)
  const [observedAt, setObservedAt] = useState('')
  const [height, setHeight] = useState('')
  const [reference, setReference] = useState(REFERENCES[0])
  const [note, setNote] = useState('')
  const [photo, setPhoto] = useState<File | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<ActionErrorState | null>(null)
  const [saved, setSaved] = useState<string | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)

  const select = (next: ObservationKind) => {
    setKind(next)
    setSaved(null)
    setError(null)
    if (!observedAt) setObservedAt(localInputValue(new Date()))
  }

  const reset = () => {
    setKind(null)
    setObservedAt('')
    setHeight('')
    setNote('')
    setPhoto(null)
    if (fileInput.current) fileInput.current.value = ''
  }

  const onPhoto = (file: File | null) => {
    setError(null)
    if (file && file.size > MAX_PHOTO_BYTES) {
      setError({ message: 'Photo trop lourde (8 Mo maximum)', auth: false })
      setPhoto(null)
      if (fileInput.current) fileInput.current.value = ''
      return
    }
    setPhoto(file)
  }

  const submit = async () => {
    if (!kind) return
    const heightValue = height.trim() === '' ? null : Number(height.replace(',', '.'))
    if (heightValue !== null && !Number.isFinite(heightValue)) {
      setError({ message: 'Hauteur invalide (en cm)', auth: false })
      return
    }
    const at = observedAt ? new Date(observedAt) : new Date()
    setSaving(true)
    setError(null)
    try {
      await createObservation(
        {
          kind,
          observed_at: Number.isNaN(at.getTime()) ? null : at.toISOString(),
          water_height_cm: heightValue,
          reference: heightValue !== null ? reference.trim() || null : null,
          note: note.trim() || null,
        },
        photo,
      )
      setSaved(`Observation « ${OBSERVATION_KIND_LABELS[kind]} » enregistrée`)
      reset()
      onSaved?.()
    } catch (err) {
      setError(toActionError(err, "Enregistrement de l'observation impossible"))
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="bg-white rounded-lg shadow p-3 sm:p-4" aria-label="Observation au moulin">
      <h2 className="text-sm font-medium text-gray-700 mb-2">Observation au moulin</h2>
      <div className="grid grid-cols-3 gap-2">
        {KIND_BUTTONS.map(({ kind: k, icon: Icon, idle, active }) => (
          <button
            key={k}
            type="button"
            onClick={() => select(k)}
            aria-pressed={kind === k}
            className={`flex flex-col items-center justify-center gap-1 rounded-xl border-2 px-2 py-4 text-base font-semibold min-h-[4.5rem] ${
              kind === k ? active : `bg-white hover:bg-gray-50 ${idle}`
            }`}
          >
            <Icon className="w-6 h-6" />
            {OBSERVATION_KIND_LABELS[k]}
          </button>
        ))}
      </div>
      <button
        type="button"
        onClick={() => select('near_miss')}
        aria-pressed={kind === 'near_miss'}
        className={`mt-2 text-xs underline ${kind === 'near_miss' ? 'text-orange-700 font-semibold' : 'text-gray-500'}`}
      >
        Presque inondé (l'eau s'est arrêtée juste avant)
      </button>

      {kind && (
        <div className="mt-3 space-y-3">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <label className="block text-sm">
              <span className="text-gray-600">Heure de l'observation</span>
              <input
                type="datetime-local"
                value={observedAt}
                onChange={(e) => setObservedAt(e.target.value)}
                className="mt-1 w-full rounded border border-gray-300 px-2 py-2 text-base"
              />
            </label>
            <div className="grid grid-cols-2 gap-2">
              <label className="block text-sm">
                <span className="text-gray-600">Hauteur (cm)</span>
                <input
                  type="number"
                  inputMode="decimal"
                  step="1"
                  value={height}
                  onChange={(e) => setHeight(e.target.value)}
                  placeholder="optionnel"
                  className="mt-1 w-full rounded border border-gray-300 px-2 py-2 text-base"
                />
              </label>
              <label className="block text-sm">
                <span className="text-gray-600">Par rapport au</span>
                <input
                  list="observation-references"
                  value={reference}
                  onChange={(e) => setReference(e.target.value)}
                  maxLength={100}
                  className="mt-1 w-full rounded border border-gray-300 px-2 py-2 text-base"
                />
                <datalist id="observation-references">
                  {REFERENCES.map((r) => (
                    <option key={r} value={r} />
                  ))}
                </datalist>
              </label>
            </div>
          </div>
          <label className="block text-sm">
            <span className="text-gray-600">Note</span>
            <textarea
              value={note}
              onChange={(e) => setNote(e.target.value)}
              maxLength={2000}
              rows={2}
              placeholder="optionnel"
              className="mt-1 w-full rounded border border-gray-300 px-2 py-2 text-base"
            />
          </label>
          <label className="flex items-center gap-2 text-sm text-gray-600 cursor-pointer">
            <Camera className="w-5 h-5 text-gray-500" />
            <span>{photo ? photo.name : 'Ajouter une photo (8 Mo max, privée)'}</span>
            <input
              ref={fileInput}
              type="file"
              accept={PHOTO_ACCEPT}
              onChange={(e) => onPhoto(e.target.files?.[0] ?? null)}
              className="sr-only"
            />
          </label>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={submit}
              disabled={saving}
              className="flex-1 rounded-lg bg-blue-600 px-4 py-3 text-base font-semibold text-white hover:bg-blue-700 disabled:opacity-50"
            >
              {saving ? 'Enregistrement...' : 'Enregistrer'}
            </button>
            <button
              type="button"
              onClick={reset}
              disabled={saving}
              className="rounded-lg border border-gray-300 px-4 py-3 text-base text-gray-700 hover:bg-gray-50"
            >
              Annuler
            </button>
          </div>
        </div>
      )}

      <ActionError error={error} onDismiss={() => setError(null)} className="mt-3" />
      {saved && (
        <p role="status" className="mt-3 flex items-center gap-2 text-sm text-green-700">
          <CheckCircle className="w-4 h-4" />
          {saved}
        </p>
      )}
    </section>
  )
}
