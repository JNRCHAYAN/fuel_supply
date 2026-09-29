import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, it, expect, vi } from 'vitest'
import { Panel } from './Panel'
import { StatusPill } from './StatusPill'
import { StatTile } from './StatTile'
import { DataTable, type Column } from './DataTable'
import { EmptyState } from './EmptyState'
import { ErrorState } from './ErrorState'
import { FreshnessBadge } from './FreshnessBadge'
import { SimulatorError } from '../lib/api/errors'

describe('Panel', () => {
  it('renders a heading and its content', () => {
    render(<Panel title="Depot inventory"><p>body</p></Panel>)
    expect(screen.getByRole('heading', { name: 'Depot inventory' })).toBeInTheDocument()
    expect(screen.getByText('body')).toBeInTheDocument()
  })
})

describe('StatusPill', () => {
  it('carries a text label as well as colour, never colour alone', () => {
    render(<StatusPill status="OPEN" kind="depot" />)
    expect(screen.getByText('Open')).toBeInTheDocument()
  })

  it('renders a distinguishable glyph per status', () => {
    const { container: open } = render(<StatusPill status="OPEN" kind="depot" />)
    const { container: outage } = render(<StatusPill status="OUTAGE" kind="station" />)
    expect(open.querySelector('svg')!.innerHTML).not.toBe(outage.querySelector('svg')!.innerHTML)
  })

  it('renders a constrained depot with the warning tone, not the critical one', () => {
    const { container } = render(<StatusPill status="CONSTRAINED" kind="depot" />)
    // CONSTRAINED is still shippable; the colour must not overstate it.
    expect(container.firstChild).toHaveAttribute('data-tone', 'warning')
  })

  it('renders a fallback for an unrecognised status rather than crashing', () => {
    render(<StatusPill status={'SOMETHING_NEW' as 'OPEN'} kind="depot" />)
    expect(screen.getByText('SOMETHING_NEW')).toBeInTheDocument()
  })
})

describe('StatTile', () => {
  it('renders a labelled value with tabular figures', () => {
    render(<StatTile label="Service level" value="98%" />)
    expect(screen.getByText('Service level')).toBeInTheDocument()
    expect(screen.getByText('98%')).toHaveClass('mono')
  })

  it('renders a placeholder when the value is unavailable', () => {
    render(<StatTile label="Unmet demand" value={null} />)
    expect(screen.getByText('—')).toBeInTheDocument()
  })
})

describe('DataTable', () => {
  interface Row { id: string; qty: number }

  const columns: Array<Column<Row>> = [
    { key: 'id', header: 'ID', render: (r: Row) => r.id },
    { key: 'qty', header: 'Quantity', numeric: true, render: (r: Row) => String(r.qty) },
  ]

  it('renders a header row and one row per record', () => {
    render(<DataTable columns={columns} rows={[{ id: 'a', qty: 1 }, { id: 'b', qty: 2 }]} getRowKey={(r) => r.id} caption="Loads" />)
    expect(screen.getAllByRole('columnheader')).toHaveLength(2)
    expect(screen.getAllByRole('row')).toHaveLength(3) // header + 2
  })

  it('renders an empty state instead of an empty table body', () => {
    render(<DataTable columns={columns} rows={[]} getRowKey={(r: Row) => r.id} emptyMessage="No allocations yet" />)
    expect(screen.getByText('No allocations yet')).toBeInTheDocument()
  })

  it('does not crash on an undefined rows prop', () => {
    render(<DataTable columns={columns} rows={undefined} getRowKey={(r: Row) => r.id} emptyMessage="Nothing" />)
    expect(screen.getByText('Nothing')).toBeInTheDocument()
  })
})

describe('EmptyState', () => {
  it('explains the emptiness rather than leaving a blank panel', () => {
    render(<EmptyState title="No alerts" description="No station is projected to stock out." />)
    expect(screen.getByText('No alerts')).toBeInTheDocument()
    expect(screen.getByText('No station is projected to stock out.')).toBeInTheDocument()
  })
})

describe('ErrorState', () => {
  it('states the cause and offers a retry', async () => {
    const onRetry = vi.fn()
    render(<ErrorState error={new SimulatorError(409, 'ROUTE_DISRUPTED', 'Route is DISRUPTED.')} onRetry={onRetry} />)
    expect(screen.getByText('Route is DISRUPTED.')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /retry/i }))
    expect(onRetry).toHaveBeenCalled()
  })

  it('announces itself to assistive technology', () => {
    render(<ErrorState error={new SimulatorError(503, 'FAULT_INJECTED', 'Unavailable.')} />)
    expect(screen.getByRole('alert')).toBeInTheDocument()
  })
})

describe('FreshnessBadge', () => {
  it('reports recent data as current', () => {
    render(<FreshnessBadge receivedAt={Date.now()} stale={false} />)
    expect(screen.getByText(/just now/i)).toBeInTheDocument()
  })

  it('flags stale data explicitly', () => {
    render(<FreshnessBadge receivedAt={Date.now() - 60_000} stale />)
    expect(screen.getByText(/stale/i)).toBeInTheDocument()
  })

  it('renders without data rather than claiming freshness', () => {
    render(<FreshnessBadge receivedAt={null} stale={false} />)
    expect(screen.queryByText(/just now/i)).not.toBeInTheDocument()
  })
})
