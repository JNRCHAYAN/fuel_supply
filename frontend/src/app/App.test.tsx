import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, it, expect, vi } from 'vitest'
import { App } from './App'

const snapshot = {
  tick: 8, sim_time: '2026-01-01T02:00:00Z', status: 'RUNNING', stale: false, age_seconds: 0,
  depots: [{ id: 'd1', name: 'Gazipur Depot', region_id: 'r1', status: 'OPEN', inventory: { DIESEL: 1000, PETROL: 2000, OCTANE: 3000 }, capacity: { DIESEL: 10000, PETROL: 10000, OCTANE: 10000 } }],
  stations: [{ id: 's1', name: 'Mirpur Station', region_id: 'r1', status: 'OPEN', inventory: { DIESEL: 100, PETROL: 200, OCTANE: 300 }, capacity: { DIESEL: 1000, PETROL: 1000, OCTANE: 1000 } }],
  regions: [{ id: 'r1', name: 'Dhaka' }], routes: [], events: [],
  supply_arrivals: [{ id: 'a1', depot_id: 'd1', fuel_type: 'DIESEL', quantity: 5000, planned_tick: 12, status: 'SCHEDULED' }],
  metrics: { service_level: 0.92 },
}
const health = { status: 'degraded', components: { api: { status: 'ok' }, llm: { status: 'degraded', detail: 'Deterministic fallback' } } }
function respond(data: unknown) { return Promise.resolve(new Response(JSON.stringify(data))) }
afterEach(() => { vi.unstubAllGlobals() })

describe('Operations dashboard', () => {
  it('shows backend inventory totals, network assets and incoming supply', async () => {
    vi.stubGlobal('fetch', vi.fn((url: string) => respond(url.endsWith('/status') ? health : snapshot)))
    render(<App />)
    expect(await screen.findByRole('heading', { name: 'Station inventory' })).toBeInTheDocument()
    expect(screen.getByText('Mirpur Station')).toBeInTheDocument()
    expect(screen.getByText('6,600')).toBeInTheDocument()
    expect(screen.getByText('92.0%')).toBeInTheDocument()
    expect(screen.getByText('5,000')).toBeInTheDocument()
    expect(await screen.findByText('Deterministic fallback')).toBeInTheDocument()
  })
  it('retains last-known inventory after a failed refresh, then recovers', async () => {
    let failed = false
    vi.stubGlobal('fetch', vi.fn((url: string) => url.endsWith('/status') ? respond(health) : failed ? Promise.reject(new Error('Offline')) : respond(snapshot)))
    render(<App />)
    await screen.findByText('Mirpur Station')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled())
    failed = true
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/last known/i)
    expect(screen.getByText('Mirpur Station')).toBeInTheDocument()
    failed = false
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
  })
  it('shows unavailability instead of invented inventory when startup fails', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('Offline'))))
    render(<App />)
    expect(await screen.findByRole('alert')).toHaveTextContent(/unavailable/i)
    expect(screen.queryByText('6,600')).not.toBeInTheDocument()
  })
  it('marks simulator stale data and handles an empty network', async () => {
    vi.stubGlobal('fetch', vi.fn((url: string) => respond(url.endsWith('/status') ? health : { ...snapshot, stale: true, stations: [], depots: [], supply_arrivals: [] })))
    render(<App />)
    expect(await screen.findByRole('alert')).toHaveTextContent(/stale/i)
    expect(screen.getByText('No stations reported')).toBeInTheDocument()
    expect(screen.getByText('No incoming deliveries')).toBeInTheDocument()
  })
})
