import { describe, it, expect } from 'vitest'
import {
  formatLiters, formatPercent, formatTick, formatSimTime, formatHours, formatDelta,
} from './format'

describe('formatLiters', () => {
  it('uses locale grouping and a litre suffix', () => {
    expect(formatLiters(60000)).toBe('60,000 L')
  })

  it('compacts large volumes with an L suffix', () => {
    expect(formatLiters(1250000, { compact: true })).toBe('1.3M L')
    expect(formatLiters(15000, { compact: true })).toBe('15.0k L')
  })

  it('renders zero and small values without a unit prefix', () => {
    expect(formatLiters(0)).toBe('0 L')
    expect(formatLiters(7.4)).toBe('7 L')
  })

  it('does not render negative zero', () => {
    expect(formatLiters(-0)).toBe('0 L')
  })

  it('renders a non-finite value as an em dash rather than NaN', () => {
    expect(formatLiters(Number.NaN)).toBe('—')
    expect(formatLiters(Number.POSITIVE_INFINITY)).toBe('—')
  })
})

describe('formatPercent', () => {
  it('renders a fraction as a whole percentage by default', () => {
    expect(formatPercent(0.981408)).toBe('98%')
  })

  it('honours a decimal precision', () => {
    expect(formatPercent(0.981408, 1)).toBe('98.1%')
  })

  it('handles the extremes', () => {
    expect(formatPercent(0)).toBe('0%')
    expect(formatPercent(1)).toBe('100%')
  })

  it('renders a non-finite value as an em dash', () => {
    expect(formatPercent(Number.NaN)).toBe('—')
  })
})

describe('formatTick', () => {
  it('labels a tick with its simulated clock time', () => {
    // Tick 0 is midnight; 15 simulated minutes per tick.
    expect(formatTick(0, 15)).toBe('T0 · 00:00')
    expect(formatTick(4, 15)).toBe('T4 · 01:00')
  })

  it('wraps past midnight', () => {
    expect(formatTick(96, 15)).toBe('T96 · 00:00')
  })
})

describe('formatSimTime', () => {
  it('renders an ISO timestamp as a short clock time', () => {
    expect(formatSimTime('2026-01-01T03:00:00+00:00')).toBe('03:00')
  })

  it('renders an unparseable value as an em dash', () => {
    expect(formatSimTime('not-a-date')).toBe('—')
  })
})

describe('formatHours', () => {
  it('renders sub-hour durations in minutes', () => {
    expect(formatHours(0.5)).toBe('30 min')
  })

  it('renders hours with one decimal', () => {
    expect(formatHours(6.2)).toBe('6.2 h')
  })

  it('renders a non-finite value as an em dash', () => {
    expect(formatHours(Number.NaN)).toBe('—')
  })
})

describe('formatDelta', () => {
  it('marks an increase with a plus sign', () => {
    expect(formatDelta(0.72, 0.19)).toBe('72% → 19%')
  })
})
