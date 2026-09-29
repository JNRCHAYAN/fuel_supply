import type { Config } from 'tailwindcss'

const token = (name: string) => `var(--${name})`

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: token('bg'),
        surface: token('surface'),
        'surface-raised': token('surface-raised'),
        hairline: token('border'),
        'hairline-strong': token('border-strong'),
        ink: token('text'),
        muted: token('text-muted'),
        subtle: token('text-subtle'),
        primary: token('primary'),
        'on-primary': token('on-primary'),
        accent: token('accent'),
        'status-good': token('status-good'),
        'status-warning': token('status-warning'),
        'status-serious': token('status-serious'),
        'status-critical': token('status-critical'),
        'series-diesel': token('series-diesel'),
        'series-petrol': token('series-petrol'),
        'series-octane': token('series-octane'),
      },
      borderColor: {
        DEFAULT: token('border'),
      },
      borderRadius: {
        control: token('radius-control'),
        panel: token('radius-panel'),
        pill: token('radius-pill'),
      },
      fontFamily: {
        sans: ['Fira Sans', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
        mono: ['Fira Code', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      zIndex: {
        content: '0', sticky: '10', dropdown: '20',
        overlay: '40', modal: '100', toast: '1000',
      },
    },
  },
  plugins: [],
} satisfies Config
