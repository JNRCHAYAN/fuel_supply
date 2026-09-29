import { describe, it, expect } from 'vitest'
import { createRng } from './seed'
import { DEPOTS, STATIONS, ROUTES, REGIONS, SUPPLY_SCHEDULE, DEMAND_PROFILES } from './fixtures'

describe('createRng', () => {
  it('produces the same sequence for the same seed', () => {
    const a = createRng(12345)
    const b = createRng(12345)
    const seqA = Array.from({ length: 5 }, () => a())
    const seqB = Array.from({ length: 5 }, () => b())
    expect(seqA).toEqual(seqB)
  })

  it('produces a different sequence for a different seed', () => {
    const a = createRng(1)
    const b = createRng(2)
    expect(a()).not.toBe(b())
  })

  it('stays within [0, 1)', () => {
    const rng = createRng(99)
    for (let i = 0; i < 1000; i++) {
      const v = rng()
      expect(v).toBeGreaterThanOrEqual(0)
      expect(v).toBeLessThan(1)
    }
  })
})

describe('world fixtures match the guide exactly', () => {
  it('defines two regions with the documented demand factors', () => {
    expect(REGIONS).toHaveLength(2)
    expect(REGIONS.map((r) => r.id).sort()).toEqual(['region-chattogram', 'region-dhaka'])
    expect(REGIONS.find((r) => r.id === 'region-chattogram')?.demand_factor).toBe(1.08)
  })

  it('defines two depots with the documented dispatch capacity', () => {
    const gazipur = DEPOTS.find((d) => d.id === 'depot-gazipur')
    const patiya = DEPOTS.find((d) => d.id === 'depot-patiya')
    expect(gazipur?.dispatch_capacity_per_tick).toBe(12000)
    expect(gazipur?.inventory.DIESEL).toBe(60000)
    expect(gazipur?.capacity.OCTANE).toBe(45000)
    expect(patiya?.dispatch_capacity_per_tick).toBe(11000)
  })

  it('defines four stations with the documented profiles', () => {
    expect(STATIONS).toHaveLength(4)
    expect(STATIONS.find((s) => s.id === 'station-mirpur')?.demand_profile).toBe('urban_high')
    expect(STATIONS.find((s) => s.id === 'station-tongi')?.demand_profile).toBe('industrial')
    expect(STATIONS.find((s) => s.id === 'station-karnaphuli')?.demand_profile).toBe('highway')
    expect(STATIONS.find((s) => s.id === 'station-coxsbazar')?.demand_profile).toBe('regional')
  })

  it('defines six routes with the documented transit times and limits', () => {
    expect(ROUTES).toHaveLength(6)
    const r = ROUTES.find((x) => x.id === 'route-patiya-coxsbazar')
    expect(r?.transit_ticks).toBe(3)
    expect(r?.max_shipment).toBe(6000)
  })

  it('defines the four demand profiles', () => {
    expect(DEMAND_PROFILES.urban_high.DIESEL).toBe(8500)
    expect(DEMAND_PROFILES.industrial.DIESEL).toBe(14000)
  })

  it('defines the 22-arrival supply schedule', () => {
    expect(SUPPLY_SCHEDULE).toHaveLength(22)
    const burst = SUPPLY_SCHEDULE.filter((a) => a.planned_tick >= 12 && a.planned_tick <= 20)
    expect(burst).toHaveLength(4)
  })
})
