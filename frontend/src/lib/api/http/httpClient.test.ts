import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createHttpClient } from './httpClient'
import { SimulatorError } from '../errors'

function jsonResponse(body: unknown, init: { status?: number; headers?: Record<string, string> } = {}) {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
  })
}

describe('http client response handling', () => {
  beforeEach(() => { vi.stubGlobal('fetch', vi.fn()) })
  afterEach(() => { vi.unstubAllGlobals() })

  it('unwraps a successful body', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse([{ id: 'depot-gazipur' }]))
    const client = createHttpClient({ baseUrl: 'http://localhost:8000' })
    const res = await client.getDepots()
    expect(res.data).toEqual([{ id: 'depot-gazipur' }])
    expect(res.stale).toBe(false)
  })

  it('reads the X-Simulator-Stale header', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse([], { headers: { 'X-Simulator-Stale': 'true' } }),
    )
    const client = createHttpClient({ baseUrl: 'http://localhost:8000' })
    expect((await client.getDepots()).stale).toBe(true)
  })

  it('normalises the {"detail":{code,message}} envelope', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse({ detail: { code: 'ROUTE_DISRUPTED', message: 'Route is DISRUPTED.' } }, { status: 409 }),
    )
    const client = createHttpClient({ baseUrl: 'http://localhost:8000' })
    await expect(client.postAllocation({
      idempotency_key: 'x', source_depot_id: 'a', destination_station_id: 'b',
      route_id: 'c', fuel_type: 'DIESEL', quantity: 1,
    })).rejects.toMatchObject({ code: 'ROUTE_DISRUPTED', status: 409, message: 'Route is DISRUPTED.' })
  })

  it('normalises the {"error":{code,message}} envelope used by injected faults', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(
        { error: { code: 'FAULT_INJECTED', message: 'Simulator API temporarily unavailable.' } },
        { status: 503 },
      ),
    )
    const client = createHttpClient({ baseUrl: 'http://localhost:8000' })
    await expect(client.getDepots()).rejects.toMatchObject({
      code: 'FAULT_INJECTED', status: 503,
    })
  })

  it('normalises FastAPI validation errors, whose detail is an array', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(
        { detail: [{ loc: ['body', 'quantity'], msg: 'ensure this value is greater than 0', type: 'value_error' }] },
        { status: 422 },
      ),
    )
    const client = createHttpClient({ baseUrl: 'http://localhost:8000' })
    await expect(client.postAllocation({
      idempotency_key: 'x', source_depot_id: 'a', destination_station_id: 'b',
      route_id: 'c', fuel_type: 'DIESEL', quantity: 0,
    })).rejects.toMatchObject({ code: 'VALIDATION_ERROR', status: 422 })
  })

  it('survives a non-JSON error body without throwing a parse error', async () => {
    // A fresh Response per call: a Response body is single-use, and handing
    // the same instance to two requests fails on the second for reasons that
    // have nothing to do with the client.
    vi.mocked(fetch).mockImplementation(async () =>
      new Response('<html>502 Bad Gateway</html>', { status: 502 }),
    )
    const client = createHttpClient({ baseUrl: 'http://localhost:8000' })
    await expect(client.getDepots()).rejects.toBeInstanceOf(SimulatorError)
    await expect(client.getDepots()).rejects.toMatchObject({ status: 502 })
  })

  it('reports a network failure as a SimulatorError rather than a raw TypeError', async () => {
    vi.mocked(fetch).mockRejectedValue(new TypeError('Failed to fetch'))
    const client = createHttpClient({ baseUrl: 'http://localhost:8000' })
    await expect(client.getDepots()).rejects.toMatchObject({ code: 'NETWORK_ERROR' })
  })

  it('builds the documented URL for demand history', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse([]))
    const client = createHttpClient({ baseUrl: 'http://localhost:8000' })
    await client.getDemandHistory({ station_id: 'station-mirpur', limit: 50 })
    const url = vi.mocked(fetch).mock.calls[0]![0] as string
    expect(url).toContain('/v1/demand-history')
    expect(url).toContain('station_id=station-mirpur')
    expect(url).toContain('limit=50')
  })
})
