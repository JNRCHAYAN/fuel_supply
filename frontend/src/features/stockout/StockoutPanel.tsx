import { useEffect, useMemo, useRef, useState } from 'react'
import { DataTable, type Column } from '../../components/DataTable'
import { EmptyState } from '../../components/EmptyState'
import { Panel } from '../../components/Panel'
import { Skeleton } from '../../components/Skeleton'
import { StatTile } from '../../components/StatTile'
import { RISK_LEVELS, type RiskLevel, type StockoutAssessment } from '../../lib/types/stockout'
import { useStockout } from './useStockout'

/** The dashboard's own figure format, reused so both screens read alike. */
const number = (value: number) => value.toLocaleString('en-US', { maximumFractionDigits: 0 })

const LEVEL_LABELS: Record<RiskLevel, string> = {
  CRITICAL: 'Critical',
  HIGH: 'High',
  MEDIUM: 'Medium',
  LOW: 'Low',
}

/** Each tile hint names its level, so a zero is never read as "no level". */
const LEVEL_HINTS: Record<RiskLevel, string> = {
  CRITICAL: 'CRITICAL · dry now, or dry inside the critical band',
  HIGH: 'HIGH · stockout inside the horizon',
  MEDIUM: 'MEDIUM · below the safety-stock floor',
  LOW: 'LOW · demand covered for the horizon',
}

const LEVEL_TONES: Record<RiskLevel, 'critical' | 'warning' | 'default' | 'good'> = {
  CRITICAL: 'critical',
  HIGH: 'warning',
  MEDIUM: 'default',
  LOW: 'good',
}

// CRITICAL and HIGH share the warning treatment, MEDIUM is deliberately the
// neutral badge and LOW is good. All three treatments already exist in
// dashboard.css — `state-label` on its own is the neutral one — so this panel
// introduces no new class. Keyed by string so a level this build has never seen
// falls back to the neutral badge instead of rendering `state-label undefined`.
const LEVEL_BADGES: Record<string, string> = {
  CRITICAL: 'state-warning',
  HIGH: 'state-warning',
  MEDIUM: '',
  LOW: 'state-good',
}

type RiskFilter = 'all' | RiskLevel

/** `2.5 ticks`, `now` once dry, an em dash when no crossing was projected. */
function formatTicks(ticks: number | null): string {
  if (ticks === null || !Number.isFinite(ticks)) return '—'
  if (ticks === 0) return 'now'
  return `${ticks.toFixed(1)} ticks`
}

function RiskBadge({ level }: { level: RiskLevel }) {
  return <span className={`state-label ${LEVEL_BADGES[level] ?? ''}`}>{level}</span>
}

/**
 * Stockout and shortage intelligence for the current network: how many
 * (station, fuel) rows sit in each risk band, and where every row's inventory is
 * projected to land over the forecast horizon.
 *
 * The hook fetches the whole response once, so the risk filter is applied here,
 * client-side — re-querying the route on every change would make a filter feel
 * like a page load.
 */
export function StockoutPanel() {
  const { data, error, loading, refresh } = useStockout()
  const [risk, setRisk] = useState<RiskFilter>('all')
  const tableRef = useRef<HTMLDivElement | null>(null)

  // `assessments` is typed as an array, but a malformed body from a newer or
  // older backend must render the empty state rather than throw on `.filter`.
  const raw: unknown = data?.assessments
  const assessments = useMemo<StockoutAssessment[]>(
    () => (Array.isArray(raw)
      ? raw.filter((item): item is StockoutAssessment => typeof item === 'object' && item !== null)
      : []),
    [raw],
  )

  const visible = useMemo(
    () => (risk === 'all' ? assessments : assessments.filter(item => item.risk === risk)),
    [assessments, risk],
  )

  // `DataTable` owns its `<tr>` elements, so the basis string — the arithmetic
  // the projection is built from — is attached to each row here, where it covers
  // the whole row on hover, rather than to a single cell. The visually hidden
  // copy in the station cell carries the same text into the accessibility tree,
  // so the numbers on screen can always be traced back to how they were derived.
  useEffect(() => {
    const host = tableRef.current
    if (!host) return
    host.querySelectorAll<HTMLTableRowElement>('tbody tr').forEach(row => {
      const basis = row.querySelector('[data-basis]')?.getAttribute('data-basis')
      if (basis) row.title = basis
    })
  }, [visible])

  if (!data) {
    return error
      ? <div role="alert" className="alert-banner">Stockout projections unavailable. Check the backend and simulator connection, then refresh. No projection has been received.</div>
      : <div role="status" className="loading-state">Loading stockout projections…</div>
  }

  const columns: Column<StockoutAssessment>[] = [
    {
      key: 'station',
      header: 'Station',
      render: row => <div data-basis={row.basis}><strong>{row.station_id}</strong><div className="text-xs text-muted">{row.fuel_type}</div><span className="sr-only">{row.basis}</span></div>,
    },
    { key: 'current', header: 'Current', numeric: true, render: row => number(row.current_inventory) },
    { key: 'demand', header: 'Demand', numeric: true, render: row => number(row.expected_demand) },
    { key: 'incoming', header: 'Incoming', numeric: true, render: row => number(row.incoming_supply) },
    {
      key: 'projected',
      header: 'Projected',
      numeric: true,
      render: row => <span className={row.projected_inventory < 0 ? 'text-status-warning' : undefined}>{number(row.projected_inventory)}</span>,
    },
    {
      key: 'balance',
      header: 'Shortage / surplus',
      render: row => {
        if (row.shortage_amount > 0) return `-${number(row.shortage_amount)}`
        if (row.surplus_amount > 0) return `+${number(row.surplus_amount)}`
        return '—'
      },
    },
    { key: 'stockout', header: 'Stockout', render: row => formatTicks(row.ticks_until_stockout) },
    { key: 'risk', header: 'Risk', render: row => <RiskBadge level={row.risk} /> },
  ]

  const horizon = data.horizon_ticks
  const filterLabel = risk === 'all' ? 'All levels' : LEVEL_LABELS[risk]

  return <div>
    {error && <div role="alert" className="alert-banner">Stale data — showing the last successful projection. Live updates will resume when the connection recovers.</div>}
    <div className="section-toolbar">
      <h2>Stockout risk</h2>
      <div className="heading-actions">
        <label>Risk <select value={risk} onChange={event => setRisk(event.target.value as RiskFilter)}><option value="all">All levels</option>{RISK_LEVELS.map(level => <option key={level} value={level}>{LEVEL_LABELS[level]}</option>)}</select></label>
        <button className="primary-button" disabled={loading} onClick={refresh}>{loading ? 'Refreshing…' : 'Refresh'}</button>
      </div>
    </div>
    <div className="stats-grid">
      {RISK_LEVELS.map(level => <StatTile key={level} label={LEVEL_LABELS[level]} value={data.summary?.[level] ?? 0} hint={LEVEL_HINTS[level]} tone={LEVEL_TONES[level]} />)}
    </div>
    <Panel title="Projected inventory by station">
      <p className="text-xs text-muted mb-3">
        {Number.isFinite(horizon) ? `Projected ${horizon} ticks ahead · ` : ''}
        Showing {visible.length} of {assessments.length} rows · {filterLabel}
      </p>
      {visible.length > 0
        ? <div ref={tableRef}><DataTable columns={columns} rows={visible} getRowKey={row => `${row.station_id}:${row.fuel_type}`} minWidth={840} caption="Projected inventory, shortage and stockout timing by station and fuel" emptyMessage="No stockout assessments reported" /></div>
        : loading
          ? <Skeleton variant="line" count={6} />
          : <EmptyState title="No stations match this risk level" description={risk === 'all' ? 'The stockout engine reported no station-level rows for this network.' : `No station currently sits at ${filterLabel} risk. Choose "All levels" to see the full picture.`} />}
    </Panel>
  </div>
}
