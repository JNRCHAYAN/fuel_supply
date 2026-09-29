import { useSimulatorQuery } from './useSimulatorQuery'
import type { Health } from '../types/simulator'

export type SystemState = 'normal' | 'degraded' | 'offline'

export interface HealthState {
  state: SystemState
  health: Health | null
  error: Error | null
  refresh: () => void
}

/**
 * Polls liveness and reachability, which are two different questions.
 *
 * Guide 7.10 exempts /v1/health from fault injection, so health alone reports
 * "normal" for the entire duration of an `unavailable` fault — exactly when
 * the degraded-mode behaviour is being judged. Health is therefore paired with
 * a real data route:
 *
 *   health ok  + data ok   -> normal
 *   health ok  + data fails -> degraded  (the API is up; requests are being faulted)
 *   health fails            -> offline   (the simulator is genuinely gone)
 */
export function useHealth(): HealthState {
  const health = useSimulatorQuery<Health>('health', (client) => client.getHealth())
  const probe = useSimulatorQuery<unknown>('health-probe', (client) => client.getInstance())

  const state: SystemState = health.error
    ? 'offline'
    : probe.error
      ? 'degraded'
      : 'normal'

  return { state, health: health.data, error: health.error ?? probe.error, refresh: health.refresh }
}
