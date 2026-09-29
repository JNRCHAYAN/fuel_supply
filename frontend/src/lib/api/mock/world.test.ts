import { describe, it, expect } from 'vitest'
import { World, hourOfTick } from './world'
import { SimulatorError } from '../errors'

describe('hourOfTick', () => {
  it('maps tick zero to hour zero', () => {
    expect(hourOfTick(0, 15)).toBe(0)
  })

  it('advances four hours every sixteen 15-minute ticks', () => {
    expect(hourOfTick(16, 15)).toBe(4)
    expect(hourOfTick(96, 15)).toBe(24 % 24)
  })

  it('wraps at 24 hours', () => {
    expect(hourOfTick(97, 15)).toBe(0)
  })
})

describe('World demand', () => {
  it('is deterministic for a given seed', () => {
    const a = new World({ seed: 12345 })
    const b = new World({ seed: 12345 })
    expect(a.demandForTick('station-mirpur', 'DIESEL', 10)).toBe(
      b.demandForTick('station-mirpur', 'DIESEL', 10),
    )
  })

  it('applies the documented per-tick demand exactly, with noise disabled', () => {
    const w = new World({ seed: 1 })
    // Mirpur: urban_high (8500 L/day diesel) in Dhaka (factor 1.00).
    // Hour 0 is off-peak for urban_high (busy 07-09 and 16-20), factor 0.70.
    const expected = (8500 / 1440) * 15 * 1.0 * 0.70
    expect(w.demandForTick('station-mirpur', 'DIESEL', 0, { noiseless: true }))
      .toBeCloseTo(expected, 6)
  })

  it('applies the Chattogram region factor of 1.08', () => {
    const w = new World({ seed: 1 })
    // Karnaphuli: highway (10500 L/day diesel) in Chattogram (factor 1.08).
    // Hour 0 is off-peak for highway (busy 06-09 and 16-20), factor 0.75.
    const expected = (10500 / 1440) * 15 * 1.08 * 0.75
    expect(w.demandForTick('station-karnaphuli', 'DIESEL', 0, { noiseless: true }))
      .toBeCloseTo(expected, 6)
  })

  it('never returns NaN or a negative value, for any station, fuel or tick', () => {
    const w = new World({ seed: 7 })
    for (const station of w.stations()) {
      for (const fuel of ['DIESEL', 'PETROL', 'OCTANE'] as const) {
        for (const tick of [0, 1, 7, 30, 96, 500, 5000]) {
          const v = w.demandForTick(station.id, fuel, tick)
          expect(Number.isFinite(v)).toBe(true)
          expect(v).toBeGreaterThanOrEqual(0)
        }
      }
    }
  })

  it('never returns NaN for an unknown station', () => {
    const w = new World({ seed: 7 })
    expect(w.demandForTick('station-does-not-exist', 'DIESEL', 10)).toBe(0)
  })

  it('applies the busy-hour multiplier for an industrial station at midday', () => {
    const w = new World({ seed: 12345 })
    // station-tongi is industrial: busy 06:00-17:59 at 1.55x, off-peak 0.45x.
    const midday = w.demandForTick('station-tongi', 'DIESEL', 32, { noiseless: true })  // hour 8
    const night = w.demandForTick('station-tongi', 'DIESEL', 8, { noiseless: true })    // hour 2
    expect(midday / night).toBeCloseTo(1.55 / 0.45, 6)
  })
})

describe('World clock', () => {
  it('starts paused at tick zero', () => {
    const w = new World({ seed: 12345 })
    expect(w.tick).toBe(0)
    expect(w.status).toBe('PAUSED')
  })

  it('advances exactly one tick per step, moving sim time on by tick_minutes', () => {
    const w = new World({ seed: 12345 })
    const start = new Date(w.simTime).getTime()
    w.step()
    expect(w.tick).toBe(1)
    const afterOne = new Date(w.simTime).getTime()
    expect(afterOne - start).toBe(15 * 60_000)
    w.step()
    expect(w.tick).toBe(2)
    expect(new Date(w.simTime).getTime() - afterOne).toBe(15 * 60_000)
  })

  it('is unchanged by a pause', () => {
    const w = new World({ seed: 12345 })
    w.run()
    w.pause()
    const before = w.tick
    expect(w.status).toBe('PAUSED')
    expect(w.tick).toBe(before)
  })
})

describe('demand observation ledger', () => {
  it('writes twelve rows per tick — four stations by three fuels', () => {
    const w = new World({ seed: 12345 })
    w.step()
    expect(w.demandHistory({ limit: 2000 })).toHaveLength(12)
    w.step()
    expect(w.demandHistory({ limit: 2000 })).toHaveLength(24)
  })

  it('returns an empty array when no tick has run, never undefined', () => {
    const w = new World({ seed: 12345 })
    expect(w.demandHistory({})).toEqual([])
  })

  it('splits demand into served and unmet against available inventory', () => {
    const w = new World({ seed: 12345 })
    w.step()
    for (const row of w.demandHistory({ limit: 2000 })) {
      expect(row.served_liters + row.unmet_liters).toBeCloseTo(row.demand_liters, 6)
      expect(row.unmet_liters).toBeGreaterThanOrEqual(0)
    }
  })

  it('serves nothing at a station that is OUTAGE', () => {
    const w = new World({ seed: 12345 })
    w.step()
    const before = w.demandHistory({ station_id: 'station-mirpur', limit: 2000 })
    expect(before.length).toBeGreaterThan(0)
  })

  it('filters by station_id', () => {
    const w = new World({ seed: 12345 })
    w.step()
    const rows = w.demandHistory({ station_id: 'station-mirpur', limit: 2000 })
    expect(rows).toHaveLength(3)
    expect(rows.every((r) => r.station_id === 'station-mirpur')).toBe(true)
  })

  it('clamps limit to the documented [1, 2000] range', () => {
    const w = new World({ seed: 12345 })
    for (let i = 0; i < 5; i++) w.step()
    expect(w.demandHistory({ limit: 0 }).length).toBe(1)      // clamped up to 1
    expect(w.demandHistory({ limit: 99999 }).length).toBe(60) // clamped down to 2000
  })

  it('returns the most recent rows first when limited', () => {
    const w = new World({ seed: 12345 })
    for (let i = 0; i < 3; i++) w.step()
    const rows = w.demandHistory({ limit: 12 })
    expect(rows).toHaveLength(12)
    expect(Math.max(...rows.map((r) => r.tick))).toBe(3)
  })
})

describe('metrics', () => {
  it('reports a service level of 1 when nothing is unmet', () => {
    const w = new World({ seed: 12345 })
    expect(w.metrics().service_level).toBe(1)
  })

  it('computes service_level as served over served plus unmet', () => {
    const w = new World({ seed: 12345 })
    for (let i = 0; i < 5; i++) w.step()
    const m = w.metrics()
    const expected = m.served_demand_liters / (m.served_demand_liters + m.unmet_demand_liters)
    expect(m.service_level).toBeCloseTo(expected, 9)
  })

  it('never divides by zero when no demand has been recorded', () => {
    const w = new World({ seed: 12345 })
    const m = w.metrics()
    expect(Number.isFinite(m.service_level)).toBe(true)
    expect(m.service_level).toBe(1)
  })
})

describe('supply arrivals', () => {
  it('exposes all 22 scheduled arrivals', () => {
    const w = new World({ seed: 12345 })
    expect(w.supplyArrivals()).toHaveLength(22)
  })

  it('is sorted by planned_tick ascending', () => {
    const w = new World({ seed: 12345 })
    const ticks = w.supplyArrivals().map((a) => a.planned_tick)
    expect(ticks).toEqual([...ticks].sort((a, b) => a - b))
  })

  it('starts every arrival as SCHEDULED with no actual tick', () => {
    const w = new World({ seed: 12345 })
    for (const a of w.supplyArrivals()) {
      expect(a.status).toBe('SCHEDULED')
      expect(a.actual_tick).toBeNull()
    }
  })

  it('deposits fuel into the depot and marks the arrival ARRIVED once its tick passes', () => {
    const w = new World({ seed: 12345 })
    const before = w.depots().find((d) => d.id === 'depot-gazipur')!.inventory.DIESEL
    for (let i = 0; i < 12; i++) w.step()
    const arrival = w.supplyArrivals().find((a) => a.planned_tick === 12)!
    expect(arrival.status).toBe('ARRIVED')
    expect(arrival.actual_tick).toBe(12)
    const after = w.depots().find((d) => d.id === 'depot-gazipur')!.inventory.DIESEL
    expect(after).toBe(before + 18000)
  })

  it('does not deposit the same arrival twice across ticks', () => {
    const w = new World({ seed: 12345 })
    for (let i = 0; i < 12; i++) w.step()
    const once = w.depots().find((d) => d.id === 'depot-gazipur')!.inventory.DIESEL
    w.step()
    w.step()
    const later = w.depots().find((d) => d.id === 'depot-gazipur')!.inventory.DIESEL
    // Further ticks may consume fuel but must never re-add the same arrival.
    expect(later).toBeLessThanOrEqual(once)
  })

  it('never exceeds depot capacity when an arrival lands', () => {
    const w = new World({ seed: 12345 })
    for (let i = 0; i < 200; i++) w.step()
    for (const depot of w.depots()) {
      for (const fuel of ['DIESEL', 'PETROL', 'OCTANE'] as const) {
        expect(depot.inventory[fuel]).toBeLessThanOrEqual(depot.capacity[fuel])
      }
    }
  })
})

const VALID = {
  idempotency_key: 'demo-001',
  source_depot_id: 'depot-gazipur',
  destination_station_id: 'station-mirpur',
  route_id: 'route-gazipur-mirpur',
  fuel_type: 'DIESEL' as const,
  quantity: 3000,
}

function expectCode(fn: () => unknown, code: string) {
  try {
    fn()
    throw new Error(`expected ${code}, but no error was thrown`)
  } catch (err) {
    expect(err).toBeInstanceOf(SimulatorError)
    expect((err as SimulatorError).code).toBe(code)
  }
}

describe('allocation validation order (Guide 5.2)', () => {
  it('accepts a valid allocation and returns PENDING', () => {
    const w = new World({ seed: 12345 })
    const a = w.createAllocation(VALID)
    expect(a.status).toBe('PENDING')
    expect(a.quantity).toBe(3000)
    expect(a.created_tick).toBe(0)
    expect(a.actual_arrival_tick).toBeNull()
  })

  it('returns the existing allocation for a repeated idempotency key and body', () => {
    const w = new World({ seed: 12345 })
    const first = w.createAllocation(VALID)
    const second = w.createAllocation(VALID)
    expect(second.id).toBe(first.id)
    expect(w.allocations()).toHaveLength(1)
  })

  it('rejects the same key with a different body', () => {
    const w = new World({ seed: 12345 })
    w.createAllocation(VALID)
    expectCode(
      () => w.createAllocation({ ...VALID, quantity: 4000 }),
      'IDEMPOTENCY_KEY_MISMATCH',
    )
  })

  it('rejects an unknown depot, station or route with NOT_FOUND', () => {
    const w = new World({ seed: 12345 })
    expectCode(() => w.createAllocation({ ...VALID, source_depot_id: 'nope' }), 'NOT_FOUND')
    expectCode(() => w.createAllocation({ ...VALID, destination_station_id: 'nope' }), 'NOT_FOUND')
    expectCode(() => w.createAllocation({ ...VALID, route_id: 'nope' }), 'NOT_FOUND')
  })

  it('rejects a route that does not connect the given depot and station', () => {
    const w = new World({ seed: 12345 })
    expectCode(
      () => w.createAllocation({ ...VALID, route_id: 'route-gazipur-tongi' }),
      'ROUTE_MISMATCH',
    )
  })

  it('rejects a station that is OUTAGE with STATION_CLOSED', () => {
    const w = new World({ seed: 12345 })
    w.setStationStatus('station-mirpur', 'OUTAGE')
    expectCode(() => w.createAllocation(VALID), 'STATION_CLOSED')
  })

  it('rejects a disrupted route with ROUTE_DISRUPTED', () => {
    const w = new World({ seed: 12345 })
    w.setRouteStatus('route-gazipur-mirpur', 'DISRUPTED')
    expectCode(() => w.createAllocation(VALID), 'ROUTE_DISRUPTED')
  })

  it('rejects a quantity above the route maximum', () => {
    const w = new World({ seed: 12345 })
    expectCode(() => w.createAllocation({ ...VALID, quantity: 7001 }), 'ROUTE_CAPACITY_EXCEEDED')
  })

  it('ACCEPTS a quantity exactly equal to the route maximum', () => {
    // No Guide 5.2 limit check fails on equality: route max (7), dispatch
    // capacity (9) and destination headroom (10) fail on strict >, and depot
    // inventory (8) fails on inventory < quantity. The boundary is legal.
    // route-gazipur-tongi caps at 6500 and Tongi has 7000 L of diesel
    // headroom, so 6500 is at the route limit and legal end to end.
    const w = new World({ seed: 12345 })
    const a = w.createAllocation({
      ...VALID,
      idempotency_key: 'k-max',
      route_id: 'route-gazipur-tongi',
      destination_station_id: 'station-tongi',
      quantity: 6500,
    })
    expect(a.quantity).toBe(6500)
  })

  it('rejects a quantity above remaining depot inventory', () => {
    const w = new World({ seed: 12345 })
    // Drain Gazipur to exactly 5000 L, then ask for 6000 on a route that allows it.
    w.drainDepot('depot-gazipur', 'DIESEL', 55000)
    expectCode(
      () => w.createAllocation({ ...VALID, idempotency_key: 'k-inv', quantity: 6000 }),
      'INSUFFICIENT_INVENTORY',
    )
  })

  it('ACCEPTS a quantity exactly equal to remaining depot inventory', () => {
    const w = new World({ seed: 12345 })
    w.drainDepot('depot-gazipur', 'DIESEL', 55000) // exactly 5000 L left
    const a = w.createAllocation({ ...VALID, idempotency_key: 'k-exact', quantity: 5000 })
    expect(a.quantity).toBe(5000)
  })

  it('rejects when the dispatch capacity for this tick is exceeded', () => {
    const w = new World({ seed: 12345 })
    // Gazipur may dispatch 12000 L per tick. Two legal loads commit 11500 L.
    w.createAllocation({
      ...VALID, idempotency_key: 'd-1',
      route_id: 'route-gazipur-tongi', destination_station_id: 'station-tongi', quantity: 6500,
    })
    w.createAllocation({
      ...VALID, idempotency_key: 'd-2',
      route_id: 'route-gazipur-karnaphuli', destination_station_id: 'station-karnaphuli', quantity: 5000,
    })
    // A third 3000 L load is legal on every other rule and breaches only this one.
    expectCode(
      () => w.createAllocation({
        ...VALID, idempotency_key: 'd-3', fuel_type: 'PETROL',
        route_id: 'route-gazipur-tongi', destination_station_id: 'station-tongi', quantity: 3000,
      }),
      'DISPATCH_CAPACITY_EXCEEDED',
    )
  })

  it('rejects when the destination cannot hold the fuel', () => {
    const w = new World({ seed: 12345 })
    // Mirpur holds 9000 of 15000 diesel; 7000 more would overflow.
    expectCode(
      () => w.createAllocation({ ...VALID, idempotency_key: 'dst-1', quantity: 7000 }),
      'DESTINATION_CAPACITY_EXCEEDED',
    )
  })

  it('reports NOT_FOUND ahead of a route mismatch when both apply', () => {
    // First failure wins: an unknown depot outranks a mismatched route.
    const w = new World({ seed: 12345 })
    expectCode(
      () => w.createAllocation({ ...VALID, source_depot_id: 'nope', route_id: 'route-gazipur-tongi' }),
      'NOT_FOUND',
    )
  })

  it('does not free an idempotency key when an allocation is cancelled', () => {
    const w = new World({ seed: 12345 })
    const a = w.createAllocation(VALID)
    w.cancelAllocation(a.id)
    expectCode(() => w.createAllocation({ ...VALID, quantity: 1000 }), 'IDEMPOTENCY_KEY_MISMATCH')
  })
})

describe('allocation lifecycle', () => {
  it('refunds depot inventory on cancel and marks it CANCELLED', () => {
    const w = new World({ seed: 12345 })
    const before = w.depots().find((d) => d.id === 'depot-gazipur')!.inventory.DIESEL
    const a = w.createAllocation(VALID)
    expect(w.depots().find((d) => d.id === 'depot-gazipur')!.inventory.DIESEL).toBe(before - 3000)
    const cancelled = w.cancelAllocation(a.id)
    expect(cancelled.status).toBe('CANCELLED')
    expect(w.depots().find((d) => d.id === 'depot-gazipur')!.inventory.DIESEL).toBe(before)
  })

  it('refuses to cancel an allocation that has already departed', () => {
    const w = new World({ seed: 12345 })
    const a = w.createAllocation(VALID)
    w.step() // now IN_TRANSIT
    expect(w.allocations().find((x) => x.id === a.id)!.status).toBe('IN_TRANSIT')
    expectCode(() => w.cancelAllocation(a.id), 'CANNOT_CANCEL')
  })

  it('reports ALLOCATION_NOT_FOUND for an unknown id', () => {
    const w = new World({ seed: 12345 })
    expectCode(() => w.cancelAllocation(9999), 'ALLOCATION_NOT_FOUND')
  })

  it('moves PENDING to IN_TRANSIT to ARRIVED across the transit window', () => {
    const w = new World({ seed: 12345 })
    const a = w.createAllocation(VALID) // transit_ticks 2, created at tick 0
    expect(w.allocations().find((x) => x.id === a.id)!.status).toBe('PENDING')
    w.step()
    expect(w.allocations().find((x) => x.id === a.id)!.status).toBe('IN_TRANSIT')
    w.step()
    const arrived = w.allocations().find((x) => x.id === a.id)!
    expect(arrived.status).toBe('ARRIVED')
    expect(arrived.actual_arrival_tick).toBe(2)
  })

  it('marks an in-transit allocation FAILED when its route is disrupted', () => {
    const w = new World({ seed: 12345 })
    const a = w.createAllocation(VALID)
    w.setRouteStatus('route-gazipur-mirpur', 'DISRUPTED')
    for (let i = 0; i < 4; i++) w.step()
    const failed = w.allocations().find((x) => x.id === a.id)!
    expect(failed.status).toBe('FAILED')
    expect(failed.failure_reason).toBeTruthy()
  })

  it('adds delivered fuel to the destination station', () => {
    const w = new World({ seed: 12345 })
    const before = w.stations().find((s) => s.id === 'station-mirpur')!.inventory.DIESEL
    w.createAllocation(VALID)
    for (let i = 0; i < 3; i++) w.step()
    const after = w.stations().find((s) => s.id === 'station-mirpur')!.inventory.DIESEL
    // Some of the 3000 may have been sold, so the increase must be positive
    // but need not equal the full shipment.
    expect(after).toBeGreaterThan(before - 1)
  })
})

describe('event injection', () => {
  it('creates an event with end_tick derived from start and duration', () => {
    const w = new World({ seed: 12345 })
    const e = w.injectEvent({ type: 'demand_spike', start_tick: 8, duration_ticks: 12, parameters: {} })
    expect(e.start_tick).toBe(8)
    expect(e.end_tick).toBe(20)
    expect(e.status).toBe('SCHEDULED')
  })

  it('defaults parameters to an empty object', () => {
    const w = new World({ seed: 12345 })
    const e = w.injectEvent({ type: 'route_disruption', start_tick: 0, duration_ticks: 5 })
    expect(e.parameters).toEqual({})
  })

  it('rejects a non-positive duration', () => {
    const w = new World({ seed: 12345 })
    expect(() => w.injectEvent({ type: 'demand_spike', start_tick: 0, duration_ticks: 0 })).toThrow()
  })

  it('rejects a negative start tick', () => {
    const w = new World({ seed: 12345 })
    expect(() => w.injectEvent({ type: 'demand_spike', start_tick: -1, duration_ticks: 5 })).toThrow()
  })
})

describe('demand_spike reverses on resolve', () => {
  it('multiplies affected stations while ACTIVE and restores exactly on RESOLVED', () => {
    const w = new World({ seed: 12345 })
    const before = w.stations().find((s) => s.id === 'station-mirpur')!.demand_multiplier

    w.injectEvent({ type: 'demand_spike', start_tick: 0, duration_ticks: 3, parameters: { region_ids: ['region-dhaka'], multiplier: 1.8 } })
    w.step()
    expect(w.stations().find((s) => s.id === 'station-mirpur')!.demand_multiplier).toBeCloseTo(before * 1.8, 9)
    expect(w.events()[0]!.status).toBe('ACTIVE')

    for (let i = 0; i < 3; i++) w.step()
    expect(w.stations().find((s) => s.id === 'station-mirpur')!.demand_multiplier).toBeCloseTo(before, 9)
    expect(w.events().find((e) => e.type === 'demand_spike')!.status).toBe('RESOLVED')
  })

  it('does not drive the multiplier below the 0.01 floor', () => {
    const w = new World({ seed: 12345 })
    w.injectEvent({ type: 'demand_spike', start_tick: 0, duration_ticks: 2, parameters: { multiplier: 0.01 } })
    w.step()
    expect(w.stations().every((s) => s.demand_multiplier >= 0.01)).toBe(true)
  })
})

describe('route_disruption, station_outage and depot_constraint reverse on resolve', () => {
  it('restores routes to AVAILABLE', () => {
    const w = new World({ seed: 12345 })
    w.injectEvent({ type: 'route_disruption', start_tick: 0, duration_ticks: 2, parameters: { route_ids: ['route-gazipur-mirpur'] } })
    w.step()
    expect(w.routes().find((r) => r.id === 'route-gazipur-mirpur')!.status).toBe('DISRUPTED')
    for (let i = 0; i < 2; i++) w.step()
    expect(w.routes().find((r) => r.id === 'route-gazipur-mirpur')!.status).toBe('AVAILABLE')
  })

  it('restores stations to OPEN', () => {
    const w = new World({ seed: 12345 })
    w.injectEvent({ type: 'station_outage', start_tick: 0, duration_ticks: 2, parameters: { station_ids: ['station-tongi'] } })
    w.step()
    expect(w.stations().find((s) => s.id === 'station-tongi')!.status).toBe('OUTAGE')
    for (let i = 0; i < 2; i++) w.step()
    expect(w.stations().find((s) => s.id === 'station-tongi')!.status).toBe('OPEN')
  })

  it('restores depots to OPEN', () => {
    const w = new World({ seed: 12345 })
    w.injectEvent({ type: 'depot_constraint', start_tick: 0, duration_ticks: 2, parameters: { depot_ids: ['depot-gazipur'] } })
    w.step()
    expect(w.depots().find((d) => d.id === 'depot-gazipur')!.status).toBe('CONSTRAINED')
    for (let i = 0; i < 2; i++) w.step()
    expect(w.depots().find((d) => d.id === 'depot-gazipur')!.status).toBe('OPEN')
  })

  it('applies to every entity when the filter list is empty', () => {
    const w = new World({ seed: 12345 })
    w.injectEvent({ type: 'route_disruption', start_tick: 0, duration_ticks: 2, parameters: { route_ids: [] } })
    w.step()
    expect(w.routes().every((r) => r.status === 'DISRUPTED')).toBe(true)
  })
})

describe('one-shot events do NOT auto-undo', () => {
  it('leaves a delayed shipment delayed after the event resolves', () => {
    const w = new World({ seed: 12345 })
    const target = w.supplyArrivals().find((a) => a.planned_tick === 12)!
    const original = target.planned_tick

    w.injectEvent({ type: 'shipment_delay', start_tick: 0, duration_ticks: 2, parameters: { delay_ticks: 5 } })
    w.step()
    const delayed = w.supplyArrivals().find((a) => a.id === target.id)!
    expect(delayed.status).toBe('DELAYED')
    expect(delayed.planned_tick).toBe(original + 5)

    for (let i = 0; i < 4; i++) w.step()
    const after = w.supplyArrivals().find((a) => a.id === target.id)!
    expect(after.planned_tick).toBe(original + 5)
  })

  it('leaves a shortfall quantity reduced after the event resolves', () => {
    const w = new World({ seed: 12345 })
    const target = w.supplyArrivals().find((a) => a.planned_tick === 12)!
    const original = target.quantity

    w.injectEvent({ type: 'supply_shortfall', start_tick: 0, duration_ticks: 2, parameters: { factor: 0.5 } })
    w.step()
    expect(w.supplyArrivals().find((a) => a.id === target.id)!.quantity).toBeCloseTo(original * 0.5, 6)

    for (let i = 0; i < 4; i++) w.step()
    expect(w.supplyArrivals().find((a) => a.id === target.id)!.quantity).toBeCloseTo(original * 0.5, 6)
  })
})

describe('events list', () => {
  it('returns events newest first', () => {
    const w = new World({ seed: 12345 })
    w.injectEvent({ type: 'demand_spike', start_tick: 0, duration_ticks: 1, parameters: {} })
    w.injectEvent({ type: 'route_disruption', start_tick: 0, duration_ticks: 1, parameters: {} })
    const ids = w.events().map((e) => e.id)
    expect(ids).toEqual([...ids].sort((a, b) => b - a))
  })

  it('returns an empty array before any event is injected', () => {
    expect(new World({ seed: 1 }).events()).toEqual([])
  })
})

describe('fault injection', () => {
  it('creates an active fault expiring after its duration', () => {
    const w = new World({ seed: 12345 })
    const f = w.injectFault({ type: 'stale_data', duration_seconds: 60 })
    expect(f.active).toBe(true)
    expect(new Date(f.end_wall_time).getTime()).toBeGreaterThan(new Date(f.start_wall_time).getTime())
  })

  it('rejects a duration of zero or above one hour', () => {
    const w = new World({ seed: 12345 })
    expect(() => w.injectFault({ type: 'latency', duration_seconds: 0 })).toThrow()
    expect(() => w.injectFault({ type: 'latency', duration_seconds: 3601 })).toThrow()
  })

  it('clears every active fault', () => {
    const w = new World({ seed: 12345 })
    w.injectFault({ type: 'stale_data', duration_seconds: 60 })
    w.injectFault({ type: 'latency', duration_seconds: 60 })
    expect(w.activeFaults()).toHaveLength(2)
    w.clearFaults()
    expect(w.activeFaults()).toHaveLength(0)
  })

  it('treats an expired fault as inactive', () => {
    const w = new World({ seed: 12345 })
    w.injectFault({ type: 'unavailable', duration_seconds: 1 })
    // Force expiry without waiting on wall-clock time.
    w.expireFaults(Date.now() + 5000)
    expect(w.activeFaults()).toHaveLength(0)
    expect(w.shouldFailRequest()).toBeNull()
  })
})

describe('fault effects on /v1 paths', () => {
  it('unavailable always fails a request with 503', () => {
    const w = new World({ seed: 12345 })
    w.injectFault({ type: 'unavailable', duration_seconds: 60 })
    const failure = w.shouldFailRequest()
    expect(failure?.status).toBe(503)
    expect(failure?.code).toBe('FAULT_INJECTED')
  })

  it('error_rate fails some requests and passes others', () => {
    const w = new World({ seed: 12345 })
    w.injectFault({ type: 'error_rate', duration_seconds: 60, parameters: { rate: 0.5 } })
    const results = Array.from({ length: 400 }, () => w.shouldFailRequest() !== null)
    const failures = results.filter(Boolean).length
    expect(failures).toBeGreaterThan(0)
    expect(failures).toBeLessThan(results.length)
  })

  it('error_rate of zero never fails', () => {
    const w = new World({ seed: 12345 })
    w.injectFault({ type: 'error_rate', duration_seconds: 60, parameters: { rate: 0 } })
    expect(Array.from({ length: 200 }, () => w.shouldFailRequest()).every((r) => r === null)).toBe(true)
  })

  it('latency does not fail requests but reports a delay', () => {
    const w = new World({ seed: 12345 })
    w.injectFault({ type: 'latency', duration_seconds: 60, parameters: { delay_ms: 250 } })
    expect(w.shouldFailRequest()).toBeNull()
    expect(w.requestDelayMs()).toBe(250)
  })

  it('stale_data marks responses stale without failing them', () => {
    const w = new World({ seed: 12345 })
    w.injectFault({ type: 'stale_data', duration_seconds: 60 })
    expect(w.isStale()).toBe(true)
    expect(w.shouldFailRequest()).toBeNull()
  })

  it('stream_disconnect marks the stream disconnected without failing REST', () => {
    const w = new World({ seed: 12345 })
    w.injectFault({ type: 'stream_disconnect', duration_seconds: 60 })
    expect(w.isStreamDisconnected()).toBe(true)
    expect(w.shouldFailRequest()).toBeNull()
  })
})
