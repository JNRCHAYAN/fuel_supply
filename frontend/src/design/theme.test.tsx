import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, it, expect, beforeEach } from 'vitest'
import { ThemeProvider, useTheme } from './theme'

function Probe() {
  const { theme, toggle } = useTheme()
  return (
    <div>
      <span data-testid="theme">{theme}</span>
      <button onClick={toggle}>toggle</button>
    </div>
  )
}

describe('ThemeProvider', () => {
  beforeEach(() => {
    window.localStorage.clear()
    document.documentElement.removeAttribute('data-theme')
  })

  it('defaults to dark when no preference is stored', () => {
    render(<ThemeProvider><Probe /></ThemeProvider>)
    expect(screen.getByTestId('theme')).toHaveTextContent('dark')
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
  })

  it('restores a stored preference', () => {
    window.localStorage.setItem('fuel.theme', 'light')
    render(<ThemeProvider><Probe /></ThemeProvider>)
    expect(screen.getByTestId('theme')).toHaveTextContent('light')
  })

  it('toggles and persists the new theme', async () => {
    render(<ThemeProvider><Probe /></ThemeProvider>)
    await userEvent.click(screen.getByRole('button', { name: 'toggle' }))
    expect(screen.getByTestId('theme')).toHaveTextContent('light')
    expect(window.localStorage.getItem('fuel.theme')).toBe('light')
    expect(document.documentElement).toHaveAttribute('data-theme', 'light')
  })

  it('ignores a corrupt stored value rather than rendering an invalid theme', () => {
    window.localStorage.setItem('fuel.theme', 'chartreuse')
    render(<ThemeProvider><Probe /></ThemeProvider>)
    expect(screen.getByTestId('theme')).toHaveTextContent('dark')
  })
})
