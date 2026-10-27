import { Link } from 'react-router-dom'
import { AlertTriangle, X } from 'lucide-react'
import type { ActionErrorState } from '../api'

/**
 * Visible error of a user action (save, delete, simulation...).
 * On 401/403 it links to the API key panel of the Configuration page.
 */
export default function ActionError({
  error,
  onDismiss,
  className = '',
}: {
  error: ActionErrorState | null
  onDismiss?: () => void
  className?: string
}) {
  if (!error) return null
  return (
    <div
      role="alert"
      className={`bg-red-50 border border-red-200 text-red-800 rounded-lg px-3 py-2 text-sm flex items-start gap-2 ${className}`}
    >
      <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0 text-red-500" />
      <div className="flex-1 min-w-0">
        <p className="break-words">{error.message}</p>
        {error.auth && (
          <p className="mt-1">
            <Link to="/config#api-key" className="font-medium text-red-700 underline hover:text-red-900">
              Renseigner la clé API
            </Link>
          </p>
        )}
      </div>
      {onDismiss && (
        <button
          type="button"
          onClick={onDismiss}
          className="text-red-500 hover:text-red-700"
          aria-label="Fermer le message d'erreur"
        >
          <X className="w-4 h-4" />
        </button>
      )}
    </div>
  )
}
