import { useCallback, useEffect, useRef, useState } from 'react'
import type { Depot, Station, Region, Route, SupplyArrival, DomainEvent, Metrics } from '../../lib/types/simulator'

export interface Snapshot {
  tick: number | null
  sim_time: string | null
  status: string | null
  stale: boolean
  age_seconds: number
  depots: Depot[]
  stations: Station[]
  regions: Region[]
  routes: Route[]
  supply_arrivals: SupplyArrival[]
  events: DomainEvent[]
  metrics: Partial<Metrics>
}
interface Health {
  status: string
  components: Record<string, { status: string; detail?: string }>
}

async function read<T>(path: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(`/api/v1/${path}`, { signal, cache: 'no-store' })
  if (!response.ok) throw new Error(`Request failed (${response.status})`)
  return response.json() as Promise<T>
}

export function useDashboard() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const [health, setHealth] = useState<Health | null>(null)
  const [error, setError] = useState(false)
  const [healthError, setHealthError] = useState(false)
  const [loading, setLoading] = useState(true)
  const [receivedAt, setReceivedAt] = useState<number | null>(null)
  const [now, setNow] = useState(Date.now())
  const active = useRef<AbortController | null>(null)

  const refresh = useCallback(async () => {
    if (active.current) return
    const controller = new AbortController()
    active.current = controller
    setLoading(true)
    const timeout = window.setTimeout(() => controller.abort(), 15000)
    const isCurrent = () => active.current === controller
    await Promise.all([
      read<Snapshot>('network/snapshot', controller.signal).then(data => {
        if (!isCurrent()) return
        setSnapshot(data)
        setReceivedAt(Date.now())
        setNow(Date.now())
        setError(false)
      }).catch(() => { if (isCurrent()) setError(true) }),
      read<Health>('status', controller.signal).then(data => {
        if (!isCurrent()) return
        setHealth(data)
        setHealthError(false)
      }).catch(() => { if (isCurrent()) setHealthError(true) }),
    ])
    window.clearTimeout(timeout)
    if (isCurrent()) {
      active.current = null
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
    const polling = window.setInterval(() => void refresh(), 5000)
    const clock = window.setInterval(() => setNow(Date.now()), 1000)
    return () => {
      window.clearInterval(polling)
      window.clearInterval(clock)
      const controller = active.current
      active.current = null
      controller?.abort()
    }
  }, [refresh])

  const age = snapshot && receivedAt !== null
    ? Math.max(0, snapshot.age_seconds + (now - receivedAt) / 1000) : null
  return { snapshot, health, error, healthError, loading, refresh, age,
    stale: Boolean(snapshot && (snapshot.stale || error || (age !== null && age > 30))) }
}
