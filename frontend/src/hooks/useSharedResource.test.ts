import { beforeEach, describe, expect, it, vi } from 'vitest'
import { loadResource, peekResource, resetSharedResources } from './useSharedResource'

describe('shared resources (one fetch per endpoint for every panel)', () => {
  beforeEach(() => resetSharedResources())

  it('joins a fetch in flight instead of starting another', async () => {
    const fetcher = vi.fn(() => Promise.resolve({ v: 1 }))
    await Promise.all([loadResource('k', fetcher, 60_000), loadResource('k', fetcher, 60_000)])
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(peekResource('k')).toEqual({ data: { v: 1 }, error: null, loading: false })
  })

  it('reuses a payload younger than its refresh period, refetches an older one', async () => {
    const fetcher = vi.fn(() => Promise.resolve(1))
    await loadResource('k', fetcher, 60_000)
    await loadResource('k', fetcher, 60_000)
    expect(fetcher).toHaveBeenCalledTimes(1)
    await loadResource('k', fetcher, 60_000, Date.now() + 61_000)
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('keeps the previous payload when a refresh fails', async () => {
    await loadResource('k', () => Promise.resolve('ok'), 0)
    await loadResource('k', () => Promise.reject(new Error('HTTP 503')), 0)
    expect(peekResource('k')).toEqual({ data: 'ok', error: 'HTTP 503', loading: false })
  })

  it('fetches a static payload (no refresh period) once per page load', async () => {
    const fetcher = vi.fn(() => Promise.resolve('basin'))
    await loadResource('geo/basin', fetcher, Infinity)
    await loadResource('geo/basin', fetcher, Infinity, Date.now() + 24 * 3600_000)
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it('keeps one entry per parameterised key (a date never shows another one)', async () => {
    await loadResource('risk/analogs@now', () => Promise.resolve('now'), 60_000)
    await loadResource('risk/analogs@2024-02-26', () => Promise.resolve('then'), 60_000)
    expect(peekResource('risk/analogs@now').data).toBe('now')
    expect(peekResource('risk/analogs@2024-02-26').data).toBe('then')
  })
})
