import type { ReactNode } from 'react'

export interface PanelProps {
  title?: string
  actions?: ReactNode
  children: ReactNode
  /** Dims the panel while still showing the last known content. */
  stale?: boolean
  className?: string
}

/**
 * A bordered surface, not a floating card: hairline border and a small radius,
 * with no drop shadow (spec 3.1). Exactly two elevation levels exist in the
 * application and neither is used here.
 */
export function Panel({ title, actions, children, stale = false, className = '' }: PanelProps) {
  return (
    <section
      data-stale={stale || undefined}
      className={`rounded-panel border border-hairline bg-surface ${stale ? 'opacity-60' : ''} ${className}`}
    >
      {(title || actions) && (
        <header className="flex items-center justify-between gap-3 border-b border-hairline px-4 py-3">
          {title ? (
            <h2 className="text-[15px] font-semibold tracking-tight text-ink">{title}</h2>
          ) : <span />}
          {actions}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  )
}
