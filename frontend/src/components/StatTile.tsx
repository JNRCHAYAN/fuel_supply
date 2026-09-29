import type { ReactNode } from 'react'

export interface StatTileProps {
  label: string
  value: string | number | null
  unit?: string
  hint?: string
  trend?: ReactNode
  tone?: 'default' | 'good' | 'warning' | 'critical'
}

const TONE_TEXT: Record<string, string> = {
  default: 'text-ink',
  good: 'text-status-good',
  warning: 'text-status-warning',
  critical: 'text-status-critical',
}

/**
 * A single headline figure. Per spec 9.6, one number is a stat tile, not a
 * chart of one bar.
 */
export function StatTile({ label, value, unit, hint, trend, tone = 'default' }: StatTileProps) {
  const display = value === null || value === undefined || value === '' ? '—' : value

  return (
    <div className="rounded-panel border border-hairline bg-surface px-3.5 py-3">
      <div className="text-[11px] font-medium uppercase tracking-wide text-subtle">{label}</div>
      <div className="mt-1 flex items-baseline gap-1.5">
        <span className={`mono text-[22px] leading-7 font-semibold ${TONE_TEXT[tone]}`}>{display}</span>
        {unit ? <span className="text-[12px] text-muted">{unit}</span> : null}
      </div>
      {hint ? <div className="mt-0.5 text-[12px] text-muted">{hint}</div> : null}
      {trend ? <div className="mt-1">{trend}</div> : null}
    </div>
  )
}
