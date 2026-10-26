import { useRef, useState } from 'react'
import { Camera, CheckCircle, Droplets, Eye, ImagePlus, Waves, X } from 'lucide-react'
import { createObservation, toActionError, type ActionErrorState } from '../api'
import type { ObservationKind } from '../types'
import ActionError from './ActionError'
import { OBSERVATION_KIND_LABELS } from '../utils/observations'

const MAX_PHOTO_BYTES = 8 * 1024 * 1024
const REFERENCES = ['plancher', 'terrasse', 'seuil du moulin']

// Mobile first: three large targets, a shape (icon) and a word for each,
// the colour only on the frame of the chosen one.
const KIND_BUTTONS: { kind: ObservationKind; icon: typeof Waves; active: string; iconColor: string }[] = [
  { kind: 'flooded', icon: Waves, active: 'border-silt bg-silt-soft', iconColor: 'text-silt' },
  { kind: 'water_rising', icon: Droplets, active: 'border-lv-orange bg-lv-orange-soft', iconColor: 'text-lv-orange' },
  { kind: 'normal', icon: Eye, active: 'border-lv-green bg-lv-green-soft', iconColor: 'text-lv-green' },
]

const FIELD =
  'mt-1 w-full rounded-lg border border-line-2 bg-canvas px-2.5 py-2 text-base text-ink placeholder:text-ink-3'
const BIG_BUTTON =
  'flex min-h-[52px] items-center justify-center gap-2 rounded-lg border px-3 text-[15px] font-bold'

/** Local "YYYY-MM-DDTHH:mm" for a datetime-local input. */
function localInputValue(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/**
 * Field observation at the mill in two taps: what is seen (Inondé / L'eau
 * monte / RAS), then optional height, reference, note and photo (camera or
 * gallery). Saved with POST /api/observations (API key required).
 */
export default function ObservationQuickForm({
  onSaved,
  initialKind = null,
}: {
  onSaved?: () => void
  /** Kind chosen on open (e.g. a link from a notification) */
  initialKind?: ObservationKind | null
}) {
  const [kind, setKind] = useState<ObservationKind | null>(initialKind)
  const [observedAt, setObservedAt] = useState(() => (initialKind ? localInputValue(new Date()) : ''))
  const [height, setHeight] = useState('')
  const [reference, setReference] = useState(REFERENCES[0])
  const [note, setNote] = useState('')
  const [photo, setPhoto] = useState<File | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<ActionErrorState | null>(null)
  const [saved, setSaved] = useState<string | null>(null)
  const cameraInput = useRef<HTMLInputElement>(null)
  const galleryInput = useRef<HTMLInputElement>(null)

  const select = (next: ObservationKind) => {
    setKind(next)
    setSaved(null)
    setError(null)
    if (!observedAt) setObservedAt(localInputValue(new Date()))
  }

  const clearPhoto = () => {
    setPhoto(null)
    if (cameraInput.current) cameraInput.current.value = ''
    if (galleryInput.current) galleryInput.current.value = ''
  }

  const reset = () => {
    setKind(null)
    setObservedAt('')
    setHeight('')
    setNote('')
    clearPhoto()
  }

  const onPhoto = (file: File | null) => {
    setError(null)
    // Some phones send HEIC without a MIME type: only a known non-image is refused
    if (file && file.type && !file.type.startsWith('image/')) {
      setError({ message: 'Ce fichier n’est pas une image', auth: false })
      clearPhoto()
      return
    }
    if (file && file.size > MAX_PHOTO_BYTES) {
      setError({ message: 'Photo trop lourde (8 Mo maximum)', auth: false })
      clearPhoto()
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
    <div className="min-w-0 rounded-card border border-line bg-surface p-4" role="group" aria-labelledby="observation-title">
      <h3 id="observation-title" className="font-display text-[19px] font-semibold tracking-[0.01em]">
        Relevé au moulin
      </h3>
      <p className="text-[13px] text-ink-3">
        Ce que vous voyez sur place : la seule mesure du moulin, elle sert à labelliser les épisodes.
      </p>
      <div className="mt-3 grid grid-cols-3 gap-2">
        {KIND_BUTTONS.map(({ kind: k, icon: Icon, active, iconColor }) => (
          <button
            key={k}
            type="button"
            onClick={() => select(k)}
            aria-pressed={kind === k}
            className={`flex min-h-[76px] flex-col items-center justify-center gap-1 rounded-xl border-2 px-1 py-3 text-[15px] font-bold text-ink ${
              kind === k ? active : 'border-line-2 bg-surface hover:bg-surface-2'
            }`}
          >
            <Icon className={`h-6 w-6 ${iconColor}`} aria-hidden="true" />
            {OBSERVATION_KIND_LABELS[k]}
          </button>
        ))}
      </div>
      <button
        type="button"
        onClick={() => select('near_miss')}
        aria-pressed={kind === 'near_miss'}
        className={`mt-2 min-h-[44px] w-full rounded-lg border px-3 text-left text-[14px] ${
          kind === 'near_miss' ? 'border-silt bg-silt-soft font-bold text-ink' : 'border-dashed border-line-2 text-ink-2'
        }`}
      >
        Presque inondé : l’eau s’est arrêtée juste avant
      </button>

      {kind && (
        <div className="mt-3 space-y-3">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="block text-[13px] text-ink-2">
              Heure de l’observation
              <input type="datetime-local" value={observedAt} onChange={(e) => setObservedAt(e.target.value)} className={FIELD} />
            </label>
            <div className="grid grid-cols-2 gap-2">
              <label className="block text-[13px] text-ink-2">
                Hauteur d’eau (cm)
                <input
                  type="number"
                  inputMode="decimal"
                  step="1"
                  value={height}
                  onChange={(e) => setHeight(e.target.value)}
                  placeholder="optionnel"
                  className={FIELD}
                />
              </label>
              <label className="block text-[13px] text-ink-2">
                Par rapport au
                <input
                  list="observation-references"
                  value={reference}
                  onChange={(e) => setReference(e.target.value)}
                  maxLength={100}
                  className={FIELD}
                />
                <datalist id="observation-references">
                  {REFERENCES.map((r) => (
                    <option key={r} value={r} />
                  ))}
                </datalist>
              </label>
            </div>
          </div>
          <label className="block text-[13px] text-ink-2">
            Note
            <textarea
              value={note}
              onChange={(e) => setNote(e.target.value)}
              maxLength={2000}
              rows={2}
              placeholder="optionnel"
              className={FIELD}
            />
          </label>

          {photo ? (
            <div className="flex min-h-[52px] items-center gap-2 rounded-lg border border-line-2 bg-canvas px-3 text-[14px]">
              <Camera className="h-5 w-5 flex-none text-ink-3" aria-hidden="true" />
              <span className="min-w-0 flex-1 truncate">{photo.name || 'photo'}</span>
              <button
                type="button"
                onClick={clearPhoto}
                className="grid h-11 w-11 flex-none place-items-center rounded-lg text-ink-2 hover:bg-surface-2"
                aria-label="Retirer la photo"
              >
                <X className="h-5 w-5" aria-hidden="true" />
              </button>
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-2">
              {/* capture: opens the camera on a phone; the second input keeps the gallery */}
              <label className={`${BIG_BUTTON} cursor-pointer border-line-2 bg-surface text-ink focus-within:outline focus-within:outline-2 focus-within:outline-accent`}>
                <Camera className="h-5 w-5" aria-hidden="true" />
                Prendre une photo
                <input
                  ref={cameraInput}
                  type="file"
                  accept="image/*"
                  capture="environment"
                  onChange={(e) => onPhoto(e.target.files?.[0] ?? null)}
                  className="sr-only"
                />
              </label>
              <label className={`${BIG_BUTTON} cursor-pointer border-line-2 bg-surface text-ink focus-within:outline focus-within:outline-2 focus-within:outline-accent`}>
                <ImagePlus className="h-5 w-5" aria-hidden="true" />
                Choisir une photo
                <input
                  ref={galleryInput}
                  type="file"
                  accept="image/*"
                  onChange={(e) => onPhoto(e.target.files?.[0] ?? null)}
                  className="sr-only"
                />
              </label>
            </div>
          )}
          <p className="text-[12px] text-ink-3">Photo privée, 8 Mo maximum, lue seulement avec la clé API.</p>

          <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-2">
            <button
              type="button"
              onClick={submit}
              disabled={saving}
              className={`${BIG_BUTTON} border-accent bg-accent text-accent-ink disabled:opacity-50`}
            >
              {saving ? 'Enregistrement…' : 'Enregistrer'}
            </button>
            <button
              type="button"
              onClick={reset}
              disabled={saving}
              className={`${BIG_BUTTON} border-line-2 bg-surface font-normal text-ink-2 hover:bg-surface-2`}
            >
              Annuler
            </button>
          </div>
        </div>
      )}

      <ActionError error={error} onDismiss={() => setError(null)} className="mt-3" />
      {saved && (
        <p role="status" className="mt-3 flex items-center gap-2 text-[14px] text-ink">
          <CheckCircle className="h-5 w-5 text-lv-green" aria-hidden="true" />
          {saved}
        </p>
      )}
    </div>
  )
}
