import { describe, it, expect } from 'vitest'
import {
  isFuelType, isAllocationStatus, isDepotStatus, isStationStatus,
  isRouteStatus, isSupplyStatus, isEventStatus, isInstanceStatus,
  SIMULATOR_ERROR_CODES, FUEL_TYPES,
} from './simulator'

describe('simulator contract guards', () => {
  it('accepts every declared fuel type', () => {
    for (const fuel of FUEL_TYPES) expect(isFuelType(fuel)).toBe(true)
  })

  it('rejects an unknown fuel type', () => {
    expect(isFuelType('KEROSENE')).toBe(false)
    expect(isFuelType(undefined)).toBe(false)
    expect(isFuelType(3)).toBe(false)
  })

  it('guards each status union', () => {
    expect(isDepotStatus('CONSTRAINED')).toBe(true)
    expect(isDepotStatus('OUTAGE')).toBe(false)   // station status, not depot
    expect(isStationStatus('OUTAGE')).toBe(true)
    expect(isRouteStatus('DISRUPTED')).toBe(true)
    expect(isSupplyStatus('DELAYED')).toBe(true)
    expect(isEventStatus('ACTIVE')).toBe(true)
    expect(isInstanceStatus('RUNNING')).toBe(true)
    expect(isAllocationStatus('IN_TRANSIT')).toBe(true)
    expect(isAllocationStatus('SHIPPED')).toBe(false)
  })

  it('declares all ten allocation error codes from the guide', () => {
    expect(SIMULATOR_ERROR_CODES).toHaveLength(10)
    expect(SIMULATOR_ERROR_CODES).toContain('DISPATCH_CAPACITY_EXCEEDED')
    expect(SIMULATOR_ERROR_CODES).toContain('IDEMPOTENCY_KEY_MISMATCH')
  })
})
