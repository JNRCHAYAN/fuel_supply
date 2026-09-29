import { fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { RiskLevel, StockoutAssessment, StockoutResponse } from '../../lib/types/stockout'
import { StockoutPanel } from './StockoutPanel'
import { useStockout } from './useStockout'

// The panel is tested against controlled payloads: the hook keeps its own
// coverage, so a change to the fetch cadence must not fail these tests.
vi.mock('./useStockout', () => ({ useStockout: vi.fn() }))

const mockUseStockout = vi.mocked(useStockout)

function assessment(overrides: Partial<StockoutAssessment> = {}): StockoutAssessment {
  return {
    station_id: 'station-mirpur',
    fuel_type: 'DIESEL',
    current_inventory: 0,
    expected_demand: 11900,
    incoming_supply: 0,
    projected_inventory: -11900,
    shortage_amount: 11900,
    surplus_amount: 0,
    stockout_tick: 9061,
    ticks_until_stockout: 0,
    risk: 'CRITICAL',
    basis: 'projected -11,900 L = 0 current + 0 incoming − 11,900 demand over 8 ticks',
    horizon_ticks: 8,
    demand_method: 'holt_winters',
    demand_confidence: 0.42,
    incoming_sources: [],
    simulated: true,
    ...overrides,
  }
}

function response(overrides: Partial<StockoutResponse> = {}): StockoutResponse {
  return {
    horizon_ticks: 8,
    generated_at_tick: 9060,
    count: 2,
    thresholds: { critical_ticks: 1, high_ticks: 3, safety_stock_fraction: 0.25, horizon_ticks: 8 },
    summary: { CRITICAL: 1, HIGH: 0, MEDIUM: 0, LOW: 1 },
    assessments: [],
    simulated: true,
    ...overrides,
  }
}

function serve(state: Partial<ReturnType<typeof useStockout>> = {}) {
  mockUseStockout.mockReturnValue({
    data: null, error: false, loading: false, refresh: vi.fn(), ...state,
  })
}

const CRITICAL_ROW = assessment({ station_id: 'station-mirpur', fuel_type: 'DIESEL', risk: 'CRITICAL' })
const LOW_ROW = assessment({
  station_id: 'station-uttara',
  fuel_type: 'PETROL',
  risk: 'LOW',
  current_inventory: 90000,
  expected_demand: 12000,
  projected_inventory: 78000,
  shortage_amount: 0,
  surplus_amount: 78000,
  stockout_tick: null,
  ticks_until_stockout: null,
  basis: 'projected 78,000 L over 8 ticks',
})

/** The tile for one level, located by its hint: each level's hint is unique. */
function tile(level: RiskLevel) {
  const hint = screen.getByText(new RegExp(`^${level} · `))
  return within(hint.parentElement as HTMLElement)
}

/** The whole `<tr>` for a station, so cell-level assertions stay scoped. */
function rowFor(stationId: string): HTMLElement {
  return screen.getByText(stationId).closest('tr') as HTMLElement
}

beforeEach(() => {
  mockUseStockout.mockReset()
})

describe('StockoutPanel', () => {
  it('renders the four risk counts from the summary', () => {
    serve({ data: response({ summary: { CRITICAL: 7, HIGH: 3, MEDIUM: 0, LOW: 4 } }) })
    render(<StockoutPanel />)

    expect(screen.getByRole('heading', { name: 'Stockout risk' })).toBeInTheDocument()
    expect(tile('CRITICAL').getByText('7')).toBeInTheDocument()
    expect(tile('HIGH').getByText('3')).toBeInTheDocument()
    // A zero is a real count, not a missing one.
    expect(tile('MEDIUM').getByText('0')).toBeInTheDocument()
    expect(tile('LOW').getByText('4')).toBeInTheDocument()
  })

  it('renders one row per assessment, each carrying its risk badge', () => {
    serve({ data: response({ assessments: [CRITICAL_ROW, LOW_ROW] }) })
    render(<StockoutPanel />)

    const table = screen.getByRole('table')
    expect(within(table).getAllByRole('row')).toHaveLength(3) // header + two rows
    expect(within(rowFor('station-mirpur')).getByText('CRITICAL')).toBeInTheDocument()
    expect(within(rowFor('station-uttara')).getByText('LOW')).toBeInTheDocument()
    expect(within(rowFor('station-uttara')).getByText('PETROL')).toBeInTheDocument()
    expect(screen.getByText(/Showing 2 of 2 rows/)).toBeInTheDocument()
  })

  it('filters rows by risk client-side without changing the summary tiles', () => {
    serve({ data: response({ assessments: [CRITICAL_ROW, LOW_ROW] }) })
    render(<StockoutPanel />)

    const select = screen.getByRole('combobox')
    expect(screen.getByText('station-mirpur')).toBeInTheDocument()
    expect(screen.getByText('station-uttara')).toBeInTheDocument()

    fireEvent.change(select, { target: { value: 'CRITICAL' } })
    expect(screen.getByText('station-mirpur')).toBeInTheDocument()
    expect(screen.queryByText('station-uttara')).not.toBeInTheDocument()
    // The counts describe the network, not the current filter.
    expect(tile('CRITICAL').getByText('1')).toBeInTheDocument()
    expect(tile('LOW').getByText('1')).toBeInTheDocument()

    fireEvent.change(select, { target: { value: 'LOW' } })
    expect(screen.queryByText('station-mirpur')).not.toBeInTheDocument()
    expect(screen.getByText('station-uttara')).toBeInTheDocument()
    expect(tile('CRITICAL').getByText('1')).toBeInTheDocument()
  })

  it('shows the loading state while there is no data yet', () => {
    serve({ data: null, error: false, loading: true })
    render(<StockoutPanel />)

    expect(screen.getByRole('status')).toHaveTextContent('Loading stockout projections')
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
  })

  it('announces an unavailable projection rather than rendering an empty table', () => {
    serve({ data: null, error: true, loading: false })
    render(<StockoutPanel />)

    expect(screen.getByRole('alert')).toHaveTextContent(/unavailable/i)
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('keeps the last projection visible behind a stale banner after a failed refresh', () => {
    serve({ data: response({ assessments: [CRITICAL_ROW, LOW_ROW] }), error: true, loading: false })
    render(<StockoutPanel />)

    expect(screen.getByRole('alert')).toHaveTextContent(/stale data/i)
    expect(screen.getByText('station-mirpur')).toBeInTheDocument()
  })

  it('shows the empty state when the filter matches nothing', () => {
    serve({ data: response({ assessments: [LOW_ROW], summary: { CRITICAL: 0, HIGH: 0, MEDIUM: 0, LOW: 1 } }) })
    render(<StockoutPanel />)

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'CRITICAL' } })

    expect(screen.getByText('No stations match this risk level')).toBeInTheDocument()
    expect(screen.getByText(/No station currently sits at Critical risk/)).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('renders an em dash for an unprojected crossing and "now" for an immediate one', () => {
    const surplusRow = assessment({
      station_id: 'station-uttara', fuel_type: 'PETROL', risk: 'LOW',
      ticks_until_stockout: null, stockout_tick: null,
      projected_inventory: 500, shortage_amount: 0, surplus_amount: 500,
    })
    serve({ data: response({ assessments: [surplusRow, CRITICAL_ROW] }) })
    render(<StockoutPanel />)

    // The surplus row renders no other em dash, so this pins the Stockout cell.
    expect(within(rowFor('station-uttara')).getByText('—')).toBeInTheDocument()
    expect(within(rowFor('station-mirpur')).getByText('now')).toBeInTheDocument()
  })

  it('exposes each row’s basis so the numbers can be traced to their arithmetic', () => {
    serve({ data: response({ assessments: [LOW_ROW] }) })
    render(<StockoutPanel />)

    const row = rowFor('station-uttara')
    expect(row).toHaveAttribute('title', LOW_ROW.basis)
    expect(within(row).getByText(LOW_ROW.basis)).toBeInTheDocument()
  })

  it('renders safely on an empty assessment list', () => {
    serve({ data: response({ assessments: [], summary: { CRITICAL: 0, HIGH: 0, MEDIUM: 0, LOW: 0 } }) })
    render(<StockoutPanel />)

    expect(screen.getByRole('heading', { name: 'Stockout risk' })).toBeInTheDocument()
    expect(screen.getByText('No stations match this risk level')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('refreshes on demand', () => {
    const refresh = vi.fn()
    serve({ data: response({ assessments: [CRITICAL_ROW] }), refresh })
    render(<StockoutPanel />)

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(refresh).toHaveBeenCalledTimes(1)
  })
})
