import { useFreshness } from '../lib/hooks/useFreshness'

/**
 * States how old the data on screen is (Guide 10.2). A stale flag is shown
 * explicitly rather than implied by a colour shift, because an operator
 * reading a number needs to know whether to trust it.
 */
export function FreshnessBadge({
  receivedAt, stale,
}: { receivedAt: number | null; stale: boolean }) {
  const { label } = useFreshness(receivedAt)

  if (receivedAt === null) {
    return <span className="text-[11px] text-subtle">No data</span>
  }

  if (stale) {
    return (
      <span className="tnum inline-flex items-center gap-1 text-[11px] font-medium text-status-warning">
        Stale · {label}
      </span>
    )
  }

  return <span className="tnum text-[11px] text-subtle">Updated {label}</span>
}
