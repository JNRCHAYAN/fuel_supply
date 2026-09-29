import type {
  Allocation, AllocationRequest, DemandObservation, Depot, DomainEvent,
  EventRequest, FaultRequest, Health, Metrics, Region, Route, SimInstance,
  Station, SupplyArrival,
} from '../../types/simulator'
import type { SimulatorClient, SimResponse, StreamHandle } from '../client'
import { SimulatorError } from '../errors'
import { World, type WorldOptions } from './world'
import { MockEmitter, type StreamEventName } from './emitter'

export interface MockClient extends SimulatorClient {
  /** Exposed for the Scenario and Resilience screens, which drive the world directly. */
  world: World
  /** Emits a simulation.tick on the bus; the world has no timer of its own. */
  publishTick(): void
}

export function createMockClient(options: WorldOptions = {}): MockClient {
  const world = new World(options)
  const emitter = new MockEmitter()

  function ok<T>(data: T): SimResponse<T> {
    return { data, stale: world.isStale(), receivedAt: Date.now() }
  }

  /**
   * Mirrors the Guide's fault surface: /admin/* and /v1/health bypass fault
   * injection entirely, everything else is subject to it.
   */
  function guard(): void {
    const failure = world.shouldFailRequest()
    if (failure) throw new SimulatorError(failure.status, failure.code, failure.message)
  }

  async function v1<T>(produce: () => T): Promise<SimResponse<T>> {
    guard()
    const delay = world.requestDelayMs()
    if (delay > 0) await new Promise((resolve) => setTimeout(resolve, delay))
    return ok(produce())
  }

  async function admin<T>(produce: () => T): Promise<SimResponse<T>> {
    return ok(produce())
  }

  return {
    world,
    publishTick() {
      emitter.emit('simulation.tick', { tick: world.tick, sim_time: world.simTime })
    },

    getHealth: () => admin<Health>(() => ({
      status: 'ok',
      database: 'ok',
      simulation: { status: world.status, tick: world.tick },
    })),
    getInstance: () => v1<SimInstance>(() => world.instance()),
    getRegions: () => v1<Region[]>(() => world.regions()),
    getDepots: () => v1<Depot[]>(() => world.depots()),
    getStations: () => v1<Station[]>(() => world.stations()),
    getRoutes: () => v1<Route[]>(() => world.routes()),
    getSupplyArrivals: () => v1<SupplyArrival[]>(() => world.supplyArrivals()),
    getEvents: () => v1<DomainEvent[]>(() => world.events()),
    getAllocations: () => v1<Allocation[]>(() => world.allocations()),
    getDemandHistory: (params) => v1<DemandObservation[]>(() => world.demandHistory(params)),
    getMetrics: () => v1<Metrics>(() => world.metrics()),

    postAllocation: (body: AllocationRequest) => v1<Allocation>(() => {
      const created = world.createAllocation(body)
      emitter.emit('allocation.status_changed', created)
      return created
    }),
    cancelAllocation: (id: number) => v1<Allocation>(() => {
      const cancelled = world.cancelAllocation(id)
      emitter.emit('allocation.status_changed', cancelled)
      return cancelled
    }),

    stream(): StreamHandle {
      // Guide 6.4: while stream_disconnect is active the stream returns 503
      // rather than opening.
      if (world.isStreamDisconnected()) {
        throw new SimulatorError(503, 'FAULT_INJECTED', 'Simulator stream temporarily unavailable.')
      }
      const subscriptions: Array<() => void> = []
      let closed = false
      return {
        on(name: StreamEventName, listener) {
          const off = emitter.subscribe((event) => {
            if (closed || event.name !== name) return
            listener(event)
          })
          subscriptions.push(off)
          return off
        },
        close() {
          if (closed) return
          closed = true
          subscriptions.forEach((off) => off())
          subscriptions.length = 0
        },
      }
    },

    adminRun: () => admin(() => { world.run(); return world.instance() }),
    adminPause: () => admin(() => { world.pause(); return world.instance() }),
    adminStep: () => admin(() => {
      world.step()
      emitter.emit('simulation.tick', { tick: world.tick, sim_time: world.simTime })
      return { tick: world.tick, sim_time: world.simTime }
    }),
    adminReset: () => admin(() => {
      world.reset()
      emitter.emit('simulator.notice', { message: 'Simulation reset' })
      return { status: 'reset' }
    }),
    adminInjectEvent: (body: EventRequest) => admin(() => world.injectEvent(body)),
    adminInjectFault: (body: FaultRequest) => admin(() => world.injectFault(body)),
    adminClearFaults: () => admin(() => { world.clearFaults(); return { status: 'cleared' } }),
    adminGetFaults: () => admin(() => world.activeFaults()),
    adminGetEvents: () => admin(() => world.events()),
    getAudit: (limit?: number) => admin(() => world.audit(limit)),
  }
}
