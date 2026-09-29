import { useState } from 'react'
import { Panel } from '../components/Panel'
import { StatTile } from '../components/StatTile'
import { DataTable, type Column } from '../components/DataTable'
import { ThemeProvider, useTheme } from '../design/theme'
import { FUEL_TYPES, type Depot, type Station } from '../lib/types/simulator'
import { useDashboard } from '../features/dashboard/useDashboard'
import { StockoutPanel } from '../features/stockout/StockoutPanel'

const number = (value: number) => value.toLocaleString('en-US', { maximumFractionDigits: 0 })

function Dashboard() {
  const { snapshot, health, error, healthError, loading, refresh, age, stale } = useDashboard()
  const { theme, toggle } = useTheme()
  const [region, setRegion] = useState('all')
  const regions = snapshot?.regions ?? []
  const selectedRegion = regions.some(item => item.id === region) ? region : 'all'
  const depots = (snapshot?.depots ?? []).filter(item => selectedRegion === 'all' || item.region_id === selectedRegion)
  const stations = (snapshot?.stations ?? []).filter(item => selectedRegion === 'all' || item.region_id === selectedRegion)
  const assets = [...depots, ...stations]
  const inventory = assets.reduce((total, item) => total + FUEL_TYPES.reduce((sum, fuel) => sum + item.inventory[fuel], 0), 0)
  const arrivals = (snapshot?.supply_arrivals ?? []).filter(item => item.status !== 'ARRIVED' && depots.some(depot => depot.id === item.depot_id)).sort((a, b) => a.planned_tick - b.planned_tick)
  const columns: Column<Depot | Station>[] = [
    { key: 'name', header: 'Location', render: item => <div><strong>{item.name}</strong><div className="text-xs text-muted">{regions.find(r => r.id === item.region_id)?.name ?? item.region_id}</div></div> },
    ...FUEL_TYPES.map(fuel => ({ key: fuel, header: `${fuel} / L`, numeric: true, render: (item: Depot | Station) => <div><span>{number(item.inventory[fuel])}</span><meter className={`fuel-meter ${fuel.toLowerCase()}`} aria-label={`${item.name} ${fuel} inventory`} min={0} max={Math.max(1, item.capacity[fuel])} value={item.inventory[fuel]} /></div> })),
    { key: 'status', header: 'Status', render: item => <span className={`state-label ${item.status === 'OPEN' ? 'state-good' : 'state-warning'}`}>{item.status}</span> },
  ]

  return <div className="dashboard-shell">
    <aside className="sidebar">
      <a className="brand" href="#overview"><span className="brand-mark">F</span><span>FUEL OPS<small>INTELLIGENCE PLATFORM</small></span></a>
      <div className="sidebar-label">WORKSPACE</div>
      <nav aria-label="Dashboard sections"><a className="selected" href="#overview">Overview <span>01</span></a><a href="#inventory">Network inventory</a><a href="#supply">Incoming supply</a><a href="#stockout">Stockout risk</a><a href="#health">Service health</a></nav>
      <div className="sidebar-note"><span className="state-label">SIMULATED NETWORK</span><p>Bangladesh fuel operations<br />BUP CSE Fest 2026</p><p>Feature 1 · Operations dashboard</p></div>
    </aside>
    <div className="workspace">
      <header className="topbar"><span>Operations / <strong>Overview</strong></span><button onClick={toggle}>{theme === 'dark' ? 'Light' : 'Dark'} theme</button></header>
      <main id="overview">
        <div className="page-heading"><div><p className="eyebrow">NETWORK COMMAND CENTER</p><h1>Operations dashboard</h1><p className="text-muted">Fuel Supply Intelligence &amp; Resilience Platform</p></div><div className="heading-actions"><span className="state-label">SIMULATED DATA</span><button className="primary-button" disabled={loading} onClick={() => void refresh()}>{loading ? 'Refreshing…' : 'Refresh'}</button></div></div>
        <div className="telemetry"><span className={stale || error ? 'text-status-warning' : 'text-muted'}>{snapshot ? stale ? 'Stale snapshot' : 'Live backend connection' : 'Awaiting network data'}</span><span>Tick <strong>{snapshot?.tick ?? '—'}</strong></span><span>{snapshot?.status ?? 'UNKNOWN'}</span><span>Age: {age === null ? '—' : `${Math.floor(age)}s`}</span><span>Auto-refresh · 5s</span><span>{snapshot?.sim_time ? `${new Date(snapshot.sim_time).toLocaleString('en-GB', { timeZone: 'UTC' })} UTC` : 'Simulation time unavailable'}</span></div>
        {(error || stale) && <div role="alert" className="alert-banner">{snapshot ? 'Stale data — showing the last known network state. Live updates will resume when the connection recovers.' : 'Network unavailable. Check the backend and simulator connection, then refresh. No inventory data has been received.'}</div>}
        {!snapshot && !error && <div role="status" className="loading-state">Loading the fuel network…</div>}
        {snapshot && <>
          <div className="section-toolbar"><h2>Network at a glance</h2><label>Region <select value={selectedRegion} onChange={event => setRegion(event.target.value)}><option value="all">All regions</option>{regions.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label></div>
          <div className="stats-grid">
            <StatTile label="Available inventory" value={number(inventory)} unit="L" hint={`${depots.length} depots · ${stations.length} stations`} />
            <StatTile label="Stations operating" value={`${stations.filter(item => item.status === 'OPEN').length} / ${stations.length}`} hint="Reported station status" />
            <StatTile label="Network service level" value={snapshot.metrics.service_level == null ? null : `${(snapshot.metrics.service_level * 100).toFixed(1)}%`} hint="Cumulative · all regions" />
            <StatTile label="Incoming deliveries" value={arrivals.length} hint={`${number(arrivals.reduce((total, item) => total + item.quantity, 0))} L scheduled or delayed`} />
          </div>
          <div id="inventory" className="inventory-stack">
            <Panel title="Station inventory" actions={<span className="text-xs text-muted">Liters / tank capacity</span>} stale={stale}><DataTable columns={columns} rows={stations} getRowKey={item => item.id} emptyMessage="No stations reported" caption="Station inventories by fuel" /></Panel>
            <Panel title="Depot inventory" stale={stale}><DataTable columns={columns} rows={depots} getRowKey={item => item.id} emptyMessage="No depots reported" caption="Depot inventories by fuel" /></Panel>
          </div>
          <div className="lower-grid">
            <div id="supply"><Panel title="Incoming supply" stale={stale}><DataTable rows={arrivals} getRowKey={item => item.id} emptyMessage="No incoming deliveries" minWidth={460} columns={[
              { key: 'depot', header: 'Destination', render: item => depots.find(depot => depot.id === item.depot_id)?.name ?? item.depot_id },
              { key: 'fuel', header: 'Fuel', render: item => item.fuel_type },
              { key: 'quantity', header: 'Liters', numeric: true, render: item => number(item.quantity) },
              { key: 'tick', header: 'Planned tick', numeric: true, render: item => item.planned_tick },
              { key: 'status', header: 'Status', render: item => <span className="state-label">{item.status}</span> },
            ]} /></Panel></div>
            <Panel title="Network disruptions" stale={stale}><p className="text-xs text-muted mb-3">All regions · active simulator events</p>{snapshot.events.filter(item => item.status === 'ACTIVE').length ? snapshot.events.filter(item => item.status === 'ACTIVE').map(item => <div className="event-row" key={item.id}><strong>{item.type.replaceAll('_', ' ')}</strong><span>Ticks {item.start_tick}–{item.end_tick}</span></div>) : <p className="text-muted py-6">No active disruptions reported</p>}</Panel>
          </div>
        </>}
        <div id="stockout"><StockoutPanel /></div>
        <section id="health" className="health-section"><h2>Service health</h2>{healthError ? <p className="text-status-warning" role="status">Service health unavailable. Retrying automatically.</p> : health ? <div className="health-grid">{Object.entries(health.components).map(([name, component]) => <div className="health-item" key={name}><div><strong>{name.replaceAll('_', ' ')}</strong><span className={`state-label ${component.status === 'ok' ? 'state-good' : 'state-warning'}`}>{component.status}</span></div><p>{component.detail ?? 'No additional detail'}</p></div>)}</div> : <p className="text-muted">Checking services…</p>}</section>
        <footer>All values represent the supplied BUP simulator. No real fuel infrastructure is connected.</footer>
      </main>
    </div>
  </div>
}

export function App() { return <ThemeProvider><Dashboard /></ThemeProvider> }
