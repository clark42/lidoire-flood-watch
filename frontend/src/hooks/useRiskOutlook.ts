import { useCallback, useEffect, useState } from 'react'
import { errorMessage, getRiskOutlook } from '../api'
import type { RiskOutlook } from '../types'

const REFRESH_MS = 10 * 60 * 1000

export interface RiskOutlookState {
  data: RiskOutlook | null
  loading: boolean
  /** Error of the last fetch; `data` keeps the previous payload */
  error: string | null
  refresh: () => void
}

/**
 * GET /api/risk/outlook (6 / 24 / 72 h), refreshed every 10 min while the
 * tab is visible (the backend caches it 10 min). `enabled` = false (e.g.
 * TimeMachine) stops the polling.
 */
export function useRiskOutlook(enabled = true): RiskOutlookState {
  const [data, setData] = useState<RiskOutlook | null>(null)
  const [loading, setLoading] = useState(enabled)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      setData(await getRiskOutlook())
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!enabled) return
    refresh()
    const interval = setInterval(() => {
      if (!document.hidden) refresh()
    }, REFRESH_MS)
    return () => clearInterval(interval)
  }, [enabled, refresh])

  return { data, loading, error, refresh }
}
