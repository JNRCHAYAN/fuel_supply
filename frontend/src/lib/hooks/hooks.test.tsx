import { render, renderHook, screen, waitFor, act } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { ApiProvider } from './ApiProvider'
import { useSimulatorQuery } from './useSimulatorQuery'
import { useFreshness } from './useFreshness'
import { useHealth } from './useHealth'
import { createMockClient } from '../api/mock/mockClient'
import { SimulatorError } from '../api/errors'
import type { Depot } from '../types/simulator'

function DepotList() {
  const { data, loading, error, stale, refresh } = useSimulatorQuery<Depot[]>(
    'depots',
    (client) => client.getDepots(),
  )
  if (loading) return <p>loading</p>
  if (error) return <p role="alert">{error.message}</p>
  return (
    <div>
      <span data-testid="stale">{String(stale)}</span>
      <span data-testid="count">{data?.length ?? 0}</span>
      <ul>{data?.map((d) => <li key={d.id}>{d.name}</li>)}</ul>
      <button onClick={refresh}>refresh</button>
    </div>
  )
}

function wrap(client: ReturnType<typeof createMockClient>) {
  return render(<ApiProvider client={client}><DepotList /></ApiProvider>)
}

describe('useSimulatorQuery', () => {
  it('loads data and clears the loading flag', async () => {
    wrap(createMockClient({ seed: 12345 }))
    expect(screen.getByText('loading')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('count')).toHaveTextContent('2'))
  })

  it('surfaces a fault as an error rather than an empty list', async () => {
    const client = createMockClient({ seed: 12345 })
    client.world.injectFault({ type: 'unavailable', duration_seconds: 60 })
    wrap(client)
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())
  })

  it('reports staleness from the response', async () => {
    const client = createMockClient({ seed: 12345 })
    client.world.injectFault({ type: 'stale_data', duration_seconds: 60 })
    wrap(client)
    await waitFor(() => expect(screen.getByTestId('stale')).toHaveTextContent('true'))
  })

  it('refetches on demand', async () => {
    const client = createMockClient({ seed: 12345 })
    const spy = vi.spyOn(client, 'getDepots')
    wrap(client)
    await waitFor(() => expect(screen.getByTestId('count')).toHaveTextContent('2'))
    await act(async () => { screen.getByRole('button', { name: 'refresh' }).click() })
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(2))
  })

  it('does not render stale data as fresh after an error', async () => {
    const client = createMockClient({ seed: 12345 })
    wrap(client)
    await waitFor(() => expect(screen.getByTestId('count')).toHaveTextContent('2'))
    client.world.injectFault({ type: 'unavailable', duration_seconds: 60 })
    await act(async () => { screen.getByRole('button', { name: 'refresh' }).click() })
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())
  })
})

describe('useFreshness', () => {
  it('reports no age and no label when there is no data', () => {
    const { result } = renderHook(() => useFreshness(null))
    expect(result.current.ageMs).toBe(0)
    expect(result.current.label).toBe('—')
  })

  it('labels the age by magnitude', () => {
    vi.useFakeTimers()
    try {
      const now = new Date('2026-01-01T12:00:00.000Z')
      vi.setSystemTime(now)

      const cases: Array<[number, string]> = [
        [0, 'just now'],
        [4_999, 'just now'],
        [5_000, '5s ago'],
        [59_000, '59s ago'],
        [60_000, '1m ago'],
        [3_599_000, '59m ago'],
        [3_600_000, '1h ago'],
        [7_200_000, '2h ago'],
      ]

      for (const [age, expected] of cases) {
        const { result, unmount } = renderHook(() =>
          useFreshness(now.getTime() - age, 1000),
        )
        expect(result.current.ageMs).toBe(age)
        expect(result.current.label).toBe(expected)
        unmount()
      }
    } finally {
      vi.useRealTimers()
    }
  })

  it('advances the age as time passes', () => {
    vi.useFakeTimers()
    try {
      vi.setSystemTime(new Date('2026-01-01T00:00:10.000Z'))
      const receivedAt = Date.now()
      const { result } = renderHook(() => useFreshness(receivedAt, 1000))
      expect(result.current.label).toBe('just now')

      act(() => { vi.advanceTimersByTime(6_000) })

      expect(result.current.ageMs).toBe(6_000)
      expect(result.current.label).toBe('6s ago')
    } finally {
      vi.useRealTimers()
    }
  })
})

function HealthProbe() {
  const { state, health, error } = useHealth()
  return (
    <div>
      <span data-testid="state">{state}</span>
      <span data-testid="health">{health?.status ?? 'none'}</span>
      <span data-testid="health-error">{error?.message ?? 'none'}</span>
    </div>
  )
}

function renderHealth(client: ReturnType<typeof createMockClient>) {
  return render(<ApiProvider client={client}><HealthProbe /></ApiProvider>)
}

describe('useHealth', () => {
  it('reports normal when the data route answers', async () => {
    renderHealth(createMockClient({ seed: 12345 }))
    await waitFor(() => expect(screen.getByTestId('state')).toHaveTextContent('normal'))
    expect(screen.getByTestId('health')).toHaveTextContent('ok')
  })

  it('reports degraded when health is up but the data route is faulted', async () => {
    // Guide 7.10 exempts /v1/health from fault injection, so health alone still
    // says "ok" here. Only the paired data probe reveals the fault.
    const client = createMockClient({ seed: 12345 })
    client.world.injectFault({ type: 'unavailable', duration_seconds: 60 })
    renderHealth(client)
    await waitFor(() => expect(screen.getByTestId('state')).toHaveTextContent('degraded'))
    expect(screen.getByTestId('health')).toHaveTextContent('ok')
  })

  it('reports offline when health itself fails', async () => {
    const client = createMockClient({ seed: 12345 })
    vi.spyOn(client, 'getHealth').mockRejectedValue(
      new SimulatorError(503, 'FAULT_INJECTED', 'Simulator API temporarily unavailable.'),
    )
    renderHealth(client)
    await waitFor(() => expect(screen.getByTestId('state')).toHaveTextContent('offline'))
    expect(screen.getByTestId('health-error')).toHaveTextContent('temporarily unavailable')
  })
})
