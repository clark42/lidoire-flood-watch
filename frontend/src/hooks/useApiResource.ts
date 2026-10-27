import { useCallback, useEffect, useRef, useState } from 'react'
import { errorMessage } from '../api'

// Several panels read the same endpoint (the map and the analog cards both
// need /api/risk/analogs): one request in flight per key, and a short reuse
// window, so that mounting them together costs a single call.
const REUSE_MS = 30_000
const inflight = new Map<string, Promise<unknown>>()
const recent = new Map<string, { at: number; data: unknown }>()

function load<T>(key: string, fetcher: () => Promise<T>, force: boolean): Promise<T> {
  const hit = recent.get(key)
  if (!force && hit && Date.now() - hit.at < REUSE_MS) return Promise.resolve(hit.data as T)
  const pending = inflight.get(key)
  if (pending) return pending as Promise<T>
  const p = fetcher()
    .then((data) => {
      recent.set(key, { at: Date.now(), data })
      return data
    })
    .finally(() => inflight.delete(key))
  inflight.set(key, p)
  return p
}

export interface ApiResource<T> {
  data: T | null
  /** Error of the last fetch; `data` keeps the previous payload */
  error: string | null
  loading: boolean
  refresh: () => void
}

/**
 * GET an endpoint keyed by `key` (null: not fetched), refreshed every
 * `refreshMs` while the tab is visible. Requests are shared between hooks
 * using the same key.
 */
export function useApiResource<T>(
  key: string | null,
  fetcher: () => Promise<T>,
  refreshMs?: number,
): ApiResource<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(key !== null)
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const run = useCallback(
    async (force: boolean, isCurrent: () => boolean = () => true) => {
      if (key === null) return
      setLoading(true)
      try {
        const value = await load(key, () => fetcherRef.current(), force)
        if (!isCurrent()) return
        setData(value)
        setError(null)
      } catch (e) {
        if (isCurrent()) setError(errorMessage(e))
      } finally {
        if (isCurrent()) setLoading(false)
      }
    },
    [key],
  )

  // A new key (another date) never shows the payload of the previous one
  const shownKey = useRef(key)
  useEffect(() => {
    if (shownKey.current !== key) {
      shownKey.current = key
      setData(null)
      setError(null)
    }
    if (key === null) return
    let alive = true
    const isCurrent = () => alive
    void run(false, isCurrent)
    const interval = refreshMs
      ? setInterval(() => {
          if (!document.hidden) void run(true, isCurrent)
        }, refreshMs)
      : null
    return () => {
      alive = false
      if (interval) clearInterval(interval)
    }
  }, [key, refreshMs, run])

  const refresh = useCallback(() => void run(true), [run])
  return { data, error, loading, refresh }
}
