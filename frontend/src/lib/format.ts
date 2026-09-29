const EM_DASH = '—'

/**
 * Every formatter returns an em dash for a non-finite input rather than the
 * string "NaN". A missing measurement must read as missing, not as a value.
 */
function finite(value: number): boolean {
  return Number.isFinite(value)
}

export function formatLiters(value: number, options: { compact?: boolean } = {}): string {
  if (!finite(value)) return EM_DASH
  const normalized = value === 0 ? 0 : value // collapses -0
  if (options.compact) {
    const abs = Math.abs(normalized)
    if (abs >= 1_000_000) return `${(normalized / 1_000_000).toFixed(1)}M L`
    if (abs >= 1_000) return `${(normalized / 1_000).toFixed(1)}k L`
  }
  return `${Math.round(normalized).toLocaleString('en-US')} L`
}

export function formatPercent(fraction: number, decimals = 0): string {
  if (!finite(fraction)) return EM_DASH
  return `${(fraction * 100).toFixed(decimals)}%`
}

export function tickToSimDate(tick: number, tickMinutes: number): Date {
  return new Date(new Date('2026-01-01T00:00:00.000Z').getTime() + tick * tickMinutes * 60_000)
}

function clockTime(date: Date): string {
  const hh = String(date.getUTCHours()).padStart(2, '0')
  const mm = String(date.getUTCMinutes()).padStart(2, '0')
  return `${hh}:${mm}`
}

export function formatTick(tick: number, tickMinutes: number): string {
  if (!finite(tick)) return EM_DASH
  return `T${tick} · ${clockTime(tickToSimDate(tick, tickMinutes))}`
}

export function formatSimTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return EM_DASH
  return clockTime(date)
}

export function formatHours(hours: number): string {
  if (!finite(hours)) return EM_DASH
  if (Math.abs(hours) < 1) return `${Math.round(hours * 60)} min`
  return `${hours.toFixed(1)} h`
}

/** Renders a before/after pair, as used by the decision-impact card. */
export function formatDelta(before: number, after: number, decimals = 0): string {
  return `${formatPercent(before, decimals)} → ${formatPercent(after, decimals)}`
}
