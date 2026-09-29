import type {
  Allocation, AllocationRequest, AuditEntry, DemandObservation, Depot,
  DomainEvent, EventRequest, Fault, FaultRequest, Health, Metrics, Region,
  Route, SimInstance, Station, SupplyArrival,
} from '../types/simulator'
import type { StreamEventName } from './mock/emitter'

export interface StreamEvent { name: StreamEventName; data: unknown; receivedAt: number }

/**
 * Every read carries its own freshness.
 *
 * Guide 10.2 requires that a stale-data fault invalidate any local cache. By
 * putting `stale` on the response rather than in a side channel, a screen
 * physically cannot render stale data without having been handed the flag.
 */
export interface SimResponse<T> {
  data: T
  stale: boolean
  receivedAt: number
}

export interface StreamHandle {
  on(name: StreamEventName, listener: (event: StreamEvent) => void): () => void
  close(): void
}

export interface SimulatorClient {
  getHealth(): Promise<SimResponse<Health>>
  getInstance(): Promise<SimResponse<SimInstance>>
  getRegions(): Promise<SimResponse<Region[]>>
  getDepots(): Promise<SimResponse<Depot[]>>
  getStations(): Promise<SimResponse<Station[]>>
  getRoutes(): Promise<SimResponse<Route[]>>
  getSupplyArrivals(): Promise<SimResponse<SupplyArrival[]>>
  getEvents(): Promise<SimResponse<DomainEvent[]>>
  getAllocations(): Promise<SimResponse<Allocation[]>>
  getDemandHistory(params: { station_id?: string; limit?: number }): Promise<SimResponse<DemandObservation[]>>
  getMetrics(): Promise<SimResponse<Metrics>>

  postAllocation(body: AllocationRequest): Promise<SimResponse<Allocation>>
  cancelAllocation(id: number): Promise<SimResponse<Allocation>>

  stream(): StreamHandle

  adminRun(): Promise<SimResponse<SimInstance>>
  adminPause(): Promise<SimResponse<SimInstance>>
  adminStep(): Promise<SimResponse<{ tick: number; sim_time: string }>>
  adminReset(): Promise<SimResponse<{ status: string }>>
  adminInjectEvent(body: EventRequest): Promise<SimResponse<DomainEvent>>
  adminInjectFault(body: FaultRequest): Promise<SimResponse<Fault>>
  adminClearFaults(): Promise<SimResponse<{ status: string }>>
  adminGetFaults(): Promise<SimResponse<Fault[]>>
  adminGetEvents(): Promise<SimResponse<DomainEvent[]>>
  getAudit(limit?: number): Promise<SimResponse<AuditEntry[]>>
}
