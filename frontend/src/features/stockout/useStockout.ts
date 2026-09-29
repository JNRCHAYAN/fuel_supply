import { useCallback, useEffect, useRef, useState } from 'react'
import type { StockoutResponse } from '../../lib/types/stockout'

async function read(path: string, signal: AbortSignal): Promise<unknown> {
  const response = await fetch(`/api/v1/${path}`, { signal, cache: 'no-store' })
  if (!response.ok) throw new Error(`Request failed (${response.status})`)
  return response.json() as Promise<unknown>
}

// A response we cannot read is an error, not an empty console: a malformed body
// must never blank the panel by replacing good data with a partial payload.
function isStockoutResponse(value: unknown): value is StockoutResponse {
  if (typeof value !== 'object' || value === null) return false
  return Array.isArray((value as { assessments?: unknown }).assessments)
}

export function useStockout(): {
  data: StockoutResponse | null
  error: boolean
  loading: boolean
  refresh: () => void
} {
  const [data, setData] = useState<StockoutResponse | null>(null)
  const [error, setError] = useState(false)
  const [loading, setLoading] = useState(true)
  const active = useRef<AbortController | null>(null)

  const refresh = useCallback(() => {
    if (active.current) return
    const controller = new AbortController()
    active.current = controller
    setLoading(true)
    const timeout = window.setTimeout(() => controller.abort(), 15000)
    const isCurrent = () => active.current === controller
    void read('stockout', controller.signal)
      .then(body => {
        if (!isCurrent()) return
        if (!isStockoutResponse(body)) throw new Error('Malformed stockout payload')
        setData(body)
        setError(false)
      })
      .catch(() => { if (isCurrent()) setError(true) })
      .finally(() => {
        window.clearTimeout(timeout)
        if (isCurrent()) {
          active.current = null
          setLoading(false)
        }
      })
  }, [])

  useEffect(() => {
    void refresh()
    const polling = window.setInterval(() => refresh(), 5000)
    return () => {
      window.clearInterval(polling)
      const controller = active.current
      active.current = null
      controller?.abort()
    }
  }, [refresh])

  return { data, error, loading, refresh }
}
