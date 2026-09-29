import type { SimulatorClient, SimResponse, StreamHandle } from '../client'
import type { StreamEventName } from '../mock/emitter'
import { SimulatorError } from '../errors'

export interface HttpOptions { baseUrl: string }

// Arrays are objects, but they are not records here: FastAPI's validation
// envelope is {"detail": [ ... ]} and must not be mistaken for the
// {"detail": {"code", "message"}} domain envelope.
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * Normalises the Guide's three distinct error envelopes (Guide 9) into one
 * SimulatorError. Getting this wrong turns a clear 409 into `undefined`, so
 * each shape is handled explicitly rather than probed with a generic unwrap.
 */
function toSimulatorError(status: number, body: unknown): SimulatorError {
  // Injected faults: {"error": {"code", "message"}}
  if (isRecord(body) && isRecord(body.error)) {
    const code = typeof body.error.code === 'string' ? body.error.code : 'FAULT_INJECTED'
    const message = typeof body.error.message === 'string' ? body.error.message : 'Simulator fault.'
    return new SimulatorError(status, code, message)
  }
  // Allocation and domain errors: {"detail": {"code", "message"}}
  if (isRecord(body) && isRecord(body.detail)) {
    const code = typeof body.detail.code === 'string' ? body.detail.code : `HTTP_${status}`
    const message = typeof body.detail.message === 'string' ? body.detail.message : 'Request failed.'
    return new SimulatorError(status, code, message)
  }
  // FastAPI validation: {"detail": [ { loc, msg, type }, ... ]}
  if (isRecord(body) && Array.isArray(body.detail)) {
    const first = body.detail[0]
    const msg = isRecord(first) && typeof first.msg === 'string'
      ? first.msg
      : 'Request failed validation.'
    return new SimulatorError(status, 'VALIDATION_ERROR', msg)
  }
  return new SimulatorError(status, `HTTP_${status}`, `Request failed with status ${status}.`)
}

export function createHttpClient(options: HttpOptions): SimulatorClient {
  const base = options.baseUrl.replace(/\/+$/, '')

  async function request<T>(path: string, init?: RequestInit): Promise<SimResponse<T>> {
    let response: Response
    try {
      response = await fetch(`${base}${path}`, {
        ...init,
        headers: {
          ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
          ...init?.headers,
        },
      })
    } catch (cause) {
      // A transport failure is a different condition from an HTTP error and
      // must not surface as an unhandled TypeError.
      throw new SimulatorError(0, 'NETWORK_ERROR',
        cause instanceof Error ? cause.message : 'Network request failed.')
    }

    const stale = response.headers.get('X-Simulator-Stale') === 'true'
    const text = await response.text()

    let body: unknown = null
    if (text) {
      try {
        body = JSON.parse(text)
      } catch {
        // A non-JSON body (a proxy error page, say) must not throw a parse
        // error that masks the real status.
        if (!response.ok) throw new SimulatorError(response.status, `HTTP_${response.status}`,
          `Request failed with status ${response.status}.`)
        throw new SimulatorError(response.status, 'INVALID_RESPONSE', 'Response was not valid JSON.')
      }
    }

    if (!response.ok) throw toSimulatorError(response.status, body)
    return { data: body as T, stale, receivedAt: Date.now() }
  }

  const get = <T,>(path: string) => request<T>(path)
  const post = <T,>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', ...(body === undefined ? {} : { body: JSON.stringify(body) }) })

  return {
    getHealth: () => get('/v1/health'),
    getInstance: () => get('/v1/instance'),
    getRegions: () => get('/v1/regions'),
    getDepots: () => get('/v1/depots'),
    getStations: () => get('/v1/stations'),
    getRoutes: () => get('/v1/routes'),
    getSupplyArrivals: () => get('/v1/supply-arrivals'),
    getEvents: () => get('/v1/events'),
    getAllocations: () => get('/v1/allocations'),
    getDemandHistory: (params) => {
      const query = new URLSearchParams()
      if (params.station_id) query.set('station_id', params.station_id)
      if (params.limit !== undefined) query.set('limit', String(params.limit))
      const suffix = query.toString()
      return get(`/v1/demand-history${suffix ? `?${suffix}` : ''}`)
    },
    getMetrics: () => get('/v1/metrics'),

    postAllocation: (body) => post('/v1/allocations', body),
    cancelAllocation: (id) => post(`/v1/allocations/${id}/cancel`),

    stream(): StreamHandle {
      const source = new EventSource(`${base}/v1/stream`)
      const listeners = new Map<StreamEventName, Set<(e: { name: StreamEventName; data: unknown; receivedAt: number }) => void>>()

      const names: StreamEventName[] = [
        'simulation.tick', 'allocation.status_changed', 'inventory.updated', 'simulator.notice',
      ]
      for (const name of names) {
        source.addEventListener(name, (event) => {
          const message = event as MessageEvent<string>
          let data: unknown = null
          try { data = JSON.parse(message.data) } catch { data = message.data }
          const payload = { name, data, receivedAt: Date.now() }
          listeners.get(name)?.forEach((listener) => listener(payload))
        })
      }

      return {
        on(name, listener) {
          const set = listeners.get(name) ?? new Set()
          set.add(listener)
          listeners.set(name, set)
          return () => set.delete(listener)
        },
        close() {
          source.close()
          listeners.clear()
        },
      }
    },

    adminRun: () => post('/admin/run'),
    adminPause: () => post('/admin/pause'),
    adminStep: () => post('/admin/step'),
    adminReset: () => post('/admin/reset'),
    adminInjectEvent: (body) => post('/admin/events', body),
    adminInjectFault: (body) => post('/admin/faults', body),
    adminClearFaults: () => post('/admin/faults/clear'),
    adminGetFaults: () => get('/admin/faults'),
    adminGetEvents: () => get('/admin/events'),
    getAudit: (limit) => get(`/admin/audit${limit === undefined ? '' : `?limit=${limit}`}`),
  }
}
