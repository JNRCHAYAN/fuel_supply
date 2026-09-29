import type {
  Depot, Station, Route, Region, SimInstance, InstanceStatus, FuelType,
  DemandObservation, Metrics, SupplyArrival,
  Allocation, AllocationRequest, DepotStatus, StationStatus, RouteStatus,
  DomainEvent, EventRequest,
  Fault, FaultRequest, FaultType,
  AuditEntry,
} from '../../types/simulator'
import {
  REGIONS, DEPOTS, STATIONS, ROUTES, DEMAND_PROFILES, PROFILE_NOISE,
  HOUR_FACTORS, DEFAULT_SEED, TICK_MINUTES, SUPPLY_SCHEDULE, cloneFuelMap,
} from './fixtures'
import { createRng } from './seed'
import { SimulatorError } from '../errors'

const SIM_EPOCH = '2026-01-01T00:00:00.000Z'

export function hourOfTick(tick: number, tickMinutes: number): number {
  const totalMinutes = tick * tickMinutes
  return Math.floor(totalMinutes / 60) % 24
}

function within(hour: number, ranges: Array<[number, number]>): boolean {
  return ranges.some(([from, to]) => hour >= from && hour <= to)
}

export interface WorldOptions {
  seed?: number
  tickMinutes?: number
  /** The world starts paused; a demo should never advance before the operator says so. */
  startRunning?: boolean
}

export class World {
  tick = 0
  status: InstanceStatus = 'PAUSED'

  private readonly seed: number
  private readonly tickMinutes: number
  private readonly rng: () => number

  private regionsState: Region[]
  private depotsState: Depot[]
  private stationsState: Station[]
  private routesState: Route[]
  private arrivals: SupplyArrival[] = []

  private observed: DemandObservation[] = []
  private nextObservationId = 1
  private servedTotalLiters = 0
  private unmetTotalLiters = 0
  private allocationLiters = 0
  private allocationFailures = 0

  private allocationRows: Allocation[] = []
  private allocationKeys = new Map<string, number>()
  private nextAllocationId = 1

  private eventRows: DomainEvent[] = []
  private nextEventId = 1

  private faultRows: Fault[] = []
  private nextFaultId = 1

  private auditRows: AuditEntry[] = []
  private nextAuditId = 1

  constructor(options: WorldOptions = {}) {
    this.seed = options.seed ?? DEFAULT_SEED
    this.tickMinutes = options.tickMinutes ?? TICK_MINUTES
    this.rng = createRng(this.seed)

    this.regionsState = REGIONS.map((r) => ({ ...r }))
    this.depotsState = DEPOTS.map((d) => ({
      ...d, capacity: cloneFuelMap(d.capacity), inventory: cloneFuelMap(d.inventory),
    }))
    this.stationsState = STATIONS.map((s) => ({
      ...s, capacity: cloneFuelMap(s.capacity), inventory: cloneFuelMap(s.inventory),
    }))
    this.routesState = ROUTES.map((r) => ({ ...r }))

    this.arrivals = SUPPLY_SCHEDULE.map((a) => ({
      id: a.id,
      depot_id: a.depot_id,
      fuel_type: a.fuel_type,
      quantity: a.quantity,
      planned_tick: a.planned_tick,
      actual_tick: null,
      status: 'SCHEDULED' as const,
    }))

    if (options.startRunning) this.status = 'RUNNING'
  }

  get simTime(): string {
    return new Date(
      new Date(SIM_EPOCH).getTime() + this.tick * this.tickMinutes * 60_000,
    ).toISOString()
  }

  instance(): SimInstance {
    return {
      id: 1,
      scenario_id: 'baseline',
      scenario_version: '1.0',
      seed: this.seed,
      sim_time: this.simTime,
      tick: this.tick,
      tick_minutes: this.tickMinutes,
      status: this.status,
    }
  }

  regions(): Region[] { return this.regionsState.map((r) => ({ ...r })) }
  depots(): Depot[] {
    return this.depotsState.map((d) => ({
      ...d, capacity: cloneFuelMap(d.capacity), inventory: cloneFuelMap(d.inventory),
    }))
  }
  stations(): Station[] {
    return this.stationsState.map((s) => ({
      ...s, capacity: cloneFuelMap(s.capacity), inventory: cloneFuelMap(s.inventory),
    }))
  }
  routes(): Route[] { return this.routesState.map((r) => ({ ...r })) }

  run(): void { this.status = 'RUNNING' }
  pause(): void { this.status = 'PAUSED' }
  toggle(): void { this.status = this.status === 'RUNNING' ? 'PAUSED' : 'RUNNING' }

  setDepotStatus(depotId: string, status: DepotStatus): void {
    const depot = this.depotsState.find((d) => d.id === depotId)
    if (depot) depot.status = status
  }

  setStationStatus(stationId: string, status: StationStatus): void {
    const station = this.stationsState.find((s) => s.id === stationId)
    if (station) station.status = status
  }

  setRouteStatus(routeId: string, status: RouteStatus): void {
    const route = this.routesState.find((r) => r.id === routeId)
    if (route) route.status = status
  }

  allocations(): Allocation[] {
    return [...this.allocationRows].sort((a, b) => b.id - a.id).map((a) => ({ ...a }))
  }

  /** Test seam: removes fuel so INSUFFICIENT_INVENTORY can be exercised. */
  drainDepot(depotId: string, fuel: FuelType, amount: number): void {
    const depot = this.depotsState.find((d) => d.id === depotId)
    if (depot) depot.inventory[fuel] = Math.max(0, depot.inventory[fuel] - amount)
  }

  private fail(status: number, code: string, message: string): never {
    throw new SimulatorError(status, code, message)
  }

  /**
   * Validates and creates an allocation. Checks run in the exact order given
   * by Guide 5.2, first failure wins, and every magnitude comparison is a
   * strict > so that a value exactly at the limit is accepted.
   */
  createAllocation(req: AllocationRequest): Allocation {
    // 1. Idempotency.
    const existingId = this.allocationKeys.get(req.idempotency_key)
    if (existingId !== undefined) {
      const existing = this.allocationRows.find((a) => a.id === existingId)!
      const sameBody =
        existing.source_depot_id === req.source_depot_id &&
        existing.destination_station_id === req.destination_station_id &&
        existing.route_id === req.route_id &&
        existing.fuel_type === req.fuel_type &&
        existing.quantity === req.quantity
      if (sameBody) return { ...existing }
      this.fail(409, 'IDEMPOTENCY_KEY_MISMATCH',
        `Idempotency key "${req.idempotency_key}" was already used with a different body.`)
    }

    // 2. Entity existence.
    const depot = this.depotsState.find((d) => d.id === req.source_depot_id)
    if (!depot) this.fail(404, 'NOT_FOUND', `Unknown depot "${req.source_depot_id}".`)
    const station = this.stationsState.find((s) => s.id === req.destination_station_id)
    if (!station) this.fail(404, 'NOT_FOUND', `Unknown station "${req.destination_station_id}".`)
    const route = this.routesState.find((r) => r.id === req.route_id)
    if (!route) this.fail(404, 'NOT_FOUND', `Unknown route "${req.route_id}".`)

    // 3. Route must connect exactly the given pair.
    if (
      route.source_depot_id !== req.source_depot_id ||
      route.destination_station_id !== req.destination_station_id
    ) {
      this.fail(409, 'ROUTE_MISMATCH',
        `Route "${route.id}" connects ${route.source_depot_id} to ${route.destination_station_id}.`)
    }

    // 4. Depot open.
    if (depot.status !== 'OPEN' && depot.status !== 'CONSTRAINED') {
      this.fail(409, 'DEPOT_CLOSED', `Depot "${depot.id}" is ${depot.status}.`)
    }

    // 5. Station open.
    if (station.status !== 'OPEN') {
      this.fail(409, 'STATION_CLOSED', `Station "${station.id}" is ${station.status}.`)
    }

    // 6. Route available.
    if (route.status !== 'AVAILABLE') {
      this.fail(409, 'ROUTE_DISRUPTED', `Route "${route.id}" is ${route.status}.`)
    }

    // 7. Route capacity.
    if (req.quantity > route.max_shipment) {
      this.fail(409, 'ROUTE_CAPACITY_EXCEEDED',
        `Quantity ${req.quantity} exceeds route maximum ${route.max_shipment}.`)
    }

    // 8. Depot inventory.
    if (depot.inventory[req.fuel_type] < req.quantity) {
      this.fail(409, 'INSUFFICIENT_INVENTORY',
        `Depot "${depot.id}" holds ${depot.inventory[req.fuel_type]} L of ${req.fuel_type}.`)
    }

    // 9. Per-tick dispatch capacity, counting this depot's in-flight and
    //    pending quantity created on this tick.
    const committed = this.allocationRows
      .filter((a) =>
        a.source_depot_id === depot.id &&
        a.created_tick === this.tick &&
        (a.status === 'PENDING' || a.status === 'IN_TRANSIT'))
      .reduce((sum, a) => sum + a.quantity, 0)
    if (committed + req.quantity > depot.dispatch_capacity_per_tick) {
      this.fail(409, 'DISPATCH_CAPACITY_EXCEEDED',
        `Depot "${depot.id}" can dispatch ${depot.dispatch_capacity_per_tick} L per tick; ${committed} L already committed.`)
    }

    // 10. Destination headroom.
    if (station.inventory[req.fuel_type] + req.quantity > station.capacity[req.fuel_type]) {
      this.fail(409, 'DESTINATION_CAPACITY_EXCEEDED',
        `Station "${station.id}" can hold ${station.capacity[req.fuel_type]} L of ${req.fuel_type}.`)
    }

    depot.inventory[req.fuel_type] -= req.quantity

    const allocation: Allocation = {
      id: this.nextAllocationId++,
      idempotency_key: req.idempotency_key,
      source_depot_id: req.source_depot_id,
      destination_station_id: req.destination_station_id,
      route_id: req.route_id,
      fuel_type: req.fuel_type,
      quantity: req.quantity,
      created_tick: this.tick,
      // The transit clock starts at creation, not at the next tick: a load on
      // a route with transit_ticks 2 dispatched at tick 0 arrives at tick 2.
      departure_tick: this.tick,
      expected_arrival_tick: this.tick + route.transit_ticks,
      actual_arrival_tick: null,
      status: 'PENDING',
      failure_reason: null,
    }
    this.allocationRows.push(allocation)
    // The key stays occupied permanently, even after cancellation.
    this.allocationKeys.set(req.idempotency_key, allocation.id)
    this.log('allocation.created', 'allocation', String(allocation.id), 'OK', { quantity: allocation.quantity, fuel_type: allocation.fuel_type })
    return { ...allocation }
  }

  cancelAllocation(id: number): Allocation {
    const allocation = this.allocationRows.find((a) => a.id === id)
    if (!allocation) this.fail(404, 'ALLOCATION_NOT_FOUND', `Unknown allocation ${id}.`)
    if (allocation.status !== 'PENDING') {
      this.fail(409, 'CANNOT_CANCEL',
        `Allocation ${id} is ${allocation.status}; only PENDING allocations can be cancelled.`)
    }

    const depot = this.depotsState.find((d) => d.id === allocation.source_depot_id)
    if (depot) {
      depot.inventory[allocation.fuel_type] = Math.min(
        depot.capacity[allocation.fuel_type],
        depot.inventory[allocation.fuel_type] + allocation.quantity,
      )
    }
    allocation.status = 'CANCELLED'
    this.log('allocation.cancelled', 'allocation', String(allocation.id), 'OK', { quantity: allocation.quantity, fuel_type: allocation.fuel_type })
    return { ...allocation }
  }

  /** Advances the lifecycle of every in-flight allocation. */
  private processAllocations(): void {
    for (const a of this.allocationRows) {
      const route = this.routesState.find((r) => r.id === a.route_id)
      if (!route) continue

      if (a.status === 'PENDING') {
        // The transit window was fixed at creation; departing only changes
        // the visible status.
        a.status = 'IN_TRANSIT'
        continue
      }

      if (a.status === 'IN_TRANSIT') {
        // A route disrupted while the load is moving fails the shipment.
        if (route.status !== 'AVAILABLE') {
          a.status = 'FAILED'
          a.failure_reason = `Route ${route.id} became ${route.status} in transit.`
          this.allocationFailures += 1
          continue
        }
        if (a.expected_arrival_tick !== null && this.tick >= a.expected_arrival_tick) {
          const station = this.stationsState.find((s) => s.id === a.destination_station_id)
          if (station) {
            station.inventory[a.fuel_type] = Math.min(
              station.capacity[a.fuel_type],
              station.inventory[a.fuel_type] + a.quantity,
            )
          }
          a.status = 'ARRIVED'
          a.actual_arrival_tick = this.tick
          this.allocationLiters += a.quantity
        }
      }
    }
  }

  events(): DomainEvent[] {
    return [...this.eventRows].sort((a, b) => b.id - a.id).map((e) => ({ ...e, parameters: { ...e.parameters } }))
  }

  injectEvent(req: EventRequest): DomainEvent {
    if (req.start_tick < 0) this.fail(422, 'VALIDATION_ERROR', 'start_tick must be >= 0.')
    if (req.duration_ticks <= 0) this.fail(422, 'VALIDATION_ERROR', 'duration_ticks must be > 0.')

    const event: DomainEvent = {
      id: this.nextEventId++,
      type: req.type,
      start_tick: req.start_tick,
      end_tick: req.start_tick + req.duration_ticks,
      status: 'SCHEDULED',
      parameters: { ...(req.parameters ?? {}) },
    }
    this.eventRows.push(event)
    this.log('event.created', 'event', String(event.id), 'OK', { type: event.type })

    // An event starting at or before the current tick takes effect at once.
    // An operator injecting a crisis expects to see it, not to wait for the
    // next tick; and a paused simulation would otherwise never apply it.
    if (event.start_tick <= this.tick) {
      this.applyEvent(event)
      event.status = 'ACTIVE'
    }

    return { ...event, parameters: { ...event.parameters } }
  }

  /**
   * Applies and reverses events by tick.
   *
   * Guide 7.8 splits the six types in two. demand_spike, route_disruption,
   * station_outage and depot_constraint reverse on resolve. shipment_delay and
   * supply_shortfall are one-shot: they mutate once and are never undone.
   */
  private processEvents(): void {
    for (const event of this.eventRows) {
      if (event.status === 'SCHEDULED' && this.tick >= event.start_tick) {
        this.applyEvent(event)
        event.status = 'ACTIVE'
      } else if (event.status === 'ACTIVE' && this.tick >= event.end_tick) {
        this.reverseEvent(event)
        event.status = 'RESOLVED'
      }
    }
  }

  private applyEvent(event: DomainEvent): void {
    const p = event.parameters

    switch (event.type) {
      case 'demand_spike': {
        const multiplier = num(p.multiplier, 1.5)
        for (const station of this.affectedStations(p)) {
          // Clamped so a repeated tiny multiplier can never reach zero and
          // divide-by-zero on reversal.
          station.demand_multiplier = Math.max(0.01, station.demand_multiplier * multiplier)
        }
        break
      }
      case 'route_disruption':
        for (const route of this.affectedRoutes(p)) this.setRouteStatus(route.id, 'DISRUPTED')
        break
      case 'station_outage':
        for (const station of this.affectedStations(p)) this.setStationStatus(station.id, 'OUTAGE')
        break
      case 'depot_constraint':
        for (const depot of this.affectedDepots(p)) this.setDepotStatus(depot.id, 'CONSTRAINED')
        break
      case 'shipment_delay': {
        const delayTicks = num(p.delay_ticks, 2)
        for (const arrival of this.affectedArrivals(p)) {
          if (arrival.status === 'ARRIVED') continue
          arrival.planned_tick += delayTicks
          arrival.status = 'DELAYED'
        }
        break
      }
      case 'supply_shortfall': {
        const factor = num(p.factor, 0.5)
        for (const arrival of this.affectedArrivals(p)) {
          if (arrival.status === 'ARRIVED') continue
          arrival.quantity = arrival.quantity * factor
        }
        break
      }
    }
  }

  private reverseEvent(event: DomainEvent): void {
    const p = event.parameters

    switch (event.type) {
      case 'demand_spike': {
        const multiplier = num(p.multiplier, 1.5)
        if (multiplier <= 0) break
        for (const station of this.affectedStations(p)) {
          station.demand_multiplier = Math.max(0.01, station.demand_multiplier / multiplier)
        }
        break
      }
      case 'route_disruption':
        for (const route of this.affectedRoutes(p)) this.setRouteStatus(route.id, 'AVAILABLE')
        break
      case 'station_outage':
        for (const station of this.affectedStations(p)) this.setStationStatus(station.id, 'OPEN')
        break
      case 'depot_constraint':
        for (const depot of this.affectedDepots(p)) this.setDepotStatus(depot.id, 'OPEN')
        break
      // shipment_delay and supply_shortfall are one-shot and deliberately
      // have no reversal branch.
      case 'shipment_delay':
      case 'supply_shortfall':
        break
    }
  }

  /** An empty filter list means "every entity of that type". Guide 7.8. */
  private affectedStations(p: Record<string, unknown>) {
    const ids = strArray(p.station_ids)
    const regionIds = strArray(p.region_ids)
    return this.stationsState.filter((s) => {
      if (ids.length > 0 && !ids.includes(s.id)) return false
      if (regionIds.length > 0 && !regionIds.includes(s.region_id)) return false
      return true
    })
  }

  private affectedRoutes(p: Record<string, unknown>) {
    const ids = strArray(p.route_ids)
    return this.routesState.filter((r) => ids.length === 0 || ids.includes(r.id))
  }

  private affectedDepots(p: Record<string, unknown>) {
    const ids = strArray(p.depot_ids)
    return this.depotsState.filter((d) => ids.length === 0 || ids.includes(d.id))
  }

  private affectedArrivals(p: Record<string, unknown>) {
    const depotIds = strArray(p.depot_ids)
    const fuelTypes = strArray(p.fuel_types)
    return this.arrivals.filter((a) => {
      if (depotIds.length > 0 && !depotIds.includes(a.depot_id)) return false
      if (fuelTypes.length > 0 && !fuelTypes.includes(a.fuel_type)) return false
      return true
    })
  }

  injectFault(req: FaultRequest): Fault {
    if (req.duration_seconds <= 0 || req.duration_seconds > 3600) {
      this.fail(422, 'VALIDATION_ERROR', 'duration_seconds must be greater than 0 and at most 3600.')
    }
    const now = Date.now()
    const fault: Fault = {
      id: this.nextFaultId++,
      type: req.type,
      start_wall_time: new Date(now).toISOString(),
      end_wall_time: new Date(now + req.duration_seconds * 1000).toISOString(),
      active: true,
      parameters: { ...(req.parameters ?? {}) },
    }
    this.faultRows.push(fault)
    return { ...fault }
  }

  /** Test seam: ages faults out without waiting on wall-clock time. */
  expireFaults(nowMs: number = Date.now()): void {
    for (const fault of this.faultRows) {
      if (new Date(fault.end_wall_time).getTime() <= nowMs) fault.active = false
    }
  }

  activeFaults(): Fault[] {
    this.expireFaults()
    return this.faultRows.filter((f) => f.active).map((f) => ({ ...f }))
  }

  clearFaults(): void {
    for (const fault of this.faultRows) fault.active = false
  }

  /** Guide 7.6: wipe everything and reload the scenario. */
  reset(): void {
    this.tick = 0
    this.status = 'PAUSED'
    this.observed = []
    this.nextObservationId = 1
    this.servedTotalLiters = 0
    this.unmetTotalLiters = 0
    this.allocationLiters = 0
    this.allocationFailures = 0
    this.allocationRows = []
    this.allocationKeys = new Map()
    this.nextAllocationId = 1
    this.eventRows = []
    this.nextEventId = 1
    this.faultRows = []
    this.nextFaultId = 1
    this.auditRows = []
    this.nextAuditId = 1

    this.regionsState = REGIONS.map((r) => ({ ...r }))
    this.depotsState = DEPOTS.map((d) => ({
      ...d, capacity: cloneFuelMap(d.capacity), inventory: cloneFuelMap(d.inventory),
    }))
    this.stationsState = STATIONS.map((s) => ({
      ...s, capacity: cloneFuelMap(s.capacity), inventory: cloneFuelMap(s.inventory),
    }))
    this.routesState = ROUTES.map((r) => ({ ...r }))
    this.arrivals = SUPPLY_SCHEDULE.map((a) => ({
      id: a.id, depot_id: a.depot_id, fuel_type: a.fuel_type, quantity: a.quantity,
      planned_tick: a.planned_tick, actual_tick: null, status: 'SCHEDULED' as const,
    }))
  }

  log(action: string, entityType: string, entityId: string, result = 'OK', metadata: Record<string, unknown> = {}): void {
    this.auditRows.push({
      id: this.nextAuditId++,
      wall_time: new Date().toISOString(),
      sim_time: this.simTime,
      tick: this.tick,
      action, entity_type: entityType, entity_id: entityId, result,
      metadata_json: metadata,
    })
    if (this.auditRows.length > 1000) this.auditRows = this.auditRows.slice(-1000)
  }

  /** Guide 7.12: limit clamped to [1, 1000], sorted id-desc. */
  audit(limit = 200): AuditEntry[] {
    const clamped = Math.min(1000, Math.max(1, Math.trunc(limit)))
    return [...this.auditRows].sort((a, b) => b.id - a.id).slice(0, clamped)
  }

  private hasFault(type: FaultType): boolean {
    return this.activeFaults().some((f) => f.type === type)
  }

  private faultParams(type: FaultType): Record<string, unknown> {
    return this.activeFaults().find((f) => f.type === type)?.parameters ?? {}
  }

  /**
   * Whether a /v1 request should fail. Guide 7.10: unavailable always fails,
   * error_rate fails with the given probability, and the other three faults do
   * not fail requests at all. /admin/* and /v1/health bypass this entirely.
   */
  shouldFailRequest(): { code: 'FAULT_INJECTED'; message: string; status: number } | null {
    if (this.hasFault('unavailable')) {
      return { code: 'FAULT_INJECTED', status: 503, message: 'Simulator API temporarily unavailable.' }
    }
    if (this.hasFault('error_rate')) {
      const rate = clamp01(num(this.faultParams('error_rate').rate, 0.25))
      if (this.rng() < rate) {
        return { code: 'FAULT_INJECTED', status: 503, message: 'Injected transient API error.' }
      }
    }
    return null
  }

  requestDelayMs(): number {
    if (!this.hasFault('latency')) return 0
    return Math.max(0, num(this.faultParams('latency').delay_ms, 500))
  }

  isStale(): boolean { return this.hasFault('stale_data') }
  isStreamDisconnected(): boolean { return this.hasFault('stream_disconnect') }

  /**
   * Demand for one station/fuel at an arbitrary tick.
   *
   * Guide 8.5 gives liters per simulated day per profile; 8.6 gives an
   * hour-of-day multiplier; the region carries a demand factor; the station
   * carries a runtime multiplier mutated by demand_spike events.
   *
   * The noise term is derived from (seed, station, fuel, tick) rather than
   * drawn from a shared stream, so asking for demand out of order still gives
   * the same answer. That property is what makes the world deterministic
   * under replay and forecast queries.
   *
   * `noiseless` exists so the model's arithmetic can be asserted exactly
   * rather than only compared against itself.
   */
  demandForTick(
    stationId: string,
    fuel: FuelType,
    tick: number,
    options: { noiseless?: boolean } = {},
  ): number {
    const station = this.stationsState.find((s) => s.id === stationId)
    // An unknown station yields no demand. Returning 0 rather than NaN keeps
    // a stale id from poisoning every downstream total.
    if (!station) return 0

    const profile = DEMAND_PROFILES[station.demand_profile]
    const dailyLiters = profile[fuel]
    const region = this.regionsState.find((r) => r.id === station.region_id)
    const regionFactor = region?.demand_factor ?? 1
    const hour = hourOfTick(tick, this.tickMinutes)
    const factors = HOUR_FACTORS[station.demand_profile]
    const hourFactor = within(hour, factors.busy) ? factors.busyFactor : factors.offPeakFactor

    const perTick = (dailyLiters / (24 * 60)) * this.tickMinutes
    const base = perTick * regionFactor * hourFactor * station.demand_multiplier

    if (options.noiseless) return Math.max(0, base)

    const noise = PROFILE_NOISE[station.demand_profile]
    const jitter = 1 + (this.jitter(stationId, fuel, tick) * 2 - 1) * noise

    const value = base * jitter
    // A malformed multiplier must never propagate NaN into every downstream
    // chart; an unavailable figure is reported as zero and flagged elsewhere.
    return Number.isFinite(value) && value > 0 ? value : 0
  }

  /** Deterministic per-(station, fuel, tick) jitter in [0, 1). */
  private jitter(stationId: string, fuel: FuelType, tick: number): number {
    let h = this.seed >>> 0
    const key = `${stationId}|${fuel}|${tick}`
    for (let i = 0; i < key.length; i++) {
      h = Math.imul(h ^ key.charCodeAt(i), 0x01000193) >>> 0
    }
    h = Math.imul(h ^ (h >>> 15), 0x2c1b3c6d) >>> 0
    return (h >>> 0) / 4294967296
  }

  /** Advances the world by exactly one tick. Extended in Tasks 6-12. */
  step(): void {
    this.tick += 1
    // Events resolve before anything else moves, so a disruption that starts
    // this tick already applies to this tick's arrivals and allocations.
    this.processEvents()
    this.processArrivals()
    this.processAllocations()
    this.observeTick()
  }

  /** Guide 4.8: sorted by planned_tick ascending. */
  supplyArrivals(): SupplyArrival[] {
    return [...this.arrivals]
      .sort((a, b) => a.planned_tick - b.planned_tick)
      .map((a) => ({ ...a }))
  }

  /**
   * Lands every scheduled arrival whose planned tick has been reached.
   * Once ARRIVED an arrival is never reprocessed, so fuel cannot be
   * deposited twice.
   */
  private processArrivals(): void {
    for (const arrival of this.arrivals) {
      if (arrival.status === 'ARRIVED') continue
      if (arrival.planned_tick > this.tick) continue

      const depot = this.depotsState.find((d) => d.id === arrival.depot_id)
      if (!depot) continue

      const capacity = depot.capacity[arrival.fuel_type]
      depot.inventory[arrival.fuel_type] = Math.min(
        capacity,
        depot.inventory[arrival.fuel_type] + arrival.quantity,
      )

      arrival.status = 'ARRIVED'
      arrival.actual_tick = this.tick
    }
  }

  /**
   * Records one tick of demand for every station and fuel, consuming station
   * inventory for whatever can be served. Guide 4.11: one row per
   * (station_id, fuel_type) per tick, so twelve rows per tick.
   */
  observeTick(): void {
    for (const station of this.stationsState) {
      for (const fuel of ['DIESEL', 'PETROL', 'OCTANE'] as const) {
        const demand = this.demandForTick(station.id, fuel, this.tick)
        const available = station.status === 'OUTAGE' ? 0 : station.inventory[fuel]
        const served = Math.min(demand, available)
        const unmet = demand - served

        station.inventory[fuel] = Math.max(0, available - served)
        this.servedTotalLiters += served
        this.unmetTotalLiters += unmet

        this.observed.push({
          id: this.nextObservationId++,
          station_id: station.id,
          fuel_type: fuel,
          tick: this.tick,
          sim_time: this.simTime,
          demand_liters: demand,
          served_liters: served,
          unmet_liters: unmet,
        })
      }
    }
  }

  /** Most recent rows first. Guide 4.11: limit is clamped to [1, 2000]. */
  demandHistory(params: { station_id?: string; limit?: number }): DemandObservation[] {
    const limit = Math.min(2000, Math.max(1, Math.trunc(params.limit ?? 200)))
    const pool = params.station_id
      ? this.observed.filter((r) => r.station_id === params.station_id)
      : this.observed
    return pool.slice(-limit).reverse()
  }

  metrics(): Metrics {
    const total = this.servedTotalLiters + this.unmetTotalLiters
    return {
      served_demand_liters: this.servedTotalLiters,
      unmet_demand_liters: this.unmetTotalLiters,
      // With no demand recorded, service level is perfect rather than NaN.
      service_level: total === 0 ? 1 : this.servedTotalLiters / total,
      allocation_liters: this.allocationLiters,
      allocation_failures: this.allocationFailures,
    }
  }
}

function num(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function strArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : []
}

function clamp01(value: number): number {
  return Math.min(1, Math.max(0, value))
}
