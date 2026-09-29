import { useCallback, useEffect, useRef, useState } from 'react'
import type { SimulatorClient, SimResponse } from '../api/client'
import { SimulatorError } from '../api/errors'
import { useApi } from './ApiProvider'

export interface QueryState<T> {
  data: T | null
  error: SimulatorError | null
  loading: boolean
  stale: boolean
  receivedAt: number | null
  refresh: () => void
}

/**
 * Loads one resource from the simulator.
 *
 * `refreshKey` lets a caller re-run the query when something external changes —
 * an SSE event, a manual world step, an operator action — without the hook
 * needing to know what that something was.
 */
export function useSimulatorQuery<T>(
  key: string,
  fetcher: (client: SimulatorClient) => Promise<SimResponse<T>>,
  options: { refreshKey?: number; enabled?: boolean } = {},
): QueryState<T> {
  const client = useApi()
  const enabled = options.enabled ?? true
  const refreshKey = options.refreshKey ?? 0

  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<SimulatorError | null>(null)
  const [loading, setLoading] = useState(enabled)
  const [stale, setStale] = useState(false)
  const [receivedAt, setReceivedAt] = useState<number | null>(null)
  const [manualNonce, setManualNonce] = useState(0)

  // Guards against a slow response from an earlier request overwriting a
  // newer one, which is exactly what a latency fault produces.
  const requestId = useRef(0)

  useEffect(() => {
    if (!enabled) {
      setLoading(false)
      return
    }
    const id = ++requestId.current
    let cancelled = false

    setLoading(true)
    fetcher(client)
      .then((response) => {
        if (cancelled || id !== requestId.current) return
        setData(response.data)
        setStale(response.stale)
        setReceivedAt(response.receivedAt)
        setError(null)
      })
      .catch((cause: unknown) => {
        if (cancelled || id !== requestId.current) return
        // The previous data is deliberately kept so a degraded screen can show
        // the last known state, but `error` is set so it is never presented as
        // current.
        setError(
          cause instanceof SimulatorError
            ? cause
            : new SimulatorError(0, 'UNKNOWN', cause instanceof Error ? cause.message : 'Unknown error'),
        )
      })
      .finally(() => {
        if (cancelled || id !== requestId.current) return
        setLoading(false)
      })

    return () => { cancelled = true }
    // `fetcher` is intentionally not a dependency: callers pass inline arrows,
    // which would refetch on every render. `key` identifies the resource.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, key, enabled, refreshKey, manualNonce])

  const refresh = useCallback(() => setManualNonce((n) => n + 1), [])

  return { data, error, loading, stale, receivedAt, refresh }
}
