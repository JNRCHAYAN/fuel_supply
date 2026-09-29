import { describe, it, expect, vi } from 'vitest'
import { createMockClient } from './mockClient'
import { SimulatorError } from '../errors'

describe('mock client responses', () => {
  it('wraps data with a non-stale flag and a receipt time by default', async () => {
    const client = createMockClient({ seed: 12345 })
    const res = await client.getDepots()
    expect(res.stale).toBe(false)
    expect(res.receivedAt).toBeGreaterThan(0)
    expect(res.data).toHaveLength(2)
  })

  it('marks responses stale while a stale_data fault is active', async () => {
    const client = createMockClient({ seed: 12345 })
    client.world.injectFault({ type: 'stale_data', duration_seconds: 60 })
    expect((await client.getDepots()).stale).toBe(true)
  })

  it('rejects with FAULT_INJECTED while an unavailable fault is active', async () => {
    const client = createMockClient({ seed: 12345 })
    client.world.injectFault({ type: 'unavailable', duration_seconds: 60 })
    await expect(client.getDepots()).rejects.toBeInstanceOf(SimulatorError)
    await expect(client.getDepots()).rejects.toMatchObject({ code: 'FAULT_INJECTED', status: 503 })
  })

  it('does not fail getHealth even while unavailable, since health bypasses faults', async () => {
    const client = createMockClient({ seed: 12345 })
    client.world.injectFault({ type: 'unavailable', duration_seconds: 60 })
    await expect(client.getHealth()).resolves.toMatchObject({ data: { status: 'ok' } })
  })

  it('exposes every documented read endpoint', async () => {
    const client = createMockClient({ seed: 12345 })
    await expect(client.getInstance()).resolves.toBeTruthy()
    await expect(client.getRegions()).resolves.toBeTruthy()
    await expect(client.getStations()).resolves.toBeTruthy()
    await expect(client.getRoutes()).resolves.toBeTruthy()
    await expect(client.getSupplyArrivals()).resolves.toBeTruthy()
    await expect(client.getEvents()).resolves.toBeTruthy()
    await expect(client.getAllocations()).resolves.toBeTruthy()
    await expect(client.getDemandHistory({})).resolves.toBeTruthy()
    await expect(client.getMetrics()).resolves.toBeTruthy()
    await expect(client.getAudit()).resolves.toBeTruthy()
  })

  it('creates an allocation through the client', async () => {
    const client = createMockClient({ seed: 12345 })
    const res = await client.postAllocation({
      idempotency_key: 'c-1',
      source_depot_id: 'depot-gazipur',
      destination_station_id: 'station-mirpur',
      route_id: 'route-gazipur-mirpur',
      fuel_type: 'DIESEL',
      quantity: 3000,
    })
    expect(res.data.status).toBe('PENDING')
  })

  it('surfaces a validation failure as a SimulatorError with its code', async () => {
    const client = createMockClient({ seed: 12345 })
    await expect(
      client.postAllocation({
        idempotency_key: 'c-2',
        source_depot_id: 'depot-gazipur',
        destination_station_id: 'station-mirpur',
        route_id: 'route-gazipur-mirpur',
        fuel_type: 'DIESEL',
        quantity: 999999,
      }),
    ).rejects.toMatchObject({ code: 'ROUTE_CAPACITY_EXCEEDED' })
  })
})

describe('mock client streaming', () => {
  it('emits a tick event when the world steps', () => {
    const client = createMockClient({ seed: 12345 })
    const onTick = vi.fn()
    const handle = client.stream()
    handle.on('simulation.tick', onTick)
    client.world.step()
    client.publishTick()
    expect(onTick).toHaveBeenCalled()
  })

  it('reports the stream as unavailable while stream_disconnect is active', () => {
    const client = createMockClient({ seed: 12345 })
    client.world.injectFault({ type: 'stream_disconnect', duration_seconds: 60 })
    expect(() => client.stream()).toThrow(SimulatorError)
  })

  it('stops delivering after close and is safe to close twice', () => {
    const client = createMockClient({ seed: 12345 })
    const onTick = vi.fn()
    const handle = client.stream()
    handle.on('simulation.tick', onTick)
    handle.close()
    handle.close()
    client.publishTick()
    expect(onTick).not.toHaveBeenCalled()
  })
})
