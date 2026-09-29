// Types for `GET /api/v1/stockout`. They mirror the response schema field for
// field, so a change on the backend is a compile error here rather than a
// silently missing column on the console.

/**
 * The risk band an assessment lands in. Distinct from `StockoutRisk` in the
 * simulator types: that one carries a *probability*, this one carries a *band*.
 */
export type RiskLevel = 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL'

/** One in-transit allocation counted against the horizon, mirrored verbatim. */
export interface IncomingSupply {
  quantity: number
  arrival_tick: number
  allocation_id: string
  source_depot_id: string
}

/** One (station, fuel) row: where inventory lands, and when it crosses zero. */
export interface StockoutAssessment {
  station_id: string
  fuel_type: string
  current_inventory: number
  expected_demand: number
  incoming_supply: number
  projected_inventory: number
  shortage_amount: number
  surplus_amount: number
  stockout_tick: number | null
  ticks_until_stockout: number | null
  risk: RiskLevel
  basis: string
  horizon_ticks: number
  demand_method: string
  demand_confidence: number
  incoming_sources: IncomingSupply[]
  simulated: boolean
}

/** The thresholds the backend actually applied, so the panel can label the bands. */
export interface StockoutThresholds {
  critical_ticks: number
  high_ticks: number
  safety_stock_fraction: number
  horizon_ticks: number
}

export interface StockoutResponse {
  horizon_ticks: number
  generated_at_tick: number | null
  count: number
  thresholds: StockoutThresholds
  summary: Record<RiskLevel, number>
  assessments: StockoutAssessment[]
  simulated: boolean
}

/** Most severe first — the order the console triages in. */
export const RISK_LEVELS: readonly RiskLevel[] = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'] as const
