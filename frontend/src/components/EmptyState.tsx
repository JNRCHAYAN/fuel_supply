import type { ReactNode } from 'react'

export function EmptyState({
  title, description, action, icon,
}: { title: string; description?: string; action?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="flex flex-col items-start gap-2 px-1 py-8">
      {icon ? <div className="text-subtle">{icon}</div> : null}
      <p className="text-[14px] font-medium text-ink">{title}</p>
      {description ? <p className="max-w-prose text-[13px] text-muted">{description}</p> : null}
      {action ? <div className="pt-1">{action}</div> : null}
    </div>
  )
}
