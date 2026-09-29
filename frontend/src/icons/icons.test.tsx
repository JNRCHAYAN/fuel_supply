import { render } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import * as icons from './index'

const NAMED = [
  'CheckCircle', 'AlertTriangle', 'XOctagon', 'Clock', 'Fuel', 'Depot', 'Station',
  'Route', 'Activity', 'Shield', 'Gauge', 'Scroll', 'Play', 'Pause', 'StepForward',
  'RotateCcw', 'Sun', 'Moon', 'Wifi', 'WifiOff', 'ChevronRight', 'Search', 'Plus',
  'X', 'ArrowRight',
] as const

describe('icon set', () => {
  it('exports every named glyph', () => {
    for (const name of NAMED) {
      expect(icons[name], `missing icon: ${name}`).toBeDefined()
    }
  })

  it('renders an svg that inherits colour rather than hardcoding it', () => {
    const { container } = render(<icons.Fuel />)
    const svg = container.querySelector('svg')!
    expect(svg).toBeInTheDocument()
    expect(svg.getAttribute('stroke')).toBe('currentColor')
  })

  it('uses a consistent 24px viewBox and stroke width across the set', () => {
    for (const name of NAMED) {
      const Glyph = icons[name]
      const { container } = render(<Glyph />)
      const svg = container.querySelector('svg')!
      expect(svg.getAttribute('viewBox')).toBe('0 0 24 24')
      expect(svg.getAttribute('stroke-width')).toBe('1.5')
    }
  })

  it('marks decorative icons as hidden from assistive technology', () => {
    const { container } = render(<icons.Fuel />)
    expect(container.querySelector('svg')).toHaveAttribute('aria-hidden', 'true')
  })
})
