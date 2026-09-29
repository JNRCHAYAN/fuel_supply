import { useEffect, useRef, useState } from 'react'
import type { StreamHandle } from '../api/client'
import { SimulatorError } from '../api/errors'
import { useApi } from './ApiProvider'

export type StreamStatus = 'connecting' | 'connected' | 'reconnecting' | 'unavailable'

/**
 * Owns the SSE connection.
 *
 * Guide 6.2: there is no Last-Event-ID replay, so a reconnect only delivers
 * events from that moment forward. `onReconnect` is therefore not a nicety —
 * it is the only way the app learns what it missed.
 */
export function useStream(options: { onEvent?: (name: string) => void; onReconnect?: () => void } = {}) {
  const client = useApi()
  const [status, setStatus] = useState<StreamStatus>('connecting')
  const [lastEventAt, setLastEventAt] = useState<number | null>(null)
  const [error, setError] = useState<SimulatorError | null>(null)

  const handlers = useRef(options)
  handlers.current = options

  useEffect(() => {
    let handle: StreamHandle | null = null
    let disposed = false

    const connect = (isReconnect: boolean) => {
      if (disposed) return
      setStatus(isReconnect ? 'reconnecting' : 'connecting')
      try {
        handle = client.stream()
      } catch (cause) {
        setStatus('unavailable')
        setError(cause instanceof SimulatorError ? cause : null)
        return
      }
      setStatus('connected')
      setError(null)
      if (isReconnect) handlers.current.onReconnect?.()

      handle.on('simulation.tick', () => {
        setLastEventAt(Date.now())
        handlers.current.onEvent?.('simulation.tick')
      })
      handle.on('allocation.status_changed', () => {
        setLastEventAt(Date.now())
        handlers.current.onEvent?.('allocation.status_changed')
      })
      handle.on('inventory.updated', () => {
        setLastEventAt(Date.now())
        handlers.current.onEvent?.('inventory.updated')
      })
      handle.on('simulator.notice', () => {
        setLastEventAt(Date.now())
        handlers.current.onEvent?.('simulator.notice')
      })
    }

    const onVisibility = () => { if (!document.hidden) connect(true) }
    document.addEventListener('visibilitychange', onVisibility)
    connect(false)

    return () => {
      disposed = true
      document.removeEventListener('visibilitychange', onVisibility)
      handle?.close()
    }
  }, [client])

  return { status, lastEventAt, error }
}
