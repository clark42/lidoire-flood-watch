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
})
