import { AlertTriangle } from '../icons'
import type { SimulatorError } from '../lib/api/errors'

/**
 * States the cause and the recovery path (spec 13). A bare "Something went
 * wrong" tells an operator nothing about whether to retry or escalate, so the
 * simulator's own error code is surfaced.
 */
export function ErrorState({
  error, onRetry,
}: { error: SimulatorError | Error; onRetry?: () => void }) {
  const code = 'code' in error ? String(error.code) : 'ERROR'

  return (
    <div role="alert" className="flex flex-col items-start gap-2 rounded-panel border border-status-critical/40 bg-status-critical/5 px-3.5 py-3">
      <div className="flex items-center gap-2 text-status-critical">
        <AlertTriangle size={14} />
        <span className="text-[13px] font-medium">{error.message}</span>
      </div>
      <div className="mono text-[11px] text-subtle">{code}</div>
      {onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="inline-flex min-h-11 min-w-11 cursor-pointer items-center justify-center rounded-control border border-hairline-strong px-3 text-[12px] font-medium text-ink transition-colors duration-[var(--dur-hover)] hover:bg-surface-raised"
        >
          Retry
        </button>
      ) : null}
    </div>
  )
}
