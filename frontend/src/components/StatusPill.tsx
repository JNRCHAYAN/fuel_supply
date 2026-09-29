import { CheckCircle, AlertTriangle, XOctagon, Clock } from '../icons'
import type { ReactNode } from 'react'

type Tone = 'good' | 'warning' | 'critical' | 'neutral'

const TONE_CLASS: Record<Tone, string> = {
  good: 'text-status-good border-status-good/40 bg-status-good/10',
  warning: 'text-status-warning border-status-warning/40 bg-status-warning/10',
  critical: 'text-status-critical border-status-critical/40 bg-status-critical/10',
  neutral: 'text-muted border-hairline-strong bg-transparent',
}

const TONE_ICON: Record<Tone, () => ReactNode> = {
  good: () => <CheckCircle size={12} />,
  warning: () => <AlertTriangle size={12} />,
  critical: () => <XOctagon size={12} />,
  neutral: () => <Clock size={12} />,
}

/**
 * Status is never communicated by colour alone (spec 9.3): every pill carries
 * an icon and a text label. CONSTRAINED and IN_TRANSIT are deliberately
 * warning-toned rather than critical — a constrained depot is still shippable,
 * and the colour must not overstate the condition to an operator deciding
 * whether to dispatch.
 */
const STATUS_TONES: Record<string, Tone> = {
  OPEN: 'good', AVAILABLE: 'good', ARRIVED: 'good',
  CONSTRAINED: 'warning', DELAYED: 'warning', IN_TRANSIT: 'warning',
  OUTAGE: 'critical', DISRUPTED: 'critical', FAILED: 'critical',
  PAUSED: 'neutral', PENDING: 'neutral', SCHEDULED: 'neutral',
  CANCELLED: 'neutral', ACTIVE: 'warning', RESOLVED: 'good',
}

function humanize(status: string): string {
  return status
    .toLowerCase()
    .split('_')
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
}

export function StatusPill({
  status, kind, size = 'md',
}: { status: string; kind?: string; size?: 'sm' | 'md' }) {
  // An unrecognised status renders as a neutral pill with its raw label rather
  // than crashing — a new enum value from a future simulator must not blank
  // the screen. Only a status the table knows is humanized; an unknown one is
  // shown verbatim, because inventing a title-cased reading of an enum value
  // this build has never seen would be a guess, not a label.
  const known = STATUS_TONES[status]
  const tone = known ?? 'neutral'
  const label = known ? humanize(status) : status
  const Glyph = TONE_ICON[tone]
  const padding = size === 'sm' ? 'px-1.5 py-0.5 text-[11px]' : 'px-2 py-0.5 text-[11px]'

  return (
    <span
      data-tone={tone}
      data-kind={kind}
      className={`inline-flex items-center gap-1 rounded-pill border font-medium ${TONE_CLASS[tone]} ${padding}`}
    >
      <Glyph />
      {label}
    </span>
  )
}
