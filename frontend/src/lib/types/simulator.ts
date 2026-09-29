export const FUEL_TYPES = ['DIESEL', 'PETROL', 'OCTANE'] as const
export type FuelType = (typeof FUEL_TYPES)[number]

export const DEPOT_STATUSES = ['OPEN', 'CONSTRAINED'] as const
export type DepotStatus = (typeof DEPOT_STATUSES)[number]

export const STATION_STATUSES = ['OPEN', 'OUTAGE'] as const
export type StationStatus = (typeof STATION_STATUSES)[number]

export const ROUTE_STATUSES = ['AVAILABLE', 'DISRUPTED'] as const
export type RouteStatus = (typeof ROUTE_STATUSES)[number]

export const SUPPLY_STATUSES = ['SCHEDULED', 'DELAYED', 'ARRIVED'] as const
export type SupplyStatus = (typeof SUPPLY_STATUSES)[number]

export const EVENT_STATUSES = ['SCHEDULED', 'ACTIVE', 'RESOLVED'] as const
export type EventStatus = (typeof EVENT_STATUSES)[number]

export const ALLOCATION_STATUSES = [
  'PENDING', 'IN_TRANSIT', 'ARRIVED', 'FAILED', 'CANCELLED',
] as const
export type AllocationStatus = (typeof ALLOCATION_STATUSES)[number]

export const INSTANCE_STATUSES = ['PAUSED', 'RUNNING'] as const
export type InstanceStatus = (typeof INSTANCE_STATUSES)[number]

export type FuelMap = Record<FuelType, number>

export interface Region {
  id: string
  name: string
  demand_factor: number
}

export interface Depot {
  id: string
  name: string
  region_id: string
  status: DepotStatus
  dispatch_capacity_per_tick: number
  capacity: FuelMap
  inventory: FuelMap
}

export interface Station {
  id: string
  name: string
  region_id: string
  status: StationStatus
  demand_profile: DemandProfileName
  demand_multiplier: number
  capacity: FuelMap
  inventory: FuelMap
}

export interface Route {
  id: string
  source_depot_id: string
  destination_station_id: string
  transit_ticks: number
  max_shipment: number
  status: RouteStatus
}

export interface SupplyArrival {
  id: string
  depot_id: string
  fuel_type: FuelType
  quantity: number
  planned_tick: number
  actual_tick: number | null
  status: SupplyStatus
}

export interface DomainEvent {
  id: number
  type: EventType
  start_tick: number
  end_tick: number
  status: EventStatus
  parameters: Record<string, unknown>
}

export type EventType =
  | 'demand_spike'
  | 'route_disruption'
  | 'station_outage'
  | 'depot_constraint'
  | 'shipment_delay'
  | 'supply_shortfall'

export type FaultType =
  | 'latency'
  | 'unavailable'
  | 'error_rate'
  | 'stale_data'
  | 'stream_disconnect'

export interface Fault {
  id: number
  type: FaultType
  start_wall_time: string
  end_wall_time: string
  active: boolean
  parameters: Record<string, unknown>
}

export interface Allocation {
  id: number
  idempotency_key: string
  source_depot_id: string
  destination_station_id: string
  route_id: string
  fuel_type: FuelType
  quantity: number
  created_tick: number
  departure_tick: number | null
  expected_arrival_tick: number | null
  actual_arrival_tick: number | null
  status: AllocationStatus
  failure_reason: string | null
}

export interface AllocationRequest {
  idempotency_key: string
  source_depot_id: string
  destination_station_id: string
  route_id: string
  fuel_type: FuelType
  quantity: number
}

export interface DemandObservation {
  id: number
  station_id: string
  fuel_type: FuelType
  tick: number
  sim_time: string
  demand_liters: number
  served_liters: number
  unmet_liters: number
}

export interface Metrics {
  served_demand_liters: number
  unmet_demand_liters: number
  service_level: number
  allocation_liters: number
  allocation_failures: number
}

export interface SimInstance {
  id: number
  scenario_id: string
  scenario_version: string
  seed: number
  sim_time: string
  tick: number
  tick_minutes: number
  status: InstanceStatus
}

export interface Health {
  status: string
  database: string
  simulation: { status: InstanceStatus; tick: number }
}

export interface AuditEntry {
  id: number
  wall_time: string
  sim_time: string
  tick: number
  action: string
  entity_type: string
  entity_id: string
  result: string
  metadata_json: Record<string, unknown>
}

export const DEMAND_PROFILE_NAMES = [
  'urban_high', 'industrial', 'highway', 'regional',
] as const
export type DemandProfileName = (typeof DEMAND_PROFILE_NAMES)[number]

export interface EventRequest {
  type: EventType
  start_tick: number
  duration_ticks: number
  parameters?: Record<string, unknown>
}

export interface FaultRequest {
  type: FaultType
  duration_seconds: number
  parameters?: Record<string, unknown>
}

export const SIMULATOR_ERROR_CODES = [
  'NOT_FOUND',
  'ALLOCATION_NOT_FOUND',
  'IDEMPOTENCY_KEY_MISMATCH',
  'ROUTE_MISMATCH',
  'DEPOT_CLOSED',
  'STATION_CLOSED',
  'ROUTE_DISRUPTED',
  'ROUTE_CAPACITY_EXCEEDED',
  'INSUFFICIENT_INVENTORY',
  'DISPATCH_CAPACITY_EXCEEDED',
] as const
export type SimulatorErrorCode = (typeof SIMULATOR_ERROR_CODES)[number] | 'DESTINATION_CAPACITY_EXCEEDED' | 'CANNOT_CANCEL' | 'FAULT_INJECTED'

function guard<T extends string>(allowed: readonly T[]) {
  const set = new Set<string>(allowed)
  return (value: unknown): value is T => typeof value === 'string' && set.has(value)
}

export const isFuelType = guard(FUEL_TYPES)
export const isDepotStatus = guard(DEPOT_STATUSES)
export const isStationStatus = guard(STATION_STATUSES)
export const isRouteStatus = guard(ROUTE_STATUSES)
export const isSupplyStatus = guard(SUPPLY_STATUSES)
export const isEventStatus = guard(EVENT_STATUSES)
export const isAllocationStatus = guard(ALLOCATION_STATUSES)
export const isInstanceStatus = guard(INSTANCE_STATUSES)
