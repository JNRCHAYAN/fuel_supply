import { useEffect, useState } from 'react'

export interface Freshness {
  ageMs: number
  label: string
}

function label(ageMs: number): string {
  if (ageMs < 5_000) return 'just now'
  if (ageMs < 60_000) return `${Math.floor(ageMs / 1000)}s ago`
  if (ageMs < 3_600_000) return `${Math.floor(ageMs / 60_000)}m ago`
  return `${Math.floor(ageMs / 3_600_000)}h ago`
}

/**
 * Guides 10.2: an operator must be able to tell how old a figure is. A screen
 * that cannot state its data's age cannot claim to be current.
 */
export function useFreshness(receivedAt: number | null, tickMs = 1000): Freshness {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (receivedAt === null) return
    const timer = setInterval(() => setNow(Date.now()), tickMs)
    return () => clearInterval(timer)
  }, [receivedAt, tickMs])

  const ageMs = receivedAt === null ? 0 : Math.max(0, now - receivedAt)
  return { ageMs, label: receivedAt === null ? '—' : label(ageMs) }
}
