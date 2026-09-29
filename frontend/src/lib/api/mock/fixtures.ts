import type {
  Depot, Station, Route, Region, DemandProfileName, FuelMap, FuelType,
} from '../../types/simulator'

export const DEFAULT_SEED = 12345
export const TICK_MINUTES = 15
export const DEFAULT_SIMULATION_SPEED = 8

export const REGIONS: Region[] = [
  { id: 'region-dhaka', name: 'Dhaka Division', demand_factor: 1.0 },
  { id: 'region-chattogram', name: 'Chattogram Division', demand_factor: 1.08 },
]

export const DEPOTS: Depot[] = [
  {
    id: 'depot-gazipur',
    name: 'Gazipur Depot',
    region_id: 'region-dhaka',
    status: 'OPEN',
    dispatch_capacity_per_tick: 12000,
    capacity: { DIESEL: 90000, PETROL: 70000, OCTANE: 45000 },
    inventory: { DIESEL: 60000, PETROL: 45000, OCTANE: 26000 },
  },
  {
    id: 'depot-patiya',
    name: 'Patiya Depot',
    region_id: 'region-chattogram',
    status: 'OPEN',
    dispatch_capacity_per_tick: 11000,
    capacity: { DIESEL: 85000, PETROL: 65000, OCTANE: 40000 },
    inventory: { DIESEL: 55000, PETROL: 42000, OCTANE: 24000 },
  },
]

export const STATIONS: Station[] = [
  {
    id: 'station-mirpur', name: 'Mirpur Fuel Station', region_id: 'region-dhaka',
    status: 'OPEN', demand_profile: 'urban_high', demand_multiplier: 1.0,
    capacity: { DIESEL: 15000, PETROL: 14000, OCTANE: 9000 },
    inventory: { DIESEL: 9000, PETROL: 9000, OCTANE: 5000 },
  },
  {
    id: 'station-tongi', name: 'Tongi Fuel Station', region_id: 'region-dhaka',
    status: 'OPEN', demand_profile: 'industrial', demand_multiplier: 1.0,
    capacity: { DIESEL: 18000, PETROL: 9000, OCTANE: 6000 },
    inventory: { DIESEL: 11000, PETROL: 6000, OCTANE: 3500 },
  },
  {
    id: 'station-karnaphuli', name: 'Karnaphuli Fuel Station', region_id: 'region-chattogram',
    status: 'OPEN', demand_profile: 'highway', demand_multiplier: 1.0,
    capacity: { DIESEL: 14000, PETROL: 15000, OCTANE: 9000 },
    inventory: { DIESEL: 8500, PETROL: 9500, OCTANE: 5200 },
  },
  {
    id: 'station-coxsbazar', name: "Cox's Bazar Fuel Station", region_id: 'region-chattogram',
    status: 'OPEN', demand_profile: 'regional', demand_multiplier: 1.0,
    capacity: { DIESEL: 12000, PETROL: 12000, OCTANE: 7000 },
    inventory: { DIESEL: 7500, PETROL: 7500, OCTANE: 4200 },
  },
]

export const ROUTES: Route[] = [
  { id: 'route-gazipur-mirpur', source_depot_id: 'depot-gazipur', destination_station_id: 'station-mirpur', transit_ticks: 2, max_shipment: 7000, status: 'AVAILABLE' },
  { id: 'route-gazipur-tongi', source_depot_id: 'depot-gazipur', destination_station_id: 'station-tongi', transit_ticks: 2, max_shipment: 6500, status: 'AVAILABLE' },
  { id: 'route-patiya-karnaphuli', source_depot_id: 'depot-patiya', destination_station_id: 'station-karnaphuli', transit_ticks: 2, max_shipment: 7000, status: 'AVAILABLE' },
  { id: 'route-patiya-coxsbazar', source_depot_id: 'depot-patiya', destination_station_id: 'station-coxsbazar', transit_ticks: 3, max_shipment: 6000, status: 'AVAILABLE' },
  { id: 'route-gazipur-karnaphuli', source_depot_id: 'depot-gazipur', destination_station_id: 'station-karnaphuli', transit_ticks: 4, max_shipment: 5000, status: 'AVAILABLE' },
  { id: 'route-patiya-mirpur', source_depot_id: 'depot-patiya', destination_station_id: 'station-mirpur', transit_ticks: 4, max_shipment: 5000, status: 'AVAILABLE' },
]

/** Liters per simulated day, per profile. Guide 8.5. */
export const DEMAND_PROFILES: Record<DemandProfileName, FuelMap> = {
  urban_high: { DIESEL: 8500, PETROL: 10500, OCTANE: 5600 },
  industrial: { DIESEL: 14000, PETROL: 4500, OCTANE: 2200 },
  highway: { DIESEL: 10500, PETROL: 11000, OCTANE: 6200 },
  regional: { DIESEL: 7200, PETROL: 7600, OCTANE: 3600 },
}

export const PROFILE_NOISE: Record<DemandProfileName, number> = {
  urban_high: 0.10,
  industrial: 0.08,
  highway: 0.12,
  regional: 0.10,
}

/**
 * Hour-of-day multiplier. Guide 8.6.
 * `busy` and `offPeak` are inclusive hour ranges in simulated local hours.
 */
export const HOUR_FACTORS: Record<
  DemandProfileName,
  { busy: Array<[number, number]>; busyFactor: number; offPeakFactor: number }
> = {
  industrial: { busy: [[6, 17]], busyFactor: 1.55, offPeakFactor: 0.45 },
  highway: { busy: [[6, 9], [16, 20]], busyFactor: 1.35, offPeakFactor: 0.75 },
  urban_high: { busy: [[7, 9], [16, 20]], busyFactor: 1.45, offPeakFactor: 0.70 },
  regional: { busy: [[7, 20]], busyFactor: 1.25, offPeakFactor: 0.65 },
}

export interface ScheduledArrival {
  id: string
  depot_id: string
  fuel_type: FuelType
  quantity: number
  planned_tick: number
}

/**
 * The shared 22-arrival schedule. Guide 8.7: four initial-burst arrivals at
 * ticks 12-20 covering day one, then eighteen recurring resupplies spaced 64
 * ticks apart (~16 simulated hours) sized to roughly one day of regional demand.
 */
function buildSupplySchedule(): ScheduledArrival[] {
  const out: ScheduledArrival[] = []
  const burst: Array<[string, FuelType, number, number]> = [
    ['depot-gazipur', 'DIESEL', 18000, 12],
    ['depot-gazipur', 'PETROL', 15000, 14],
    ['depot-patiya', 'DIESEL', 17000, 16],
    ['depot-patiya', 'OCTANE', 11000, 20],
  ]
  burst.forEach(([depot_id, fuel_type, quantity, planned_tick], i) => {
    out.push({ id: `supply-burst-${i + 1}`, depot_id, fuel_type, quantity, planned_tick })
  })

  // Roughly one day of regional demand per depot, per fuel, every 64 ticks.
  const recurring: Array<[string, FuelType, number]> = [
    ['depot-gazipur', 'DIESEL', 22000],
    ['depot-gazipur', 'PETROL', 16500],
    ['depot-gazipur', 'OCTANE', 8800],
    ['depot-patiya', 'DIESEL', 21000],
    ['depot-patiya', 'PETROL', 16000],
    ['depot-patiya', 'OCTANE', 8600],
  ]
  let n = 0
  for (let cycle = 0; cycle < 3; cycle++) {
    const planned_tick = 64 + cycle * 64
    for (const [depot_id, fuel_type, quantity] of recurring) {
      n += 1
      out.push({
        id: `supply-rec-${String(n).padStart(3, '0')}`,
        depot_id, fuel_type, quantity, planned_tick,
      })
    }
  }
  return out
}

export const SUPPLY_SCHEDULE: ScheduledArrival[] = buildSupplySchedule()

export function cloneFuelMap(m: FuelMap): FuelMap {
  return { DIESEL: m.DIESEL, PETROL: m.PETROL, OCTANE: m.OCTANE }
}
