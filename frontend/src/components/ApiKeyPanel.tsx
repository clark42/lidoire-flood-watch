import { useState } from 'react'
import { KeyRound } from 'lucide-react'
import { getApiKey, setApiKey } from '../api'

/**
 * API key used for mutating requests (POST/PUT/DELETE).
 * Stored in this browser's localStorage only, sent as X-API-Key.
 */
export default function ApiKeyPanel() {
  const [stored, setStored] = useState<string | null>(() => getApiKey())
  const [draft, setDraft] = useState('')
  const [visible, setVisible] = useState(false)

  const save = () => {
    const value = draft.trim()
    if (!value) return
    setApiKey(value)
    setStored(getApiKey())
    setDraft('')
  }

  const clear = () => {
    setApiKey(null)
    setStored(getApiKey())
    setDraft('')
  }

  const masked = stored
    ? stored.length > 4
      ? `${'•'.repeat(Math.min(stored.length - 4, 12))}${stored.slice(-4)}`
      : '••••'
    : null

  return (
    <div className="bg-white rounded-lg shadow">
      <div className="p-4 border-b">
        <h3 className="font-medium flex items-center gap-2">
          <KeyRound className="w-5 h-5" />
          Cle API (actions d'ecriture)
        </h3>
      </div>
      <div className="p-4 space-y-3">
        <p className="text-sm text-gray-500">
          Requise par le backend pour creer, modifier ou supprimer des episodes et lancer
          backfill / detection. Conservee uniquement dans ce navigateur et envoyee dans
          l'en-tete <code className="text-xs bg-gray-100 px-1 rounded">X-API-Key</code>.
        </p>
        <div className="text-sm">
          <span className="text-gray-500">Cle actuelle : </span>
          {masked ? (
            <span className="font-mono">{visible ? stored : masked}</span>
          ) : (
            <span className="text-gray-400">aucune</span>
          )}
          {stored && (
            <button
              type="button"
              onClick={() => setVisible(v => !v)}
              className="ml-2 text-xs text-blue-600 hover:underline"
            >
              {visible ? 'masquer' : 'afficher'}
            </button>
          )}
        </div>
        <form
          className="flex flex-wrap gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            save()
          }}
        >
          <input
            type="password"
            autoComplete="off"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Nouvelle cle API"
            aria-label="Nouvelle cle API"
            className="flex-1 min-w-0 border rounded px-3 py-2 text-sm"
          />
          <button
            type="submit"
            disabled={!draft.trim()}
            className="px-3 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50"
          >
            Enregistrer
          </button>
          <button
            type="button"
            onClick={clear}
            disabled={!stored}
            className="px-3 py-2 border rounded text-sm hover:bg-gray-50 disabled:opacity-50"
          >
            Effacer
          </button>
        </form>
      </div>
    </div>
  )
}
