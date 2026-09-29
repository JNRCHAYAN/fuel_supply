import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { StockoutResponse } from '../../lib/types/stockout'
import { useStockout } from './useStockout'

const BODY: StockoutResponse = {
  horizon_ticks: 8,
  generated_at_tick: 9060,
  count: 1,
  thresholds: { critical_ticks: 1, high_ticks: 3, safety_stock_fraction: 0.25, horizon_ticks: 8 },
  summary: { CRITICAL: 1, HIGH: 0, MEDIUM: 0, LOW: 0 },
  assessments: [],
  simulated: true,
}

function json(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status }))
}

/** Flush the fetch -> json -> setState promise chain without touching timers. */
async function flush() {
  await act(async () => { await Promise.resolve() })
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('useStockout', () => {
  it('reads the stockout route and exposes the parsed payload as data', async () => {
    const fetchMock = vi.fn(() => json(BODY))
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useStockout())
    expect(result.current.loading).toBe(true)

    await waitFor(() => expect(result.current.data).toEqual(BODY))
    expect(result.current.error).toBe(false)
    expect(result.current.loading).toBe(false)
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/stockout',
      expect.objectContaining({ cache: 'no-store' }),
    )
  })

  it('sets error on a non-ok response and clears it on the next success', async () => {
    let failing = true
    const fetchMock = vi.fn(() =>
      failing ? Promise.resolve(new Response('down', { status: 503 })) : json(BODY))
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useStockout())
    await waitFor(() => expect(result.current.error).toBe(true))
    // A failed read must not invent a payload.
    expect(result.current.data).toBeNull()

    failing = false
    act(() => { result.current.refresh() })
    await waitFor(() => expect(result.current.data).toEqual(BODY))
    expect(result.current.error).toBe(false)
  })

  const malformed: Array<[string, unknown]> = [
    ['a bare string', 'not an object'],
    ['null', null],
    ['an object whose assessments is not an array', { assessments: 'nope' }],
  ]

  it.each(malformed)('rejects %s as a failure rather than as data', async (_label, body) => {
    const fetchMock = vi.fn(() => json(body))
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useStockout())
    await waitFor(() => expect(result.current.error).toBe(true))
    expect(result.current.data).toBeNull()
    expect(result.current.loading).toBe(false)
  })

  it('keeps the last good payload when a later body is malformed', async () => {
    let body: unknown = BODY
    const fetchMock = vi.fn(() => json(body))
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useStockout())
    await waitFor(() => expect(result.current.data).toEqual(BODY))

    body = { assessments: 42 }
    act(() => { result.current.refresh() })
    await waitFor(() => expect(result.current.error).toBe(true))
    // The console keeps the last projection rather than blanking on a partial read.
    expect(result.current.data).toEqual(BODY)
  })

  it('polls the route again after the 5s interval', async () => {
    vi.useFakeTimers()
    try {
      const fetchMock = vi.fn(() => json(BODY))
      vi.stubGlobal('fetch', fetchMock)

      renderHook(() => useStockout())
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      expect(fetchMock).toHaveBeenCalledTimes(1)

      await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
      expect(fetchMock).toHaveBeenCalledTimes(2)

      await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
      expect(fetchMock).toHaveBeenCalledTimes(3)
    } finally {
      vi.useRealTimers()
    }
  })

  it('refetches on demand', async () => {
    const fetchMock = vi.fn(() => json(BODY))
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useStockout())
    await waitFor(() => expect(result.current.data).toEqual(BODY))
    expect(fetchMock).toHaveBeenCalledTimes(1)

    act(() => { result.current.refresh() })
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
  })

  it('aborts the in-flight request on unmount without setting state afterwards', async () => {
    const signals: AbortSignal[] = []
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
      const signal = init?.signal as AbortSignal
      signals.push(signal)
      signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
    }))
    vi.stubGlobal('fetch', fetchMock)
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})

    const { unmount } = renderHook(() => useStockout())
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(signals[0]).toBeInstanceOf(AbortSignal)
    expect(signals[0].aborted).toBe(false)

    unmount()

    // The rejected read must be swallowed, not land as a state update on a gone component.
    await flush()
    expect(signals[0].aborted).toBe(true)
    expect(consoleError).not.toHaveBeenCalled()
  })
})
