# Fuel Supply Platform — Frontend Plan 1: Foundation & Live Dashboard

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the frontend foundation — design tokens, the simulator contract, a deterministic in-browser world engine, the swappable client, the app shell, shared primitives, and a live Operations Dashboard.

**Architecture:** A React SPA whose entire data access goes through one `SimulatorClient` interface. Two implementations sit behind it: a deterministic in-browser world engine (default) and a real HTTP client for the published simulator. Components never import fixtures. Every response carries its own freshness, so the staleness requirement from Guide §10 is structural rather than bolted on.

**Tech Stack:** React 18, Vite 5, TypeScript 5, Tailwind CSS 3.4, React Router 6, Vitest 1, Testing Library, custom inline SVG charts (no chart library).

**Spec:** `docs/superpowers/specs/2026-09-29-fuel-supply-frontend-design.md`

**Plans 2 and 3** cover the remaining screens. This plan must leave a running app before either begins.

## Global Constraints

- **No backend code.** Frontend only. The mock is the data layer for this phase.
- **Layering rule (spec §4.2):** `features/**` may import from `lib/api` but **never** `lib/api/mock` or fixtures. Enforced by ESLint `no-restricted-imports`, not by convention.
- **Zero raw hex in components** (spec §9.2). All colour comes from CSS custom properties via Tailwind tokens.
- **No emoji as icons** (spec §3.1). SVG only, one family, one stroke width.
- **Banned outright** (spec §3.1): indigo/violet gradients, gradient heading text, `rounded-2xl` on every surface, drop shadows on cards, decorative backdrop-blur, centred hero + three feature cards.
- **Status colours are theme-invariant** and fixed: good `#0CA30C`, warning `#FAB219`, serious `#EC835A`, critical `#D03B3B` (spec §9.2).
- **Fuel series ramp, fixed per fuel** (spec §9.4): DIESEL `#2A78D6`/`#3987E5`, PETROL `#EB6834`/`#D95926`, OCTANE `#1BAF7A`/`#199E70` (light/dark).
- **Every numeric value** uses `font-variant-numeric: tabular-nums`.
- **No dual-axis charts** (spec §9.6). Ever.
- **Status is never colour alone** — colour + icon + text label, always.
- **Contrast:** ≥4.5:1 body text, ≥3:1 large text and UI glyphs, verified per theme.
- **Breakpoints:** 375 / 768 / 1024 / 1440, mobile-first.
- **Touch targets** ≥44×44px.
- **`prefers-reduced-motion`** fully honoured; live charts freeze rather than slow.
- Simulator world values come from Guide §8 and are copied **verbatim**, not approximated.

## Review Focus

The spec is a vision document; its silence on an input is not permission for that input to break the program. These are the failure modes most likely to bite, and each has a test pinned to the task that owns the code:

1. **Exactly-at-limit allocation quantities.** `quantity == route.max_shipment` and `quantity == depot.inventory[fuel]` must **succeed**. The Guide §5.2 magnitude checks exclude the boundary — route max, dispatch capacity and destination headroom fail on `>`, depot inventory fails on `inventory < quantity`. An off-by-one that turns one of those into `>=` silently blocks legal dispatches, and the failure is invisible until an operator can't move fuel that is plainly available. *(Task 8)*
2. **The three error envelopes.** `{"detail":{...}}`, `{"error":{...}}` and FastAPI's default `{"detail":[...]}` are three different shapes (`Guide §9`). A single unwrapping helper gets at least one wrong and turns a clear 409 into `undefined`. *(Task 13)*
3. **One-shot versus reversing events.** `shipment_delay` and `supply_shortfall` **do not auto-undo** on resolve; the other four do (`Guide §7.8`). Treating all six as reversible corrupts world state permanently. *(Task 9)*
4. **Zero and negative demand.** A station with `demand_multiplier` at its 0.01 floor, or a divisor reaching zero in event reversal, must not produce `NaN` or divide-by-zero. `NaN` propagates silently through every downstream chart. *(Task 5)*
5. **Empty and undefined collections.** A depot with no routes, a station with no demand history, an empty allocation ledger. The Guide's world always has data; the real one may not, and `.map` on `undefined` is the classic crash. *(Tasks 18, 21)*
6. **Health bypasses faults, so health alone cannot detect degradation.** `Guide §7.10` exempts `/v1/health` from fault injection. An app that reads system state from health alone shows "normal" throughout an `unavailable` fault — the exact moment the degraded-mode requirement is being scored. *(Task 16)*

---

## File Structure

| Path | Responsibility |
|---|---|
| `frontend/src/design/tokens.css` | CSS custom properties, both themes. The only place colour literals exist. |
| `frontend/src/design/theme.tsx` | Theme provider, persisted preference, `data-theme` stamping |
| `frontend/src/lib/types/simulator.ts` | Verbatim transcription of the Guide §4–§7 contract |
| `frontend/src/lib/types/domain.ts` | UI-facing derived types (risk, freshness) |
| `frontend/src/lib/api/client.ts` | `SimulatorClient` interface, `SimResponse<T>`, `SimulatorError` |
| `frontend/src/lib/api/mock/seed.ts` | Seeded PRNG |
| `frontend/src/lib/api/mock/fixtures.ts` | Guide §8 world definition, verbatim |
| `frontend/src/lib/api/mock/world.ts` | Deterministic world engine |
| `frontend/src/lib/api/mock/emitter.ts` | SSE-shaped event bus |
| `frontend/src/lib/api/mock/mockClient.ts` | `SimulatorClient` over the world engine |
| `frontend/src/lib/api/http/httpClient.ts` | `SimulatorClient` over `fetch` |
| `frontend/src/lib/api/index.ts` | Implementation selector |
| `frontend/src/lib/hooks/*` | `useSimulatorQuery`, `useStream`, `useFreshness`, `useHealth` |
| `frontend/src/lib/format.ts` | Liters, ticks, sim-time, percent, duration |
| `frontend/src/components/*` | Shared primitives |
| `frontend/src/icons/*` | SVG icon set |
| `frontend/src/app/shell/*` | TopBar, SideNav, StatusStrip, DegradedBanner |
| `frontend/src/features/dashboard/*` | Operations Dashboard |
| `frontend/src/features/_stub/*` | Placeholder screens for Plans 2–3 routes |

---

## Task 1: Scaffold and tooling

**Files:**
- Create: `frontend/package.json`, `frontend/vite.config.ts`, `frontend/tsconfig.json`, `frontend/tsconfig.node.json`, `frontend/tailwind.config.ts`, `frontend/postcss.config.js`, `frontend/index.html`, `frontend/.env.example`, `frontend/src/main.tsx`, `frontend/src/app/App.tsx`, `frontend/src/test/setup.ts`, `frontend/src/app/App.test.tsx`

**Interfaces:**
- Consumes: nothing
- Produces: a working `npm run dev`, `npm test`, `npm run build`, `npm run typecheck`, `npm run lint`

- [ ] **Step 1: Create the project skeleton**

Run from the repo root:

```bash
mkdir -p frontend/src && cd frontend
```

Create `frontend/package.json`:

```json
{
  "name": "fuel-supply-frontend",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc -b && vite build",
    "preview": "vite preview",
    "typecheck": "tsc --noEmit",
    "test": "vitest run",
    "test:watch": "vitest",
    "lint": "eslint . --ext .ts,.tsx --max-warnings 0"
  },
  "dependencies": {
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "react-router-dom": "^6.26.2"
  },
  "devDependencies": {
    "@testing-library/jest-dom": "^6.5.0",
    "@testing-library/react": "^16.0.1",
    "@testing-library/user-event": "^14.5.2",
    "@types/react": "^18.3.11",
    "@types/react-dom": "^18.3.1",
    "@typescript-eslint/eslint-plugin": "^8.8.0",
    "@typescript-eslint/parser": "^8.8.0",
    "@vitejs/plugin-react": "^4.3.2",
    "autoprefixer": "^10.4.20",
    "eslint": "^9.12.0",
    "eslint-plugin-react-hooks": "^5.0.0",
    "jsdom": "^25.0.1",
    "postcss": "^8.4.47",
    "tailwindcss": "^3.4.13",
    "typescript": "^5.6.3",
    "vite": "^5.4.9",
    "vitest": "^1.6.0"
  }
}
```

- [ ] **Step 2: Create build configuration**

`frontend/vite.config.ts`:

```ts
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: true,
  },
})
```

`frontend/src/test/setup.ts`:

```ts
import '@testing-library/jest-dom/vitest'
```

`frontend/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noFallthroughCasesInSwitch": true,
    "exactOptionalPropertyTypes": true,
    "noUncheckedIndexedAccess": true,
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "skipLibCheck": true,
    "types": ["vitest/globals"]
  },
  "include": ["src"]
}
```

`frontend/postcss.config.js`:

```js
export default {
  plugins: { tailwindcss: {}, autoprefixer: {} },
}
```

`frontend/index.html`:

```html
<!doctype html>
<html lang="en" data-theme="dark">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Fuel Supply Intelligence &amp; Resilience Platform</title>
    <meta name="description" content="Operator console for the simulated Bangladesh fuel supply network." />
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

`frontend/.env.example`:

```bash
# 'mock' runs the deterministic in-browser world engine (default).
# 'live' talks to the published BUP Fuel Supply Simulator over HTTP.
VITE_SIMULATOR_MODE=mock

# Only used when VITE_SIMULATOR_MODE=live
VITE_SIMULATOR_BASE_URL=http://localhost:8000
```

- [ ] **Step 3: Install dependencies**

Run: `cd frontend && npm install`
Expected: completes without error.

- [ ] **Step 4: Write the smoke test**

`frontend/src/app/App.test.tsx`:

```tsx
import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { App } from './App'

describe('App', () => {
  it('renders the application heading', () => {
    render(<App />)
    expect(screen.getByRole('heading', { level: 1 })).toBeInTheDocument()
  })
})
```

- [ ] **Step 5: Run the test to verify it fails**

Run: `npm test`
Expected: FAIL — cannot resolve `./App`.

- [ ] **Step 6: Write the minimal App**

`frontend/src/app/App.tsx`:

```tsx
export function App() {
  return (
    <main>
      <h1>Fuel Supply Intelligence &amp; Resilience Platform</h1>
    </main>
  )
}
```

`frontend/src/main.tsx`:

```tsx
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './app/App'
import './design/tokens.css'

const root = document.getElementById('root')
if (!root) throw new Error('Root element #root not found')

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
```

Create an empty `frontend/src/design/tokens.css` for now — Task 2 fills it.

- [ ] **Step 7: Run the test to verify it passes**

Run: `npm test`
Expected: PASS, 1 test.

- [ ] **Step 8: Add the ESLint layering rule**

`frontend/eslint.config.js`:

```js
import tseslint from '@typescript-eslint/eslint-plugin'
import tsparser from '@typescript-eslint/parser'
import reactHooks from 'eslint-plugin-react-hooks'

// The layering rule from spec 4.2: feature code must never reach past the
// client interface into fixtures or the mock implementation. Without this it
// erodes silently and the mock-to-live swap stops being a config change.
const LAYERING = [
  {
    group: ['**/api/mock/*', '**/api/mock/**', '**/fixtures', '**/fixtures/**'],
    message:
      'Features must not import fixtures or the mock client. Import from lib/api instead.',
  },
]

export default [
  { ignores: ['dist/**', 'node_modules/**'] },
  {
    files: ['**/*.ts', '**/*.tsx'],
    languageOptions: {
      parser: tsparser,
      parserOptions: { ecmaVersion: 2022, sourceType: 'module', ecmaFeatures: { jsx: true } },
    },
    plugins: { '@typescript-eslint': tseslint, 'react-hooks': reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'no-restricted-imports': ['error', { patterns: LAYERING }],
    },
  },
]
```

- [ ] **Step 9: Add `.env` to the ignore list and verify the toolchain**

Run: `echo "VITE_SIMULATOR_MODE=mock" > .env && npm run typecheck && npm run lint && npm run build`
Expected: all three pass.

- [ ] **Step 10: Commit**

```bash
cd ..
git add frontend
git commit -m "feat(frontend): scaffold React + Vite + TS + Tailwind with layering lint rule"
```

---

## Task 2: Design tokens and theme

**Files:**
- Create: `frontend/src/design/tokens.css`, `frontend/src/design/theme.tsx`, `frontend/src/design/theme.test.tsx`
- Modify: `frontend/tailwind.config.ts`

**Interfaces:**
- Consumes: Task 1's scaffold
- Produces: `<ThemeProvider>` wrapping the app; `useTheme(): { theme: 'dark' | 'light'; setTheme(t): void; toggle(): void }`; Tailwind colour utilities `bg-surface`, `text-muted`, `border-hairline`, etc.

- [ ] **Step 1: Write the failing test**

`frontend/src/design/theme.test.tsx`:

```tsx
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm test -- src/design/theme.test.tsx`
Expected: FAIL — cannot resolve `./theme`.

- [ ] **Step 3: Write the token sheet**

`frontend/src/design/tokens.css`:

```css
/* The only file in the codebase containing colour literals (spec 9.2).
   Components reference semantic tokens; no raw hex appears elsewhere. */

:root,
:root[data-theme='dark'] {
  color-scheme: dark;

  --bg: #0b1220;
  --surface: #131c2e;
  --surface-raised: #1a2438;
  --border: rgba(255, 255, 255, 0.08);
  --border-strong: rgba(255, 255, 255, 0.14);

  --text: #e8edf5;
  --text-muted: #94a3b8;
  --text-subtle: #64748b;

  --primary: #3b82f6;
  --on-primary: #ffffff;
  --accent: #f59e0b;

  --chart-surface: #131c2e;
  --chart-grid: rgba(255, 255, 255, 0.06);
  --chart-axis: rgba(255, 255, 255, 0.14);

  --series-diesel: #3987e5;
  --series-petrol: #d95926;
  --series-octane: #199e70;
}

:root[data-theme='light'] {
  color-scheme: light;

  --bg: #f8fafc;
  --surface: #ffffff;
  --surface-raised: #ffffff;
  --border: #dbeafe;
  --border-strong: #bfdbfe;

  --text: #0f172a;
  --text-muted: #475569;
  --text-subtle: #64748b;

  --primary: #1e40af;
  --on-primary: #ffffff;
  --accent: #d97706;

  --chart-surface: #ffffff;
  --chart-grid: rgba(15, 23, 42, 0.07);
  --chart-axis: rgba(15, 23, 42, 0.18);

  --series-diesel: #2a78d6;
  --series-petrol: #eb6834;
  --series-octane: #1baf7a;
}

/* Status colours are theme-invariant by design (spec 9.2): a critical alert
   must read identically in a dark control room and on a light laptop. */
:root {
  --status-good: #0ca30c;
  --status-warning: #fab219;
  --status-serious: #ec835a;
  --status-critical: #d03b3b;

  --radius-control: 4px;
  --radius-panel: 6px;
  --radius-pill: 999px;

  --z-content: 0;
  --z-sticky: 10;
  --z-dropdown: 20;
  --z-overlay: 40;
  --z-modal: 100;
  --z-toast: 1000;

  --dur-hover: 150ms;
  --dur-panel: 200ms;
  --dur-modal-in: 250ms;
  --dur-modal-out: 150ms;
  --dur-route: 300ms;
}

@media (prefers-reduced-motion: reduce) {
  :root {
    --dur-hover: 0ms;
    --dur-panel: 0ms;
    --dur-modal-in: 0ms;
    --dur-modal-out: 0ms;
    --dur-route: 0ms;
  }
}

html,
body,
#root {
  height: 100%;
}

body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: 'Fira Sans', system-ui, -apple-system, 'Segoe UI', sans-serif;
  font-size: 14px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}

/* Every number in this application either aligns in a column or updates
   live, so tabular figures are the default rather than the exception. */
.tnum {
  font-variant-numeric: tabular-nums;
}

.mono {
  font-family: 'Fira Code', ui-monospace, SFMono-Regular, Menlo, monospace;
  font-variant-numeric: tabular-nums;
}

*:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: 2px;
  border-radius: 2px;
}
```

- [ ] **Step 4: Map tokens into Tailwind**

`frontend/tailwind.config.ts`:

```ts
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
```

- [ ] **Step 5: Write the theme provider**

`frontend/src/design/theme.tsx`:

```tsx
import {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
  type ReactNode,
} from 'react'

export type Theme = 'dark' | 'light'

const STORAGE_KEY = 'fuel.theme'

type ThemeContextValue = {
  theme: Theme
  setTheme: (theme: Theme) => void
  toggle: () => void
}

const ThemeContext = createContext<ThemeContextValue | null>(null)

function readStoredTheme(): Theme {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    // A corrupt or outdated value must not produce an invalid theme.
    return raw === 'light' || raw === 'dark' ? raw : 'dark'
  } catch {
    // Storage can throw in private windows and when site data is blocked.
    return 'dark'
  }
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(readStoredTheme)

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    try {
      window.localStorage.setItem(STORAGE_KEY, theme)
    } catch {
      // Preference simply does not persist; the app still renders correctly.
    }
  }, [theme])

  const setTheme = useCallback((next: Theme) => setThemeState(next), [])
  const toggle = useCallback(
    () => setThemeState((current) => (current === 'dark' ? 'light' : 'dark')),
    [],
  )

  const value = useMemo(() => ({ theme, setTheme, toggle }), [theme, setTheme, toggle])

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext)
  if (!ctx) throw new Error('useTheme must be used inside a ThemeProvider')
  return ctx
}
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `npm test -- src/design/theme.test.tsx`
Expected: PASS, 4 tests.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/design frontend/tailwind.config.ts
git commit -m "feat(frontend): add design tokens and theme provider with corrupt-value guard"
```

---

## Task 3: Simulator contract types

**Files:**
- Create: `frontend/src/lib/types/simulator.ts`, `frontend/src/lib/types/simulator.test.ts`

**Interfaces:**
- Consumes: nothing
- Produces: all entity types and unions, plus `isFuelType`, `isAllocationStatus`, `SIMULATOR_ERROR_CODES`

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/types/simulator.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import {
  isFuelType, isAllocationStatus, isDepotStatus, isStationStatus,
  isRouteStatus, isSupplyStatus, isEventStatus, isInstanceStatus,
  SIMULATOR_ERROR_CODES, FUEL_TYPES,
} from './simulator'

describe('simulator contract guards', () => {
  it('accepts every declared fuel type', () => {
    for (const fuel of FUEL_TYPES) expect(isFuelType(fuel)).toBe(true)
  })

  it('rejects an unknown fuel type', () => {
    expect(isFuelType('KEROSENE')).toBe(false)
    expect(isFuelType(undefined)).toBe(false)
    expect(isFuelType(3)).toBe(false)
  })

  it('guards each status union', () => {
    expect(isDepotStatus('CONSTRAINED')).toBe(true)
    expect(isDepotStatus('OUTAGE')).toBe(false)   // station status, not depot
    expect(isStationStatus('OUTAGE')).toBe(true)
    expect(isRouteStatus('DISRUPTED')).toBe(true)
    expect(isSupplyStatus('DELAYED')).toBe(true)
    expect(isEventStatus('ACTIVE')).toBe(true)
    expect(isInstanceStatus('RUNNING')).toBe(true)
    expect(isAllocationStatus('IN_TRANSIT')).toBe(true)
    expect(isAllocationStatus('SHIPPED')).toBe(false)
  })

  it('declares all ten allocation error codes from the guide', () => {
    expect(SIMULATOR_ERROR_CODES).toHaveLength(10)
    expect(SIMULATOR_ERROR_CODES).toContain('DISPATCH_CAPACITY_EXCEEDED')
    expect(SIMULATOR_ERROR_CODES).toContain('IDEMPOTENCY_KEY_MISMATCH')
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm test -- src/lib/types/simulator.test.ts`
Expected: FAIL — cannot resolve `./simulator`.

- [ ] **Step 3: Write the contract**

`frontend/src/lib/types/simulator.ts` — transcribed verbatim from Guide §4–§7. Do not normalise or "improve" these shapes.

```ts
export const FUEL_TYPES = ['DIESEL', 'PETROL', 'OCTANE'] as const
export type FuelType = (typeof FUEL_TYPES)[number]

export const DEPOT_STATUSES = ['OPEN', 'CONSTRAINED'] as const
export type DepotStatus = (typeof DEPOT_STATUSES)[number]

export const STATION_STATUSES = ['OPEN', 'OUTAGE'] as const
export type StationStatus = (typeof STATION_STATUSES)[number]

export const ROUTE_STATUSES = ['AVAILABLE', 'DISRUPTED'] as const
export type RouteStatus = (typeof ROUTE_STATUSES)[number]

export const SUPPLY_STATUSES = ['SCHEDULED', 'DELAYED', 'ARRIVED'] as const
export type SupplyStatus = (typeof SUPPLY_STATUSES)[number]

export const EVENT_STATUSES = ['SCHEDULED', 'ACTIVE', 'RESOLVED'] as const
export type EventStatus = (typeof EVENT_STATUSES)[number]

export const ALLOCATION_STATUSES = [
  'PENDING', 'IN_TRANSIT', 'ARRIVED', 'FAILED', 'CANCELLED',
] as const
export type AllocationStatus = (typeof ALLOCATION_STATUSES)[number]

export const INSTANCE_STATUSES = ['PAUSED', 'RUNNING'] as const
export type InstanceStatus = (typeof INSTANCE_STATUSES)[number]

export type FuelMap = Record<FuelType, number>

export interface Region {
  id: string
  name: string
  demand_factor: number
}

export interface Depot {
  id: string
  name: string
  region_id: string
  status: DepotStatus
  dispatch_capacity_per_tick: number
  capacity: FuelMap
  inventory: FuelMap
}

export interface Station {
  id: string
  name: string
  region_id: string
  status: StationStatus
  demand_profile: DemandProfileName
  demand_multiplier: number
  capacity: FuelMap
  inventory: FuelMap
}

export interface Route {
  id: string
  source_depot_id: string
  destination_station_id: string
  transit_ticks: number
  max_shipment: number
  status: RouteStatus
}

export interface SupplyArrival {
  id: string
  depot_id: string
  fuel_type: FuelType
  quantity: number
  planned_tick: number
  actual_tick: number | null
  status: SupplyStatus
}

export interface DomainEvent {
  id: number
  type: EventType
  start_tick: number
  end_tick: number
  status: EventStatus
  parameters: Record<string, unknown>
}

export type EventType =
  | 'demand_spike'
  | 'route_disruption'
  | 'station_outage'
  | 'depot_constraint'
  | 'shipment_delay'
  | 'supply_shortfall'

export type FaultType =
  | 'latency'
  | 'unavailable'
  | 'error_rate'
  | 'stale_data'
  | 'stream_disconnect'

export interface Fault {
  id: number
  type: FaultType
  start_wall_time: string
  end_wall_time: string
  active: boolean
  parameters: Record<string, unknown>
}

export interface Allocation {
  id: number
  idempotency_key: string
  source_depot_id: string
  destination_station_id: string
  route_id: string
  fuel_type: FuelType
  quantity: number
  created_tick: number
  departure_tick: number | null
  expected_arrival_tick: number | null
  actual_arrival_tick: number | null
  status: AllocationStatus
  failure_reason: string | null
}

export interface AllocationRequest {
  idempotency_key: string
  source_depot_id: string
  destination_station_id: string
  route_id: string
  fuel_type: FuelType
  quantity: number
}

export interface DemandObservation {
  id: number
  station_id: string
  fuel_type: FuelType
  tick: number
  sim_time: string
  demand_liters: number
  served_liters: number
  unmet_liters: number
}

export interface Metrics {
  served_demand_liters: number
  unmet_demand_liters: number
  service_level: number
  allocation_liters: number
  allocation_failures: number
}

export interface SimInstance {
  id: number
  scenario_id: string
  scenario_version: string
  seed: number
  sim_time: string
  tick: number
  tick_minutes: number
  status: InstanceStatus
}

export interface Health {
  status: string
  database: string
  simulation: { status: InstanceStatus; tick: number }
}

export interface AuditEntry {
  id: number
  wall_time: string
  sim_time: string
  tick: number
  action: string
  entity_type: string
  entity_id: string
  result: string
  metadata_json: Record<string, unknown>
}

export const DEMAND_PROFILE_NAMES = [
  'urban_high', 'industrial', 'highway', 'regional',
] as const
export type DemandProfileName = (typeof DEMAND_PROFILE_NAMES)[number]

export interface EventRequest {
  type: EventType
  start_tick: number
  duration_ticks: number
  parameters?: Record<string, unknown>
}

export interface FaultRequest {
  type: FaultType
  duration_seconds: number
  parameters?: Record<string, unknown>
}

export const SIMULATOR_ERROR_CODES = [
  'NOT_FOUND',
  'ALLOCATION_NOT_FOUND',
  'IDEMPOTENCY_KEY_MISMATCH',
  'ROUTE_MISMATCH',
  'DEPOT_CLOSED',
  'STATION_CLOSED',
  'ROUTE_DISRUPTED',
  'ROUTE_CAPACITY_EXCEEDED',
  'INSUFFICIENT_INVENTORY',
  'DISPATCH_CAPACITY_EXCEEDED',
] as const
export type SimulatorErrorCode = (typeof SIMULATOR_ERROR_CODES)[number] | 'DESTINATION_CAPACITY_EXCEEDED' | 'CANNOT_CANCEL' | 'FAULT_INJECTED'

function guard<T extends string>(allowed: readonly T[]) {
  const set = new Set<string>(allowed)
  return (value: unknown): value is T => typeof value === 'string' && set.has(value)
}

export const isFuelType = guard(FUEL_TYPES)
export const isDepotStatus = guard(DEPOT_STATUSES)
export const isStationStatus = guard(STATION_STATUSES)
export const isRouteStatus = guard(ROUTE_STATUSES)
export const isSupplyStatus = guard(SUPPLY_STATUSES)
export const isEventStatus = guard(EVENT_STATUSES)
export const isAllocationStatus = guard(ALLOCATION_STATUSES)
export const isInstanceStatus = guard(INSTANCE_STATUSES)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `npm test -- src/lib/types/simulator.test.ts`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/types
git commit -m "feat(frontend): transcribe simulator contract types from integration guide"
```

---

## Task 4: Seeded PRNG and world fixtures

**Files:**
- Create: `frontend/src/lib/api/mock/seed.ts`, `frontend/src/lib/api/mock/fixtures.ts`, `frontend/src/lib/api/mock/fixtures.test.ts`

**Interfaces:**
- Consumes: Task 3 types
- Produces: `createRng(seed: number): () => number`; `REGIONS`, `DEPOTS`, `STATIONS`, `ROUTES`, `DEMAND_PROFILES`, `HOUR_FACTORS`, `PROFILE_NOISE`, `SUPPLY_SCHEDULE`, `DEMAND_PROFILE_NAMES`, `cloneFuelMap`, `DEFAULT_SEED`, `DEFAULT_SIMULATION_SPEED`, `TICK_MINUTES`

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/api/mock/fixtures.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { createRng } from './seed'
import { DEPOTS, STATIONS, ROUTES, REGIONS, SUPPLY_SCHEDULE, DEMAND_PROFILES } from './fixtures'

describe('createRng', () => {
  it('produces the same sequence for the same seed', () => {
    const a = createRng(12345)
    const b = createRng(12345)
    const seqA = Array.from({ length: 5 }, () => a())
    const seqB = Array.from({ length: 5 }, () => b())
    expect(seqA).toEqual(seqB)
  })

  it('produces a different sequence for a different seed', () => {
    const a = createRng(1)
    const b = createRng(2)
    expect(a()).not.toBe(b())
  })

  it('stays within [0, 1)', () => {
    const rng = createRng(99)
    for (let i = 0; i < 1000; i++) {
      const v = rng()
      expect(v).toBeGreaterThanOrEqual(0)
      expect(v).toBeLessThan(1)
    }
  })
})

describe('world fixtures match the guide exactly', () => {
  it('defines two regions with the documented demand factors', () => {
    expect(REGIONS).toHaveLength(2)
    expect(REGIONS.map((r) => r.id).sort()).toEqual(['region-chattogram', 'region-dhaka'])
    expect(REGIONS.find((r) => r.id === 'region-chattogram')?.demand_factor).toBe(1.08)
  })

  it('defines two depots with the documented dispatch capacity', () => {
    const gazipur = DEPOTS.find((d) => d.id === 'depot-gazipur')
    const patiya = DEPOTS.find((d) => d.id === 'depot-patiya')
    expect(gazipur?.dispatch_capacity_per_tick).toBe(12000)
    expect(gazipur?.inventory.DIESEL).toBe(60000)
    expect(gazipur?.capacity.OCTANE).toBe(45000)
    expect(patiya?.dispatch_capacity_per_tick).toBe(11000)
  })

  it('defines four stations with the documented profiles', () => {
    expect(STATIONS).toHaveLength(4)
    expect(STATIONS.find((s) => s.id === 'station-mirpur')?.demand_profile).toBe('urban_high')
    expect(STATIONS.find((s) => s.id === 'station-tongi')?.demand_profile).toBe('industrial')
    expect(STATIONS.find((s) => s.id === 'station-karnaphuli')?.demand_profile).toBe('highway')
    expect(STATIONS.find((s) => s.id === 'station-coxsbazar')?.demand_profile).toBe('regional')
  })

  it('defines six routes with the documented transit times and limits', () => {
    expect(ROUTES).toHaveLength(6)
    const r = ROUTES.find((x) => x.id === 'route-patiya-coxsbazar')
    expect(r?.transit_ticks).toBe(3)
    expect(r?.max_shipment).toBe(6000)
  })

  it('defines the four demand profiles', () => {
    expect(DEMAND_PROFILES.urban_high.DIESEL).toBe(8500)
    expect(DEMAND_PROFILES.industrial.DIESEL).toBe(14000)
  })

  it('defines the 22-arrival supply schedule', () => {
    expect(SUPPLY_SCHEDULE).toHaveLength(22)
    const burst = SUPPLY_SCHEDULE.filter((a) => a.planned_tick >= 12 && a.planned_tick <= 20)
    expect(burst).toHaveLength(4)
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm test -- src/lib/api/mock/fixtures.test.ts`
Expected: FAIL — cannot resolve `./seed`.

- [ ] **Step 3: Write the PRNG**

`frontend/src/lib/api/mock/seed.ts`:

```ts
/**
 * mulberry32 — a small, fast, deterministic PRNG.
 *
 * The Guide states the world is deterministic: same scenario + same seed +
 * same actions produce byte-identical state. A seeded generator is what makes
 * that true in the browser, and what makes a demo reproducible.
 */
export function createRng(seed: number): () => number {
  let state = seed >>> 0
  return function next(): number {
    state = (state + 0x6d2b79f5) >>> 0
    let t = state
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}
```

- [ ] **Step 4: Write the fixtures**

`frontend/src/lib/api/mock/fixtures.ts` — values copied verbatim from Guide §8.

```ts
import type {
  Depot, Station, Route, Region, DemandProfileName, FuelMap, FuelType,
} from '../../types/simulator'

export const DEFAULT_SEED = 12345
export const TICK_MINUTES = 15
export const DEFAULT_SIMULATION_SPEED = 8

export const REGIONS: Region[] = [
  { id: 'region-dhaka', name: 'Dhaka Division', demand_factor: 1.0 },
  { id: 'region-chattogram', name: 'Chattogram Division', demand_factor: 1.08 },
]

export const DEPOTS: Depot[] = [
  {
    id: 'depot-gazipur',
    name: 'Gazipur Depot',
    region_id: 'region-dhaka',
    status: 'OPEN',
    dispatch_capacity_per_tick: 12000,
    capacity: { DIESEL: 90000, PETROL: 70000, OCTANE: 45000 },
    inventory: { DIESEL: 60000, PETROL: 45000, OCTANE: 26000 },
  },
  {
    id: 'depot-patiya',
    name: 'Patiya Depot',
    region_id: 'region-chattogram',
    status: 'OPEN',
    dispatch_capacity_per_tick: 11000,
    capacity: { DIESEL: 85000, PETROL: 65000, OCTANE: 40000 },
    inventory: { DIESEL: 55000, PETROL: 42000, OCTANE: 24000 },
  },
]

export const STATIONS: Station[] = [
  {
    id: 'station-mirpur', name: 'Mirpur Fuel Station', region_id: 'region-dhaka',
    status: 'OPEN', demand_profile: 'urban_high', demand_multiplier: 1.0,
    capacity: { DIESEL: 15000, PETROL: 14000, OCTANE: 9000 },
    inventory: { DIESEL: 9000, PETROL: 9000, OCTANE: 5000 },
  },
  {
    id: 'station-tongi', name: 'Tongi Fuel Station', region_id: 'region-dhaka',
    status: 'OPEN', demand_profile: 'industrial', demand_multiplier: 1.0,
    capacity: { DIESEL: 18000, PETROL: 9000, OCTANE: 6000 },
    inventory: { DIESEL: 11000, PETROL: 6000, OCTANE: 3500 },
  },
  {
    id: 'station-karnaphuli', name: 'Karnaphuli Fuel Station', region_id: 'region-chattogram',
    status: 'OPEN', demand_profile: 'highway', demand_multiplier: 1.0,
    capacity: { DIESEL: 14000, PETROL: 15000, OCTANE: 9000 },
    inventory: { DIESEL: 8500, PETROL: 9500, OCTANE: 5200 },
  },
  {
    id: 'station-coxsbazar', name: "Cox's Bazar Fuel Station", region_id: 'region-chattogram',
    status: 'OPEN', demand_profile: 'regional', demand_multiplier: 1.0,
    capacity: { DIESEL: 12000, PETROL: 12000, OCTANE: 7000 },
    inventory: { DIESEL: 7500, PETROL: 7500, OCTANE: 4200 },
  },
]

export const ROUTES: Route[] = [
  { id: 'route-gazipur-mirpur', source_depot_id: 'depot-gazipur', destination_station_id: 'station-mirpur', transit_ticks: 2, max_shipment: 7000, status: 'AVAILABLE' },
  { id: 'route-gazipur-tongi', source_depot_id: 'depot-gazipur', destination_station_id: 'station-tongi', transit_ticks: 2, max_shipment: 6500, status: 'AVAILABLE' },
  { id: 'route-patiya-karnaphuli', source_depot_id: 'depot-patiya', destination_station_id: 'station-karnaphuli', transit_ticks: 2, max_shipment: 7000, status: 'AVAILABLE' },
  { id: 'route-patiya-coxsbazar', source_depot_id: 'depot-patiya', destination_station_id: 'station-coxsbazar', transit_ticks: 3, max_shipment: 6000, status: 'AVAILABLE' },
  { id: 'route-gazipur-karnaphuli', source_depot_id: 'depot-gazipur', destination_station_id: 'station-karnaphuli', transit_ticks: 4, max_shipment: 5000, status: 'AVAILABLE' },
  { id: 'route-patiya-mirpur', source_depot_id: 'depot-patiya', destination_station_id: 'station-mirpur', transit_ticks: 4, max_shipment: 5000, status: 'AVAILABLE' },
]

/** Liters per simulated day, per profile. Guide 8.5. */
export const DEMAND_PROFILES: Record<DemandProfileName, FuelMap> = {
  urban_high: { DIESEL: 8500, PETROL: 10500, OCTANE: 5600 },
  industrial: { DIESEL: 14000, PETROL: 4500, OCTANE: 2200 },
  highway: { DIESEL: 10500, PETROL: 11000, OCTANE: 6200 },
  regional: { DIESEL: 7200, PETROL: 7600, OCTANE: 3600 },
}

export const PROFILE_NOISE: Record<DemandProfileName, number> = {
  urban_high: 0.10,
  industrial: 0.08,
  highway: 0.12,
  regional: 0.10,
}

/**
 * Hour-of-day multiplier. Guide 8.6.
 * `busy` and `offPeak` are inclusive hour ranges in simulated local hours.
 */
export const HOUR_FACTORS: Record<
  DemandProfileName,
  { busy: Array<[number, number]>; busyFactor: number; offPeakFactor: number }
> = {
  industrial: { busy: [[6, 17]], busyFactor: 1.55, offPeakFactor: 0.45 },
  highway: { busy: [[6, 9], [16, 20]], busyFactor: 1.35, offPeakFactor: 0.75 },
  urban_high: { busy: [[7, 9], [16, 20]], busyFactor: 1.45, offPeakFactor: 0.70 },
  regional: { busy: [[7, 20]], busyFactor: 1.25, offPeakFactor: 0.65 },
}

export interface ScheduledArrival {
  id: string
  depot_id: string
  fuel_type: FuelType
  quantity: number
  planned_tick: number
}

/**
 * The shared 22-arrival schedule. Guide 8.7: four initial-burst arrivals at
 * ticks 12-20 covering day one, then eighteen recurring resupplies spaced 64
 * ticks apart (~16 simulated hours) sized to roughly one day of regional demand.
 */
function buildSupplySchedule(): ScheduledArrival[] {
  const out: ScheduledArrival[] = []
  const burst: Array<[string, FuelType, number, number]> = [
    ['depot-gazipur', 'DIESEL', 18000, 12],
    ['depot-gazipur', 'PETROL', 15000, 14],
    ['depot-patiya', 'DIESEL', 17000, 16],
    ['depot-patiya', 'OCTANE', 11000, 20],
  ]
  burst.forEach(([depot_id, fuel_type, quantity, planned_tick], i) => {
    out.push({ id: `supply-burst-${i + 1}`, depot_id, fuel_type, quantity, planned_tick })
  })

  // Roughly one day of regional demand per depot, per fuel, every 64 ticks.
  const recurring: Array<[string, FuelType, number]> = [
    ['depot-gazipur', 'DIESEL', 22000],
    ['depot-gazipur', 'PETROL', 16500],
    ['depot-gazipur', 'OCTANE', 8800],
    ['depot-patiya', 'DIESEL', 21000],
    ['depot-patiya', 'PETROL', 16000],
    ['depot-patiya', 'OCTANE', 8600],
  ]
  let n = 0
  for (let cycle = 0; cycle < 3; cycle++) {
    const planned_tick = 64 + cycle * 64
    for (const [depot_id, fuel_type, quantity] of recurring) {
      n += 1
      out.push({
        id: `supply-rec-${String(n).padStart(3, '0')}`,
        depot_id, fuel_type, quantity, planned_tick,
      })
    }
  }
  return out
}

export const SUPPLY_SCHEDULE: ScheduledArrival[] = buildSupplySchedule()

export function cloneFuelMap(m: FuelMap): FuelMap {
  return { DIESEL: m.DIESEL, PETROL: m.PETROL, OCTANE: m.OCTANE }
}
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `npm test -- src/lib/api/mock/fixtures.test.ts`
Expected: PASS, 9 tests.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/lib/api/mock
git commit -m "feat(frontend): add seeded PRNG and Guide 8 world fixtures"
```

---

## Task 5: World engine — clock and demand model

**Files:**
- Create: `frontend/src/lib/api/mock/world.ts`, `frontend/src/lib/api/mock/world.demand.test.ts`

**Interfaces:**
- Consumes: Tasks 3–4
- Produces: `class World` with `.tick`, `.simTime`, `.status`, `.step()`, `.instance()`, `.regions()`, `.depots()`, `.stations()`, `.routes()`, and `demandForTick(stationId, fuel, tick, options?: { noiseless?: boolean }): number`; helper `hourOfTick(tick, tickMinutes)`

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/api/mock/world.demand.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { World, hourOfTick } from './world'

describe('hourOfTick', () => {
  it('maps tick zero to hour zero', () => {
    expect(hourOfTick(0, 15)).toBe(0)
  })

  it('advances four hours every sixteen 15-minute ticks', () => {
    expect(hourOfTick(16, 15)).toBe(4)
    expect(hourOfTick(96, 15)).toBe(24 % 24)
  })

  it('wraps at 24 hours', () => {
    expect(hourOfTick(97, 15)).toBe(0)
  })
})

describe('World demand', () => {
  it('is deterministic for a given seed', () => {
    const a = new World({ seed: 12345 })
    const b = new World({ seed: 12345 })
    expect(a.demandForTick('station-mirpur', 'DIESEL', 10)).toBe(
      b.demandForTick('station-mirpur', 'DIESEL', 10),
    )
  })

  it('applies the documented per-tick demand exactly, with noise disabled', () => {
    const w = new World({ seed: 1 })
    // Mirpur: urban_high (8500 L/day diesel) in Dhaka (factor 1.00).
    // Hour 0 is off-peak for urban_high (busy 07-09 and 16-20), factor 0.70.
    const expected = (8500 / 1440) * 15 * 1.0 * 0.70
    expect(w.demandForTick('station-mirpur', 'DIESEL', 0, { noiseless: true }))
      .toBeCloseTo(expected, 6)
  })

  it('applies the Chattogram region factor of 1.08', () => {
    const w = new World({ seed: 1 })
    // Karnaphuli: highway (10500 L/day diesel) in Chattogram (factor 1.08).
    // Hour 0 is off-peak for highway (busy 06-09 and 16-20), factor 0.75.
    const expected = (10500 / 1440) * 15 * 1.08 * 0.75
    expect(w.demandForTick('station-karnaphuli', 'DIESEL', 0, { noiseless: true }))
      .toBeCloseTo(expected, 6)
  })

  it('never returns NaN or a negative value, for any station, fuel or tick', () => {
    const w = new World({ seed: 7 })
    for (const station of w.stations()) {
      for (const fuel of ['DIESEL', 'PETROL', 'OCTANE'] as const) {
        for (const tick of [0, 1, 7, 30, 96, 500, 5000]) {
          const v = w.demandForTick(station.id, fuel, tick)
          expect(Number.isFinite(v)).toBe(true)
          expect(v).toBeGreaterThanOrEqual(0)
        }
      }
    }
  })

  it('never returns NaN for an unknown station', () => {
    const w = new World({ seed: 7 })
    expect(w.demandForTick('station-does-not-exist', 'DIESEL', 10)).toBe(0)
  })

  it('applies the busy-hour multiplier for an industrial station at midday', () => {
    const w = new World({ seed: 12345 })
    // station-tongi is industrial: busy 06:00-17:59 at 1.55x, off-peak 0.45x.
    const midday = w.demandForTick('station-tongi', 'DIESEL', 32, { noiseless: true })  // hour 8
    const night = w.demandForTick('station-tongi', 'DIESEL', 8, { noiseless: true })    // hour 2
    expect(midday / night).toBeCloseTo(1.55 / 0.45, 6)
  })
})

describe('World clock', () => {
  it('starts paused at tick zero', () => {
    const w = new World({ seed: 12345 })
    expect(w.tick).toBe(0)
    expect(w.status).toBe('PAUSED')
  })

  it('advances exactly one tick per step, moving sim time on by tick_minutes', () => {
    const w = new World({ seed: 12345 })
    const start = new Date(w.simTime).getTime()
    w.step()
    expect(w.tick).toBe(1)
    const afterOne = new Date(w.simTime).getTime()
    expect(afterOne - start).toBe(15 * 60_000)
    w.step()
    expect(w.tick).toBe(2)
    expect(new Date(w.simTime).getTime() - afterOne).toBe(15 * 60_000)
  })

  it('is unchanged by a pause', () => {
    const w = new World({ seed: 12345 })
    w.run()
    w.pause()
    const before = w.tick
    expect(w.status).toBe('PAUSED')
    expect(w.tick).toBe(before)
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm test -- src/lib/api/mock/world.demand.test.ts`
Expected: FAIL — cannot resolve `./world`.

- [ ] **Step 3: Write the world engine core**

`frontend/src/lib/api/mock/world.ts`. This task covers the clock and demand; Tasks 6–12 extend the same class.

```ts
import type {
  Depot, Station, Route, Region, SimInstance, InstanceStatus, FuelType,
  DemandObservation, Metrics,
} from '../../types/simulator'
import { createRng } from './seed'
import {
  REGIONS, DEPOTS, STATIONS, ROUTES, DEMAND_PROFILES, PROFILE_NOISE,
  HOUR_FACTORS, DEFAULT_SEED, TICK_MINUTES, cloneFuelMap,
} from './fixtures'

const SIM_EPOCH = '2026-01-01T00:00:00.000Z'

export function hourOfTick(tick: number, tickMinutes: number): number {
  const totalMinutes = tick * tickMinutes
  return Math.floor(totalMinutes / 60) % 24
}

function within(hour: number, ranges: Array<[number, number]>): boolean {
  return ranges.some(([from, to]) => hour >= from && hour <= to)
}

export interface WorldOptions {
  seed?: number
  tickMinutes?: number
  /** The world starts paused; a demo should never advance before the operator says so. */
  startRunning?: boolean
}

export class World {
  tick = 0
  status: InstanceStatus = 'PAUSED'

  private readonly seed: number
  private readonly tickMinutes: number
  private readonly rng: () => number

  private regionsState: Region[]
  private depotsState: Depot[]
  private stationsState: Station[]
  private routesState: Route[]

  constructor(options: WorldOptions = {}) {
    this.seed = options.seed ?? DEFAULT_SEED
    this.tickMinutes = options.tickMinutes ?? TICK_MINUTES
    this.rng = createRng(this.seed)

    this.regionsState = REGIONS.map((r) => ({ ...r }))
    this.depotsState = DEPOTS.map((d) => ({
      ...d, capacity: cloneFuelMap(d.capacity), inventory: cloneFuelMap(d.inventory),
    }))
    this.stationsState = STATIONS.map((s) => ({
      ...s, capacity: cloneFuelMap(s.capacity), inventory: cloneFuelMap(s.inventory),
    }))
    this.routesState = ROUTES.map((r) => ({ ...r }))

    if (options.startRunning) this.status = 'RUNNING'
  }

  get simTime(): string {
    return new Date(
      new Date(SIM_EPOCH).getTime() + this.tick * this.tickMinutes * 60_000,
    ).toISOString()
  }

  instance(): SimInstance {
    return {
      id: 1,
      scenario_id: 'baseline',
      scenario_version: '1.0',
      seed: this.seed,
      sim_time: this.simTime,
      tick: this.tick,
      tick_minutes: this.tickMinutes,
      status: this.status,
    }
  }

  regions(): Region[] { return this.regionsState.map((r) => ({ ...r })) }
  depots(): Depot[] {
    return this.depotsState.map((d) => ({
      ...d, capacity: cloneFuelMap(d.capacity), inventory: cloneFuelMap(d.inventory),
    }))
  }
  stations(): Station[] {
    return this.stationsState.map((s) => ({
      ...s, capacity: cloneFuelMap(s.capacity), inventory: cloneFuelMap(s.inventory),
    }))
  }
  routes(): Route[] { return this.routesState.map((r) => ({ ...r })) }

  run(): void { this.status = 'RUNNING' }
  pause(): void { this.status = 'PAUSED' }
  toggle(): void { this.status = this.status === 'RUNNING' ? 'PAUSED' : 'RUNNING' }

  /**
   * Demand for one station/fuel at an arbitrary tick.
   *
   * Guide 8.5 gives liters per simulated day per profile; 8.6 gives an
   * hour-of-day multiplier; the region carries a demand factor; the station
   * carries a runtime multiplier mutated by demand_spike events.
   *
   * The noise term is derived from (seed, station, fuel, tick) rather than
   * drawn from a shared stream, so asking for demand out of order still gives
   * the same answer. That property is what makes the world deterministic
   * under replay and forecast queries.
   *
   * `noiseless` exists so the model's arithmetic can be asserted exactly
   * rather than only compared against itself.
   */
  demandForTick(
    stationId: string,
    fuel: FuelType,
    tick: number,
    options: { noiseless?: boolean } = {},
  ): number {
    const station = this.stationsState.find((s) => s.id === stationId)
    // An unknown station yields no demand. Returning 0 rather than NaN keeps
    // a stale id from poisoning every downstream total.
    if (!station) return 0

    const profile = DEMAND_PROFILES[station.demand_profile]
    const dailyLiters = profile[fuel]
    const region = this.regionsState.find((r) => r.id === station.region_id)
    const regionFactor = region?.demand_factor ?? 1
    const hour = hourOfTick(tick, this.tickMinutes)
    const factors = HOUR_FACTORS[station.demand_profile]
    const hourFactor = within(hour, factors.busy) ? factors.busyFactor : factors.offPeakFactor

    const perTick = (dailyLiters / (24 * 60)) * this.tickMinutes
    const base = perTick * regionFactor * hourFactor * station.demand_multiplier

    if (options.noiseless) return Math.max(0, base)

    const noise = PROFILE_NOISE[station.demand_profile]
    const jitter = 1 + (this.jitter(stationId, fuel, tick) * 2 - 1) * noise

    const value = base * jitter
    // A malformed multiplier must never propagate NaN into every downstream
    // chart; an unavailable figure is reported as zero and flagged elsewhere.
    return Number.isFinite(value) && value > 0 ? value : 0
  }

  /** Deterministic per-(station, fuel, tick) jitter in [0, 1). */
  private jitter(stationId: string, fuel: FuelType, tick: number): number {
    let h = this.seed >>> 0
    const key = `${stationId}|${fuel}|${tick}`
    for (let i = 0; i < key.length; i++) {
      h = Math.imul(h ^ key.charCodeAt(i), 0x01000193) >>> 0
    }
    h = Math.imul(h ^ (h >>> 15), 0x2c1b3c6d) >>> 0
    return (h >>> 0) / 4294967296
  }

  /** Advances the world by exactly one tick. Extended in Tasks 6-12. */
  step(): void {
    this.tick += 1
  }
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `npm test -- src/lib/api/mock/world.demand.test.ts`
Expected: PASS, 12 tests.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/api/mock/world.ts frontend/src/lib/api/mock/world.demand.test.ts
git commit -m "feat(frontend): add world clock and deterministic demand model"
```

---

## Task 6: World engine — demand observation ledger and metrics

**Files:**
- Modify: `frontend/src/lib/api/mock/world.ts`
- Create: `frontend/src/lib/api/mock/world.ledger.test.ts`

**Interfaces:**
- Consumes: Task 5's `World`
- Produces: `world.observeTick()`, `world.demandHistory({ station_id?, limit? }): DemandObservation[]`, `world.metrics(): Metrics`

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/api/mock/world.ledger.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { World } from './world'

describe('demand observation ledger', () => {
  it('writes twelve rows per tick — four stations by three fuels', () => {
    const w = new World({ seed: 12345 })
    w.step()
    expect(w.demandHistory({ limit: 2000 })).toHaveLength(12)
    w.step()
    expect(w.demandHistory({ limit: 2000 })).toHaveLength(24)
  })

  it('returns an empty array when no tick has run, never undefined', () => {
    const w = new World({ seed: 12345 })
    expect(w.demandHistory({})).toEqual([])
  })

  it('splits demand into served and unmet against available inventory', () => {
    const w = new World({ seed: 12345 })
    w.step()
    for (const row of w.demandHistory({ limit: 2000 })) {
      expect(row.served_liters + row.unmet_liters).toBeCloseTo(row.demand_liters, 6)
      expect(row.unmet_liters).toBeGreaterThanOrEqual(0)
    }
  })

  it('serves nothing at a station that is OUTAGE', () => {
    const w = new World({ seed: 12345 })
    w.step()
    const before = w.demandHistory({ station_id: 'station-mirpur', limit: 2000 })
    expect(before.length).toBeGreaterThan(0)
  })

  it('filters by station_id', () => {
    const w = new World({ seed: 12345 })
    w.step()
    const rows = w.demandHistory({ station_id: 'station-mirpur', limit: 2000 })
    expect(rows).toHaveLength(3)
    expect(rows.every((r) => r.station_id === 'station-mirpur')).toBe(true)
  })

  it('clamps limit to the documented [1, 2000] range', () => {
    const w = new World({ seed: 12345 })
    for (let i = 0; i < 5; i++) w.step()
    expect(w.demandHistory({ limit: 0 }).length).toBe(1)      // clamped up to 1
    expect(w.demandHistory({ limit: 99999 }).length).toBe(60) // clamped down to 2000
  })

  it('returns the most recent rows first when limited', () => {
    const w = new World({ seed: 12345 })
    for (let i = 0; i < 3; i++) w.step()
    const rows = w.demandHistory({ limit: 12 })
    expect(rows).toHaveLength(12)
    expect(Math.max(...rows.map((r) => r.tick))).toBe(3)
  })
})

describe('metrics', () => {
  it('reports a service level of 1 when nothing is unmet', () => {
    const w = new World({ seed: 12345 })
    expect(w.metrics().service_level).toBe(1)
  })

  it('computes service_level as served over served plus unmet', () => {
    const w = new World({ seed: 12345 })
    for (let i = 0; i < 5; i++) w.step()
    const m = w.metrics()
    const expected = m.served_demand_liters / (m.served_demand_liters + m.unmet_demand_liters)
    expect(m.service_level).toBeCloseTo(expected, 9)
  })

  it('never divides by zero when no demand has been recorded', () => {
    const w = new World({ seed: 12345 })
    const m = w.metrics()
    expect(Number.isFinite(m.service_level)).toBe(true)
    expect(m.service_level).toBe(1)
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm test -- src/lib/api/mock/world.ledger.test.ts`
Expected: FAIL — `w.observeTick is not a function`.

- [ ] **Step 3: Implement the ledger**

Add to `frontend/src/lib/api/mock/world.ts`. Append these members to the `World` class and add `observed` to the private fields:

```ts
  private observed: DemandObservation[] = []
  private nextObservationId = 1
  private servedTotalLiters = 0
  private unmetTotalLiters = 0
```

```ts
  /**
   * Records one tick of demand for every station and fuel, consuming station
   * inventory for whatever can be served. Guide 4.11: one row per
   * (station_id, fuel_type) per tick, so twelve rows per tick.
   */
  observeTick(): void {
    for (const station of this.stationsState) {
      for (const fuel of ['DIESEL', 'PETROL', 'OCTANE'] as const) {
        const demand = this.demandForTick(station.id, fuel, this.tick)
        const available = station.status === 'OUTAGE' ? 0 : station.inventory[fuel]
        const served = Math.min(demand, available)
        const unmet = demand - served

        station.inventory[fuel] = Math.max(0, available - served)
        this.servedTotalLiters += served
        this.unmetTotalLiters += unmet

        this.observed.push({
          id: this.nextObservationId++,
          station_id: station.id,
          fuel_type: fuel,
          tick: this.tick,
          sim_time: this.simTime,
          demand_liters: demand,
          served_liters: served,
          unmet_liters: unmet,
        })
      }
    }
  }

  /** Most recent rows first. Guide 4.11: limit is clamped to [1, 2000]. */
  demandHistory(params: { station_id?: string; limit?: number }): DemandObservation[] {
    const limit = Math.min(2000, Math.max(1, Math.trunc(params.limit ?? 200)))
    const pool = params.station_id
      ? this.observed.filter((r) => r.station_id === params.station_id)
      : this.observed
    return pool.slice(-limit).reverse()
  }

  metrics(): Metrics {
    const total = this.servedTotalLiters + this.unmetTotalLiters
    return {
      served_demand_liters: this.servedTotalLiters,
      unmet_demand_liters: this.unmetTotalLiters,
      // With no demand recorded, service level is perfect rather than NaN.
      service_level: total === 0 ? 1 : this.servedTotalLiters / total,
      allocation_liters: this.allocationLiters,
      allocation_failures: this.allocationFailures,
    }
  }
```

Also add these two counters alongside the other private fields — Task 7 maintains them:

```ts
  private allocationLiters = 0
  private allocationFailures = 0
```

And call `observeTick()` from `step()`, replacing the Task 5 body:

```ts
  step(): void {
    this.tick += 1
    this.observeTick()
  }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `npm test -- src/lib/api/mock/world.ledger.test.ts`
Expected: PASS, 10 tests.

- [ ] **Step 5: Run the whole suite to check for regressions**

Run: `npm test`
Expected: All tests pass. Task 5's clock tests still pass — `step()` now also observes, which does not change `tick` or `status`.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/lib/api/mock
git commit -m "feat(frontend): record per-tick demand ledger and compute metrics"
```

---

## Task 7: World engine — supply arrivals

**Files:**
- Modify: `frontend/src/lib/api/mock/world.ts`
- Create: `frontend/src/lib/api/mock/world.supply.test.ts`

**Interfaces:**
- Consumes: Tasks 4–6
- Produces: `world.supplyArrivals(): SupplyArrival[]`

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/api/mock/world.supply.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { World } from './world'

describe('supply arrivals', () => {
  it('exposes all 22 scheduled arrivals', () => {
    const w = new World({ seed: 12345 })
    expect(w.supplyArrivals()).toHaveLength(22)
  })

  it('is sorted by planned_tick ascending', () => {
    const w = new World({ seed: 12345 })
    const ticks = w.supplyArrivals().map((a) => a.planned_tick)
    expect(ticks).toEqual([...ticks].sort((a, b) => a - b))
  })

  it('starts every arrival as SCHEDULED with no actual tick', () => {
    const w = new World({ seed: 12345 })
    for (const a of w.supplyArrivals()) {
      expect(a.status).toBe('SCHEDULED')
      expect(a.actual_tick).toBeNull()
    }
  })

  it('deposits fuel into the depot and marks the arrival ARRIVED once its tick passes', () => {
    const w = new World({ seed: 12345 })
    const before = w.depots().find((d) => d.id === 'depot-gazipur')!.inventory.DIESEL
    for (let i = 0; i < 12; i++) w.step()
    const arrival = w.supplyArrivals().find((a) => a.planned_tick === 12)!
    expect(arrival.status).toBe('ARRIVED')
    expect(arrival.actual_tick).toBe(12)
    const after = w.depots().find((d) => d.id === 'depot-gazipur')!.inventory.DIESEL
    expect(after).toBe(before + 18000)
  })

  it('does not deposit the same arrival twice across ticks', () => {
    const w = new World({ seed: 12345 })
    for (let i = 0; i < 12; i++) w.step()
    const once = w.depots().find((d) => d.id === 'depot-gazipur')!.inventory.DIESEL
    w.step()
    w.step()
    const later = w.depots().find((d) => d.id === 'depot-gazipur')!.inventory.DIESEL
    // Further ticks may consume fuel but must never re-add the same arrival.
    expect(later).toBeLessThanOrEqual(once)
  })

  it('never exceeds depot capacity when an arrival lands', () => {
    const w = new World({ seed: 12345 })
    for (let i = 0; i < 200; i++) w.step()
    for (const depot of w.depots()) {
      for (const fuel of ['DIESEL', 'PETROL', 'OCTANE'] as const) {
        expect(depot.inventory[fuel]).toBeLessThanOrEqual(depot.capacity[fuel])
      }
    }
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm test -- src/lib/api/mock/world.supply.test.ts`
Expected: FAIL — `w.supplyArrivals is not a function`.

- [ ] **Step 3: Implement supply arrivals**

Add to `frontend/src/lib/api/mock/world.ts` — a private field initialised in the constructor:

```ts
  private arrivals: SupplyArrival[] = []
```

In the constructor, after the routes are set:

```ts
    this.arrivals = SUPPLY_SCHEDULE.map((a) => ({
      id: a.id,
      depot_id: a.depot_id,
      fuel_type: a.fuel_type,
      quantity: a.quantity,
      planned_tick: a.planned_tick,
      actual_tick: null,
      status: 'SCHEDULED' as const,
    }))
```

Add the accessor and the arrival processor:

```ts
  /** Guide 4.8: sorted by planned_tick ascending. */
  supplyArrivals(): SupplyArrival[] {
    return [...this.arrivals]
      .sort((a, b) => a.planned_tick - b.planned_tick)
      .map((a) => ({ ...a }))
  }

  /**
   * Lands every scheduled arrival whose planned tick has been reached.
   * Once ARRIVED an arrival is never reprocessed, so fuel cannot be
   * deposited twice.
   */
  private processArrivals(): void {
    for (const arrival of this.arrivals) {
      if (arrival.status === 'ARRIVED') continue
      if (arrival.planned_tick > this.tick) continue

      const depot = this.depotsState.find((d) => d.id === arrival.depot_id)
      if (!depot) continue

      const capacity = depot.capacity[arrival.fuel_type]
      depot.inventory[arrival.fuel_type] = Math.min(
        capacity,
        depot.inventory[arrival.fuel_type] + arrival.quantity,
      )

      arrival.status = 'ARRIVED'
      arrival.actual_tick = this.tick
    }
  }
```

Add the import of `SupplyArrival` and `SUPPLY_SCHEDULE` to the existing import statements, and call `processArrivals()` from `step()` **before** `observeTick()`, so fuel that lands this tick is available to meet this tick's demand:

```ts
  step(): void {
    this.tick += 1
    this.processArrivals()
    this.observeTick()
  }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `npm test -- src/lib/api/mock/world.supply.test.ts`
Expected: PASS, 6 tests.

- [ ] **Step 5: Run the whole suite**

Run: `npm test`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/lib/api/mock
git commit -m "feat(frontend): process supply arrivals with capacity clamping"
```

---

## Task 8: World engine — allocations and the full validation order

**Files:**
- Modify: `frontend/src/lib/api/mock/world.ts`
- Create: `frontend/src/lib/api/mock/world.allocations.test.ts`

**Interfaces:**
- Consumes: Tasks 3–7
- Produces: `world.createAllocation(req: AllocationRequest): Allocation` (throws `SimulatorError`), `world.allocations(): Allocation[]`, `world.cancelAllocation(id: number): Allocation`, `world.setDepotStatus(id, status)`, `world.setStationStatus(id, status)`, `world.setRouteStatus(id, status)`

This task depends on `SimulatorError` from Task 13. Create it here — Task 13 imports it rather than redefining it.

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/api/mock/world.allocations.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { World } from './world'
import { SimulatorError } from '../errors'

const VALID = {
  idempotency_key: 'demo-001',
  source_depot_id: 'depot-gazipur',
  destination_station_id: 'station-mirpur',
  route_id: 'route-gazipur-mirpur',
  fuel_type: 'DIESEL' as const,
  quantity: 3000,
}

function expectCode(fn: () => unknown, code: string) {
  try {
    fn()
    throw new Error(`expected ${code}, but no error was thrown`)
  } catch (err) {
    expect(err).toBeInstanceOf(SimulatorError)
    expect((err as SimulatorError).code).toBe(code)
  }
}

describe('allocation validation order (Guide 5.2)', () => {
  it('accepts a valid allocation and returns PENDING', () => {
    const w = new World({ seed: 12345 })
    const a = w.createAllocation(VALID)
    expect(a.status).toBe('PENDING')
    expect(a.quantity).toBe(3000)
    expect(a.created_tick).toBe(0)
    expect(a.actual_arrival_tick).toBeNull()
  })

  it('returns the existing allocation for a repeated idempotency key and body', () => {
    const w = new World({ seed: 12345 })
    const first = w.createAllocation(VALID)
    const second = w.createAllocation(VALID)
    expect(second.id).toBe(first.id)
    expect(w.allocations()).toHaveLength(1)
  })

  it('rejects the same key with a different body', () => {
    const w = new World({ seed: 12345 })
    w.createAllocation(VALID)
    expectCode(
      () => w.createAllocation({ ...VALID, quantity: 4000 }),
      'IDEMPOTENCY_KEY_MISMATCH',
    )
  })

  it('rejects an unknown depot, station or route with NOT_FOUND', () => {
    const w = new World({ seed: 12345 })
    expectCode(() => w.createAllocation({ ...VALID, source_depot_id: 'nope' }), 'NOT_FOUND')
    expectCode(() => w.createAllocation({ ...VALID, destination_station_id: 'nope' }), 'NOT_FOUND')
    expectCode(() => w.createAllocation({ ...VALID, route_id: 'nope' }), 'NOT_FOUND')
  })

  it('rejects a route that does not connect the given depot and station', () => {
    const w = new World({ seed: 12345 })
    expectCode(
      () => w.createAllocation({ ...VALID, route_id: 'route-gazipur-tongi' }),
      'ROUTE_MISMATCH',
    )
  })

  it('rejects a station that is OUTAGE with STATION_CLOSED', () => {
    const w = new World({ seed: 12345 })
    w.setStationStatus('station-mirpur', 'OUTAGE')
    expectCode(() => w.createAllocation(VALID), 'STATION_CLOSED')
  })

  it('rejects a disrupted route with ROUTE_DISRUPTED', () => {
    const w = new World({ seed: 12345 })
    w.setRouteStatus('route-gazipur-mirpur', 'DISRUPTED')
    expectCode(() => w.createAllocation(VALID), 'ROUTE_DISRUPTED')
  })

  it('rejects a quantity above the route maximum', () => {
    const w = new World({ seed: 12345 })
    expectCode(() => w.createAllocation({ ...VALID, quantity: 7001 }), 'ROUTE_CAPACITY_EXCEEDED')
  })

  it('ACCEPTS a quantity exactly equal to the route maximum', () => {
    // No Guide 5.2 limit check fails on equality: route max (7), dispatch
    // capacity (9) and destination headroom (10) fail on strict >, and depot
    // inventory (8) fails on inventory < quantity. The boundary is legal.
    // route-gazipur-tongi caps at 6500 and Tongi has 7000 L of diesel
    // headroom, so 6500 is at the route limit and legal end to end.
    const w = new World({ seed: 12345 })
    const a = w.createAllocation({
      ...VALID,
      idempotency_key: 'k-max',
      route_id: 'route-gazipur-tongi',
      destination_station_id: 'station-tongi',
      quantity: 6500,
    })
    expect(a.quantity).toBe(6500)
  })

  it('rejects a quantity above remaining depot inventory', () => {
    const w = new World({ seed: 12345 })
    // Drain Gazipur to exactly 5000 L, then ask for 6000 on a route that allows it.
    w.drainDepot('depot-gazipur', 'DIESEL', 55000)
    expectCode(
      () => w.createAllocation({ ...VALID, idempotency_key: 'k-inv', quantity: 6000 }),
      'INSUFFICIENT_INVENTORY',
    )
  })

  it('ACCEPTS a quantity exactly equal to remaining depot inventory', () => {
    const w = new World({ seed: 12345 })
    w.drainDepot('depot-gazipur', 'DIESEL', 55000) // exactly 5000 L left
    const a = w.createAllocation({ ...VALID, idempotency_key: 'k-exact', quantity: 5000 })
    expect(a.quantity).toBe(5000)
  })

  it('rejects when the dispatch capacity for this tick is exceeded', () => {
    const w = new World({ seed: 12345 })
    // Gazipur may dispatch 12000 L per tick. Two legal loads commit 11500 L.
    w.createAllocation({
      ...VALID, idempotency_key: 'd-1',
      route_id: 'route-gazipur-tongi', destination_station_id: 'station-tongi', quantity: 6500,
    })
    w.createAllocation({
      ...VALID, idempotency_key: 'd-2',
      route_id: 'route-gazipur-karnaphuli', destination_station_id: 'station-karnaphuli', quantity: 5000,
    })
    // A third 3000 L load is legal on every other rule and breaches only this one.
    expectCode(
      () => w.createAllocation({
        ...VALID, idempotency_key: 'd-3', fuel_type: 'PETROL',
        route_id: 'route-gazipur-tongi', destination_station_id: 'station-tongi', quantity: 3000,
      }),
      'DISPATCH_CAPACITY_EXCEEDED',
    )
  })

  it('rejects when the destination cannot hold the fuel', () => {
    const w = new World({ seed: 12345 })
    // Mirpur holds 9000 of 15000 diesel; 7000 more would overflow.
    expectCode(
      () => w.createAllocation({ ...VALID, idempotency_key: 'dst-1', quantity: 7000 }),
      'DESTINATION_CAPACITY_EXCEEDED',
    )
  })

  it('reports NOT_FOUND ahead of a route mismatch when both apply', () => {
    // First failure wins: an unknown depot outranks a mismatched route.
    const w = new World({ seed: 12345 })
    expectCode(
      () => w.createAllocation({ ...VALID, source_depot_id: 'nope', route_id: 'route-gazipur-tongi' }),
      'NOT_FOUND',
    )
  })

  it('does not free an idempotency key when an allocation is cancelled', () => {
    const w = new World({ seed: 12345 })
    const a = w.createAllocation(VALID)
    w.cancelAllocation(a.id)
    expectCode(() => w.createAllocation({ ...VALID, quantity: 1000 }), 'IDEMPOTENCY_KEY_MISMATCH')
  })
})

describe('allocation lifecycle', () => {
  it('refunds depot inventory on cancel and marks it CANCELLED', () => {
    const w = new World({ seed: 12345 })
    const before = w.depots().find((d) => d.id === 'depot-gazipur')!.inventory.DIESEL
    const a = w.createAllocation(VALID)
    expect(w.depots().find((d) => d.id === 'depot-gazipur')!.inventory.DIESEL).toBe(before - 3000)
    const cancelled = w.cancelAllocation(a.id)
    expect(cancelled.status).toBe('CANCELLED')
    expect(w.depots().find((d) => d.id === 'depot-gazipur')!.inventory.DIESEL).toBe(before)
  })

  it('refuses to cancel an allocation that has already departed', () => {
    const w = new World({ seed: 12345 })
    const a = w.createAllocation(VALID)
    w.step() // now IN_TRANSIT
    expect(w.allocations().find((x) => x.id === a.id)!.status).toBe('IN_TRANSIT')
    expectCode(() => w.cancelAllocation(a.id), 'CANNOT_CANCEL')
  })

  it('reports ALLOCATION_NOT_FOUND for an unknown id', () => {
    const w = new World({ seed: 12345 })
    expectCode(() => w.cancelAllocation(9999), 'ALLOCATION_NOT_FOUND')
  })

  it('moves PENDING to IN_TRANSIT to ARRIVED across the transit window', () => {
    const w = new World({ seed: 12345 })
    const a = w.createAllocation(VALID) // transit_ticks 2, created at tick 0
    expect(w.allocations().find((x) => x.id === a.id)!.status).toBe('PENDING')
    w.step()
    expect(w.allocations().find((x) => x.id === a.id)!.status).toBe('IN_TRANSIT')
    w.step()
    const arrived = w.allocations().find((x) => x.id === a.id)!
    expect(arrived.status).toBe('ARRIVED')
    expect(arrived.actual_arrival_tick).toBe(2)
  })

  it('marks an in-transit allocation FAILED when its route is disrupted', () => {
    const w = new World({ seed: 12345 })
    const a = w.createAllocation(VALID)
    w.setRouteStatus('route-gazipur-mirpur', 'DISRUPTED')
    for (let i = 0; i < 4; i++) w.step()
    const failed = w.allocations().find((x) => x.id === a.id)!
    expect(failed.status).toBe('FAILED')
    expect(failed.failure_reason).toBeTruthy()
  })

  it('adds delivered fuel to the destination station', () => {
    const w = new World({ seed: 12345 })
    const before = w.stations().find((s) => s.id === 'station-mirpur')!.inventory.DIESEL
    w.createAllocation(VALID)
    for (let i = 0; i < 3; i++) w.step()
    const after = w.stations().find((s) => s.id === 'station-mirpur')!.inventory.DIESEL
    // Some of the 3000 may have been sold, so the increase must be positive
    // but need not equal the full shipment.
    expect(after).toBeGreaterThan(before - 1)
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm test -- src/lib/api/mock/world.allocations.test.ts`
Expected: FAIL — cannot resolve `../errors`.

- [ ] **Step 3: Create the error type**

`frontend/src/lib/api/errors.ts`:

```ts
import type { SimulatorErrorCode } from '../types/simulator'

/**
 * Raised by both client implementations so callers handle one error shape.
 *
 * The Guide uses three different envelopes (9): allocation failures arrive as
 * {"detail":{"code","message"}}, injected faults as
 * {"error":{"code","message"}}, and Pydantic validation as FastAPI's default
 * {"detail":[...]}. All three normalise into this type at the boundary.
 */
export class SimulatorError extends Error {
  readonly status: number
  readonly code: SimulatorErrorCode | string

  constructor(status: number, code: SimulatorErrorCode | string, message: string) {
    super(message)
    this.name = 'SimulatorError'
    this.status = status
    this.code = code
  }
}
```

- [ ] **Step 4: Implement allocations and validation**

Add to `frontend/src/lib/api/mock/world.ts`. Private fields:

```ts
  private allocationRows: Allocation[] = []
  private allocationKeys = new Map<string, number>()
  private nextAllocationId = 1
```

Methods:

```ts
  allocations(): Allocation[] {
    return [...this.allocationRows].sort((a, b) => b.id - a.id).map((a) => ({ ...a }))
  }

  /** Test seam: removes fuel so INSUFFICIENT_INVENTORY can be exercised. */
  drainDepot(depotId: string, fuel: FuelType, amount: number): void {
    const depot = this.depotsState.find((d) => d.id === depotId)
    if (depot) depot.inventory[fuel] = Math.max(0, depot.inventory[fuel] - amount)
  }

  private fail(status: number, code: string, message: string): never {
    throw new SimulatorError(status, code, message)
  }

  /**
   * Validates and creates an allocation. Checks run in the exact order given
   * by Guide 5.2, first failure wins, and every magnitude comparison is a
   * strict > so that a value exactly at the limit is accepted.
   */
  createAllocation(req: AllocationRequest): Allocation {
    // 1. Idempotency.
    const existingId = this.allocationKeys.get(req.idempotency_key)
    if (existingId !== undefined) {
      const existing = this.allocationRows.find((a) => a.id === existingId)!
      const sameBody =
        existing.source_depot_id === req.source_depot_id &&
        existing.destination_station_id === req.destination_station_id &&
        existing.route_id === req.route_id &&
        existing.fuel_type === req.fuel_type &&
        existing.quantity === req.quantity
      if (sameBody) return { ...existing }
      this.fail(409, 'IDEMPOTENCY_KEY_MISMATCH',
        `Idempotency key "${req.idempotency_key}" was already used with a different body.`)
    }

    // 2. Entity existence.
    const depot = this.depotsState.find((d) => d.id === req.source_depot_id)
    if (!depot) this.fail(404, 'NOT_FOUND', `Unknown depot "${req.source_depot_id}".`)
    const station = this.stationsState.find((s) => s.id === req.destination_station_id)
    if (!station) this.fail(404, 'NOT_FOUND', `Unknown station "${req.destination_station_id}".`)
    const route = this.routesState.find((r) => r.id === req.route_id)
    if (!route) this.fail(404, 'NOT_FOUND', `Unknown route "${req.route_id}".`)

    // 3. Route must connect exactly the given pair.
    if (
      route.source_depot_id !== req.source_depot_id ||
      route.destination_station_id !== req.destination_station_id
    ) {
      this.fail(409, 'ROUTE_MISMATCH',
        `Route "${route.id}" connects ${route.source_depot_id} to ${route.destination_station_id}.`)
    }

    // 4. Depot open.
    if (depot.status !== 'OPEN' && depot.status !== 'CONSTRAINED') {
      this.fail(409, 'DEPOT_CLOSED', `Depot "${depot.id}" is ${depot.status}.`)
    }

    // 5. Station open.
    if (station.status !== 'OPEN') {
      this.fail(409, 'STATION_CLOSED', `Station "${station.id}" is ${station.status}.`)
    }

    // 6. Route available.
    if (route.status !== 'AVAILABLE') {
      this.fail(409, 'ROUTE_DISRUPTED', `Route "${route.id}" is ${route.status}.`)
    }

    // 7. Route capacity.
    if (req.quantity > route.max_shipment) {
      this.fail(409, 'ROUTE_CAPACITY_EXCEEDED',
        `Quantity ${req.quantity} exceeds route maximum ${route.max_shipment}.`)
    }

    // 8. Depot inventory.
    if (depot.inventory[req.fuel_type] < req.quantity) {
      this.fail(409, 'INSUFFICIENT_INVENTORY',
        `Depot "${depot.id}" holds ${depot.inventory[req.fuel_type]} L of ${req.fuel_type}.`)
    }

    // 9. Per-tick dispatch capacity, counting this depot's in-flight and
    //    pending quantity created on this tick.
    const committed = this.allocationRows
      .filter((a) =>
        a.source_depot_id === depot.id &&
        a.created_tick === this.tick &&
        (a.status === 'PENDING' || a.status === 'IN_TRANSIT'))
      .reduce((sum, a) => sum + a.quantity, 0)
    if (committed + req.quantity > depot.dispatch_capacity_per_tick) {
      this.fail(409, 'DISPATCH_CAPACITY_EXCEEDED',
        `Depot "${depot.id}" can dispatch ${depot.dispatch_capacity_per_tick} L per tick; ${committed} L already committed.`)
    }

    // 10. Destination headroom.
    if (station.inventory[req.fuel_type] + req.quantity > station.capacity[req.fuel_type]) {
      this.fail(409, 'DESTINATION_CAPACITY_EXCEEDED',
        `Station "${station.id}" can hold ${station.capacity[req.fuel_type]} L of ${req.fuel_type}.`)
    }

    depot.inventory[req.fuel_type] -= req.quantity

    const allocation: Allocation = {
      id: this.nextAllocationId++,
      idempotency_key: req.idempotency_key,
      source_depot_id: req.source_depot_id,
      destination_station_id: req.destination_station_id,
      route_id: req.route_id,
      fuel_type: req.fuel_type,
      quantity: req.quantity,
      created_tick: this.tick,
      // The transit clock starts at creation, not at the next tick: a load on
      // a route with transit_ticks 2 dispatched at tick 0 arrives at tick 2.
      departure_tick: this.tick,
      expected_arrival_tick: this.tick + route.transit_ticks,
      actual_arrival_tick: null,
      status: 'PENDING',
      failure_reason: null,
    }
    this.allocationRows.push(allocation)
    // The key stays occupied permanently, even after cancellation.
    this.allocationKeys.set(req.idempotency_key, allocation.id)
    return { ...allocation }
  }

  cancelAllocation(id: number): Allocation {
    const allocation = this.allocationRows.find((a) => a.id === id)
    if (!allocation) this.fail(404, 'ALLOCATION_NOT_FOUND', `Unknown allocation ${id}.`)
    if (allocation.status !== 'PENDING') {
      this.fail(409, 'CANNOT_CANCEL',
        `Allocation ${id} is ${allocation.status}; only PENDING allocations can be cancelled.`)
    }

    const depot = this.depotsState.find((d) => d.id === allocation.source_depot_id)
    if (depot) {
      depot.inventory[allocation.fuel_type] = Math.min(
        depot.capacity[allocation.fuel_type],
        depot.inventory[allocation.fuel_type] + allocation.quantity,
      )
    }
    allocation.status = 'CANCELLED'
    return { ...allocation }
  }

  /** Advances the lifecycle of every in-flight allocation. */
  private processAllocations(): void {
    for (const a of this.allocationRows) {
      const route = this.routesState.find((r) => r.id === a.route_id)
      if (!route) continue

      if (a.status === 'PENDING') {
        // The transit window was fixed at creation; departing only changes
        // the visible status.
        a.status = 'IN_TRANSIT'
        continue
      }

      if (a.status === 'IN_TRANSIT') {
        // A route disrupted while the load is moving fails the shipment.
        if (route.status !== 'AVAILABLE') {
          a.status = 'FAILED'
          a.failure_reason = `Route ${route.id} became ${route.status} in transit.`
          this.allocationFailures += 1
          continue
        }
        if (a.expected_arrival_tick !== null && this.tick >= a.expected_arrival_tick) {
          const station = this.stationsState.find((s) => s.id === a.destination_station_id)
          if (station) {
            station.inventory[a.fuel_type] = Math.min(
              station.capacity[a.fuel_type],
              station.inventory[a.fuel_type] + a.quantity,
            )
          }
          a.status = 'ARRIVED'
          a.actual_arrival_tick = this.tick
          this.allocationLiters += a.quantity
        }
      }
    }
  }
```

Call `processAllocations()` from `step()`. Order matters: arrivals land first so the depot has fuel, then allocations move, then demand is observed against final inventory.

```ts
  step(): void {
    this.tick += 1
    this.processArrivals()
    this.processAllocations()
    this.observeTick()
  }
```

Task 9 adds `this.processEvents()` as the first line of `step()`, which is why it is absent here.

Also add the three status setters. These are the single place entity status is mutated: Task 9's events call them rather than assigning status inline, so the constants and any future side effects stay in one spot.

```ts
  setDepotStatus(depotId: string, status: DepotStatus): void {
    const depot = this.depotsState.find((d) => d.id === depotId)
    if (depot) depot.status = status
  }

  setStationStatus(stationId: string, status: StationStatus): void {
    const station = this.stationsState.find((s) => s.id === stationId)
    if (station) station.status = status
  }

  setRouteStatus(routeId: string, status: RouteStatus): void {
    const route = this.routesState.find((r) => r.id === routeId)
    if (route) route.status = status
  }
```

Add the needed type imports: `Allocation`, `AllocationRequest`, `DepotStatus`, `StationStatus`, `RouteStatus`, and `SimulatorError` from `../errors`. (`EventRequest` and `DomainEvent` are Task 9's; don't import them yet.)

- [ ] **Step 5: Run the test to verify it passes**

Run: `npm test -- src/lib/api/mock/world.allocations.test.ts`
Expected: PASS, 21 tests.

- [ ] **Step 6: Run the whole suite**

Run: `npm test`
Expected: All pass.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/lib/api/mock frontend/src/lib/api/errors.ts
git commit -m "feat(frontend): implement allocation lifecycle and Guide 5.2 validation order"
```

---

## Task 9: World engine — events with correct reversal semantics

**Files:**
- Modify: `frontend/src/lib/api/mock/world.ts`
- Create: `frontend/src/lib/api/mock/world.events.test.ts`

**Interfaces:**
- Consumes: Task 8's `World`, including its `setDepotStatus` / `setStationStatus` / `setRouteStatus` setters
- Produces: `world.injectEvent(req: EventRequest): DomainEvent`, `world.events(): DomainEvent[]`

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/api/mock/world.events.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { World } from './world'

describe('event injection', () => {
  it('creates an event with end_tick derived from start and duration', () => {
    const w = new World({ seed: 12345 })
    const e = w.injectEvent({ type: 'demand_spike', start_tick: 8, duration_ticks: 12, parameters: {} })
    expect(e.start_tick).toBe(8)
    expect(e.end_tick).toBe(20)
    expect(e.status).toBe('SCHEDULED')
  })

  it('defaults parameters to an empty object', () => {
    const w = new World({ seed: 12345 })
    const e = w.injectEvent({ type: 'route_disruption', start_tick: 0, duration_ticks: 5 })
    expect(e.parameters).toEqual({})
  })

  it('rejects a non-positive duration', () => {
    const w = new World({ seed: 12345 })
    expect(() => w.injectEvent({ type: 'demand_spike', start_tick: 0, duration_ticks: 0 })).toThrow()
  })

  it('rejects a negative start tick', () => {
    const w = new World({ seed: 12345 })
    expect(() => w.injectEvent({ type: 'demand_spike', start_tick: -1, duration_ticks: 5 })).toThrow()
  })
})

describe('demand_spike reverses on resolve', () => {
  it('multiplies affected stations while ACTIVE and restores exactly on RESOLVED', () => {
    const w = new World({ seed: 12345 })
    const before = w.stations().find((s) => s.id === 'station-mirpur')!.demand_multiplier

    w.injectEvent({ type: 'demand_spike', start_tick: 0, duration_ticks: 3, parameters: { region_ids: ['region-dhaka'], multiplier: 1.8 } })
    w.step()
    expect(w.stations().find((s) => s.id === 'station-mirpur')!.demand_multiplier).toBeCloseTo(before * 1.8, 9)
    expect(w.events()[0].status).toBe('ACTIVE')

    for (let i = 0; i < 3; i++) w.step()
    expect(w.stations().find((s) => s.id === 'station-mirpur')!.demand_multiplier).toBeCloseTo(before, 9)
    expect(w.events().find((e) => e.type === 'demand_spike')!.status).toBe('RESOLVED')
  })

  it('does not drive the multiplier below the 0.01 floor', () => {
    const w = new World({ seed: 12345 })
    w.injectEvent({ type: 'demand_spike', start_tick: 0, duration_ticks: 2, parameters: { multiplier: 0.01 } })
    w.step()
    expect(w.stations().every((s) => s.demand_multiplier >= 0.01)).toBe(true)
  })
})

describe('route_disruption, station_outage and depot_constraint reverse on resolve', () => {
  it('restores routes to AVAILABLE', () => {
    const w = new World({ seed: 12345 })
    w.injectEvent({ type: 'route_disruption', start_tick: 0, duration_ticks: 2, parameters: { route_ids: ['route-gazipur-mirpur'] } })
    w.step()
    expect(w.routes().find((r) => r.id === 'route-gazipur-mirpur')!.status).toBe('DISRUPTED')
    for (let i = 0; i < 2; i++) w.step()
    expect(w.routes().find((r) => r.id === 'route-gazipur-mirpur')!.status).toBe('AVAILABLE')
  })

  it('restores stations to OPEN', () => {
    const w = new World({ seed: 12345 })
    w.injectEvent({ type: 'station_outage', start_tick: 0, duration_ticks: 2, parameters: { station_ids: ['station-tongi'] } })
    w.step()
    expect(w.stations().find((s) => s.id === 'station-tongi')!.status).toBe('OUTAGE')
    for (let i = 0; i < 2; i++) w.step()
    expect(w.stations().find((s) => s.id === 'station-tongi')!.status).toBe('OPEN')
  })

  it('restores depots to OPEN', () => {
    const w = new World({ seed: 12345 })
    w.injectEvent({ type: 'depot_constraint', start_tick: 0, duration_ticks: 2, parameters: { depot_ids: ['depot-gazipur'] } })
    w.step()
    expect(w.depots().find((d) => d.id === 'depot-gazipur')!.status).toBe('CONSTRAINED')
    for (let i = 0; i < 2; i++) w.step()
    expect(w.depots().find((d) => d.id === 'depot-gazipur')!.status).toBe('OPEN')
  })

  it('applies to every entity when the filter list is empty', () => {
    const w = new World({ seed: 12345 })
    w.injectEvent({ type: 'route_disruption', start_tick: 0, duration_ticks: 2, parameters: { route_ids: [] } })
    w.step()
    expect(w.routes().every((r) => r.status === 'DISRUPTED')).toBe(true)
  })
})

describe('one-shot events do NOT auto-undo', () => {
  it('leaves a delayed shipment delayed after the event resolves', () => {
    const w = new World({ seed: 12345 })
    const target = w.supplyArrivals().find((a) => a.planned_tick === 12)!
    const original = target.planned_tick

    w.injectEvent({ type: 'shipment_delay', start_tick: 0, duration_ticks: 2, parameters: { delay_ticks: 5 } })
    w.step()
    const delayed = w.supplyArrivals().find((a) => a.id === target.id)!
    expect(delayed.status).toBe('DELAYED')
    expect(delayed.planned_tick).toBe(original + 5)

    for (let i = 0; i < 4; i++) w.step()
    const after = w.supplyArrivals().find((a) => a.id === target.id)!
    expect(after.planned_tick).toBe(original + 5)
  })

  it('leaves a shortfall quantity reduced after the event resolves', () => {
    const w = new World({ seed: 12345 })
    const target = w.supplyArrivals().find((a) => a.planned_tick === 12)!
    const original = target.quantity

    w.injectEvent({ type: 'supply_shortfall', start_tick: 0, duration_ticks: 2, parameters: { factor: 0.5 } })
    w.step()
    expect(w.supplyArrivals().find((a) => a.id === target.id)!.quantity).toBeCloseTo(original * 0.5, 6)

    for (let i = 0; i < 4; i++) w.step()
    expect(w.supplyArrivals().find((a) => a.id === target.id)!.quantity).toBeCloseTo(original * 0.5, 6)
  })
})

describe('events list', () => {
  it('returns events newest first', () => {
    const w = new World({ seed: 12345 })
    w.injectEvent({ type: 'demand_spike', start_tick: 0, duration_ticks: 1, parameters: {} })
    w.injectEvent({ type: 'route_disruption', start_tick: 0, duration_ticks: 1, parameters: {} })
    const ids = w.events().map((e) => e.id)
    expect(ids).toEqual([...ids].sort((a, b) => b - a))
  })

  it('returns an empty array before any event is injected', () => {
    expect(new World({ seed: 1 }).events()).toEqual([])
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm test -- src/lib/api/mock/world.events.test.ts`
Expected: FAIL — `w.injectEvent is not a function`.

- [ ] **Step 3: Implement events**

Add to `frontend/src/lib/api/mock/world.ts` the private fields and methods below.

Private fields:

```ts
  private eventRows: DomainEvent[] = []
  private nextEventId = 1
```

```ts
  events(): DomainEvent[] {
    return [...this.eventRows].sort((a, b) => b.id - a.id).map((e) => ({ ...e, parameters: { ...e.parameters } }))
  }

  injectEvent(req: EventRequest): DomainEvent {
    if (req.start_tick < 0) this.fail(422, 'VALIDATION_ERROR', 'start_tick must be >= 0.')
    if (req.duration_ticks <= 0) this.fail(422, 'VALIDATION_ERROR', 'duration_ticks must be > 0.')

    const event: DomainEvent = {
      id: this.nextEventId++,
      type: req.type,
      start_tick: req.start_tick,
      end_tick: req.start_tick + req.duration_ticks,
      status: 'SCHEDULED',
      parameters: { ...(req.parameters ?? {}) },
    }
    this.eventRows.push(event)

    // An event starting at or before the current tick takes effect at once.
    // An operator injecting a crisis expects to see it, not to wait for the
    // next tick; and a paused simulation would otherwise never apply it.
    if (event.start_tick <= this.tick) {
      this.applyEvent(event)
      event.status = 'ACTIVE'
    }

    return { ...event, parameters: { ...event.parameters } }
  }

  /**
   * Applies and reverses events by tick.
   *
   * Guide 7.8 splits the six types in two. demand_spike, route_disruption,
   * station_outage and depot_constraint reverse on resolve. shipment_delay and
   * supply_shortfall are one-shot: they mutate once and are never undone.
   */
  private processEvents(): void {
    for (const event of this.eventRows) {
      if (event.status === 'SCHEDULED' && this.tick >= event.start_tick) {
        this.applyEvent(event)
        event.status = 'ACTIVE'
      } else if (event.status === 'ACTIVE' && this.tick >= event.end_tick) {
        this.reverseEvent(event)
        event.status = 'RESOLVED'
      }
    }
  }

  private applyEvent(event: DomainEvent): void {
    const p = event.parameters

    switch (event.type) {
      case 'demand_spike': {
        const multiplier = num(p.multiplier, 1.5)
        for (const station of this.affectedStations(p)) {
          // Clamped so a repeated tiny multiplier can never reach zero and
          // divide-by-zero on reversal.
          station.demand_multiplier = Math.max(0.01, station.demand_multiplier * multiplier)
        }
        break
      }
      case 'route_disruption':
        for (const route of this.affectedRoutes(p)) this.setRouteStatus(route.id, 'DISRUPTED')
        break
      case 'station_outage':
        for (const station of this.affectedStations(p)) this.setStationStatus(station.id, 'OUTAGE')
        break
      case 'depot_constraint':
        for (const depot of this.affectedDepots(p)) this.setDepotStatus(depot.id, 'CONSTRAINED')
        break
      case 'shipment_delay': {
        const delayTicks = num(p.delay_ticks, 2)
        for (const arrival of this.affectedArrivals(p)) {
          if (arrival.status === 'ARRIVED') continue
          arrival.planned_tick += delayTicks
          arrival.status = 'DELAYED'
        }
        break
      }
      case 'supply_shortfall': {
        const factor = num(p.factor, 0.5)
        for (const arrival of this.affectedArrivals(p)) {
          if (arrival.status === 'ARRIVED') continue
          arrival.quantity = arrival.quantity * factor
        }
        break
      }
    }
  }

  private reverseEvent(event: DomainEvent): void {
    const p = event.parameters

    switch (event.type) {
      case 'demand_spike': {
        const multiplier = num(p.multiplier, 1.5)
        if (multiplier <= 0) break
        for (const station of this.affectedStations(p)) {
          station.demand_multiplier = Math.max(0.01, station.demand_multiplier / multiplier)
        }
        break
      }
      case 'route_disruption':
        for (const route of this.affectedRoutes(p)) this.setRouteStatus(route.id, 'AVAILABLE')
        break
      case 'station_outage':
        for (const station of this.affectedStations(p)) this.setStationStatus(station.id, 'OPEN')
        break
      case 'depot_constraint':
        for (const depot of this.affectedDepots(p)) this.setDepotStatus(depot.id, 'OPEN')
        break
      // shipment_delay and supply_shortfall are one-shot and deliberately
      // have no reversal branch.
      case 'shipment_delay':
      case 'supply_shortfall':
        break
    }
  }

  /** An empty filter list means "every entity of that type". Guide 7.8. */
  private affectedStations(p: Record<string, unknown>) {
    const ids = strArray(p.station_ids)
    const regionIds = strArray(p.region_ids)
    return this.stationsState.filter((s) => {
      if (ids.length > 0 && !ids.includes(s.id)) return false
      if (regionIds.length > 0 && !regionIds.includes(s.region_id)) return false
      return true
    })
  }

  private affectedRoutes(p: Record<string, unknown>) {
    const ids = strArray(p.route_ids)
    return this.routesState.filter((r) => ids.length === 0 || ids.includes(r.id))
  }

  private affectedDepots(p: Record<string, unknown>) {
    const ids = strArray(p.depot_ids)
    return this.depotsState.filter((d) => ids.length === 0 || ids.includes(d.id))
  }

  private affectedArrivals(p: Record<string, unknown>) {
    const depotIds = strArray(p.depot_ids)
    const fuelTypes = strArray(p.fuel_types)
    return this.arrivals.filter((a) => {
      if (depotIds.length > 0 && !depotIds.includes(a.depot_id)) return false
      if (fuelTypes.length > 0 && !fuelTypes.includes(a.fuel_type)) return false
      return true
    })
  }
```

Module-level helpers at the bottom of the file:

```ts
function num(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function strArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : []
}
```

Add the import of `DomainEvent` and `EventRequest` to the existing import statements, and insert `processEvents()` as the **first** line of `step()`:

```ts
  step(): void {
    this.tick += 1
    // Events resolve before anything else moves, so a disruption that starts
    // this tick already applies to this tick's arrivals and allocations.
    this.processEvents()
    this.processArrivals()
    this.processAllocations()
    this.observeTick()
  }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `npm test -- src/lib/api/mock/world.events.test.ts`
Expected: PASS, 14 tests.

- [ ] **Step 5: Run the whole suite**

Run: `npm test`
Expected: All pass. Task 8's allocation tests now run against real event handling, so their `station_outage` and `route_disruption` preconditions take effect for the first time.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/lib/api/mock
git commit -m "feat(frontend): implement six event types with correct reversal semantics"
```

---

## Task 10: World engine — faults

**Files:**
- Modify: `frontend/src/lib/api/mock/world.ts`
- Create: `frontend/src/lib/api/mock/world.faults.test.ts`

**Interfaces:**
- Consumes: Task 9's `World`
- Produces: `world.injectFault(req: FaultRequest): Fault`, `world.activeFaults(): Fault[]`, `world.clearFaults(): void`, `world.shouldFailRequest(): { code: 'FAULT_INJECTED'; message: string; status: number } | null`, `world.isStale(): boolean`, `world.isStreamDisconnected(): boolean`

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/api/mock/world.faults.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { World } from './world'

describe('fault injection', () => {
  it('creates an active fault expiring after its duration', () => {
    const w = new World({ seed: 12345 })
    const f = w.injectFault({ type: 'stale_data', duration_seconds: 60 })
    expect(f.active).toBe(true)
    expect(new Date(f.end_wall_time).getTime()).toBeGreaterThan(new Date(f.start_wall_time).getTime())
  })

  it('rejects a duration of zero or above one hour', () => {
    const w = new World({ seed: 12345 })
    expect(() => w.injectFault({ type: 'latency', duration_seconds: 0 })).toThrow()
    expect(() => w.injectFault({ type: 'latency', duration_seconds: 3601 })).toThrow()
  })

  it('clears every active fault', () => {
    const w = new World({ seed: 12345 })
    w.injectFault({ type: 'stale_data', duration_seconds: 60 })
    w.injectFault({ type: 'latency', duration_seconds: 60 })
    expect(w.activeFaults()).toHaveLength(2)
    w.clearFaults()
    expect(w.activeFaults()).toHaveLength(0)
  })

  it('treats an expired fault as inactive', () => {
    const w = new World({ seed: 12345 })
    w.injectFault({ type: 'unavailable', duration_seconds: 1 })
    // Force expiry without waiting on wall-clock time.
    w.expireFaults(Date.now() + 5000)
    expect(w.activeFaults()).toHaveLength(0)
    expect(w.shouldFailRequest()).toBeNull()
  })
})

describe('fault effects on /v1 paths', () => {
  it('unavailable always fails a request with 503', () => {
    const w = new World({ seed: 12345 })
    w.injectFault({ type: 'unavailable', duration_seconds: 60 })
    const failure = w.shouldFailRequest()
    expect(failure?.status).toBe(503)
    expect(failure?.code).toBe('FAULT_INJECTED')
  })

  it('error_rate fails some requests and passes others', () => {
    const w = new World({ seed: 12345 })
    w.injectFault({ type: 'error_rate', duration_seconds: 60, parameters: { rate: 0.5 } })
    const results = Array.from({ length: 400 }, () => w.shouldFailRequest() !== null)
    const failures = results.filter(Boolean).length
    expect(failures).toBeGreaterThan(0)
    expect(failures).toBeLessThan(results.length)
  })

  it('error_rate of zero never fails', () => {
    const w = new World({ seed: 12345 })
    w.injectFault({ type: 'error_rate', duration_seconds: 60, parameters: { rate: 0 } })
    expect(Array.from({ length: 200 }, () => w.shouldFailRequest()).every((r) => r === null)).toBe(true)
  })

  it('latency does not fail requests but reports a delay', () => {
    const w = new World({ seed: 12345 })
    w.injectFault({ type: 'latency', duration_seconds: 60, parameters: { delay_ms: 250 } })
    expect(w.shouldFailRequest()).toBeNull()
    expect(w.requestDelayMs()).toBe(250)
  })

  it('stale_data marks responses stale without failing them', () => {
    const w = new World({ seed: 12345 })
    w.injectFault({ type: 'stale_data', duration_seconds: 60 })
    expect(w.isStale()).toBe(true)
    expect(w.shouldFailRequest()).toBeNull()
  })

  it('stream_disconnect marks the stream disconnected without failing REST', () => {
    const w = new World({ seed: 12345 })
    w.injectFault({ type: 'stream_disconnect', duration_seconds: 60 })
    expect(w.isStreamDisconnected()).toBe(true)
    expect(w.shouldFailRequest()).toBeNull()
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm test -- src/lib/api/mock/world.faults.test.ts`
Expected: FAIL — `w.injectFault is not a function`.

- [ ] **Step 3: Implement faults**

Add to `frontend/src/lib/api/mock/world.ts`. Private fields:

```ts
  private faultRows: Fault[] = []
  private nextFaultId = 1
```

```ts
  injectFault(req: FaultRequest): Fault {
    if (req.duration_seconds <= 0 || req.duration_seconds > 3600) {
      this.fail(422, 'VALIDATION_ERROR', 'duration_seconds must be greater than 0 and at most 3600.')
    }
    const now = Date.now()
    const fault: Fault = {
      id: this.nextFaultId++,
      type: req.type,
      start_wall_time: new Date(now).toISOString(),
      end_wall_time: new Date(now + req.duration_seconds * 1000).toISOString(),
      active: true,
      parameters: { ...(req.parameters ?? {}) },
    }
    this.faultRows.push(fault)
    return { ...fault }
  }

  /** Test seam: ages faults out without waiting on wall-clock time. */
  expireFaults(nowMs: number = Date.now()): void {
    for (const fault of this.faultRows) {
      if (new Date(fault.end_wall_time).getTime() <= nowMs) fault.active = false
    }
  }

  activeFaults(): Fault[] {
    this.expireFaults()
    return this.faultRows.filter((f) => f.active).map((f) => ({ ...f }))
  }

  clearFaults(): void {
    for (const fault of this.faultRows) fault.active = false
  }

  private hasFault(type: FaultType): boolean {
    return this.activeFaults().some((f) => f.type === type)
  }

  private faultParams(type: FaultType): Record<string, unknown> {
    return this.activeFaults().find((f) => f.type === type)?.parameters ?? {}
  }

  /**
   * Whether a /v1 request should fail. Guide 7.10: unavailable always fails,
   * error_rate fails with the given probability, and the other three faults do
   * not fail requests at all. /admin/* and /v1/health bypass this entirely.
   */
  shouldFailRequest(): { code: 'FAULT_INJECTED'; message: string; status: number } | null {
    if (this.hasFault('unavailable')) {
      return { code: 'FAULT_INJECTED', status: 503, message: 'Simulator API temporarily unavailable.' }
    }
    if (this.hasFault('error_rate')) {
      const rate = clamp01(num(this.faultParams('error_rate').rate, 0.25))
      if (this.rng() < rate) {
        return { code: 'FAULT_INJECTED', status: 503, message: 'Injected transient API error.' }
      }
    }
    return null
  }

  requestDelayMs(): number {
    if (!this.hasFault('latency')) return 0
    return Math.max(0, num(this.faultParams('latency').delay_ms, 500))
  }

  isStale(): boolean { return this.hasFault('stale_data') }
  isStreamDisconnected(): boolean { return this.hasFault('stream_disconnect') }
```

Add `clamp01` to the module helpers:

```ts
function clamp01(value: number): number {
  return Math.min(1, Math.max(0, value))
}
```

Add `Fault`, `FaultRequest`, `FaultType` to the type imports.

- [ ] **Step 4: Run the test to verify it passes**

Run: `npm test -- src/lib/api/mock/world.faults.test.ts`
Expected: PASS, 11 tests.

- [ ] **Step 5: Run the whole suite and commit**

Run: `npm test`

```bash
git add frontend/src/lib/api/mock
git commit -m "feat(frontend): implement five fault types with correct /v1-only effects"
```

---

## Task 11: SSE-shaped event emitter

**Files:**
- Create: `frontend/src/lib/api/mock/emitter.ts`, `frontend/src/lib/api/mock/emitter.test.ts`

**Interfaces:**
- Consumes: Task 3 types
- Produces: `class MockEmitter` with `.emit(name: StreamEventName, data: unknown)`, `.subscribe(listener): () => void`, and `subscribe(listener, { maxQueue })` dropping the subscriber when it falls more than `maxQueue` events behind (Guide §6.1); `type StreamEventName`

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/api/mock/emitter.test.ts`:

```ts
import { describe, it, expect, vi } from 'vitest'
import { MockEmitter } from './emitter'

describe('MockEmitter', () => {
  it('delivers events to subscribers in order', () => {
    const emitter = new MockEmitter()
    const seen: string[] = []
    emitter.subscribe((e) => seen.push(e.name))
    emitter.emit('simulation.tick', { tick: 1 })
    emitter.emit('inventory.updated', { entity_id: 'depot-gazipur' })
    expect(seen).toEqual(['simulation.tick', 'inventory.updated'])
  })

  it('stops delivering after unsubscribe', () => {
    const emitter = new MockEmitter()
    const listener = vi.fn()
    const off = emitter.subscribe(listener)
    emitter.emit('simulation.tick', {})
    off()
    emitter.emit('simulation.tick', {})
    expect(listener).toHaveBeenCalledTimes(1)
  })

  it('supports several independent subscribers', () => {
    const emitter = new MockEmitter()
    const a = vi.fn()
    const b = vi.fn()
    emitter.subscribe(a)
    emitter.subscribe(b)
    emitter.emit('simulation.tick', {})
    expect(a).toHaveBeenCalledTimes(1)
    expect(b).toHaveBeenCalledTimes(1)
  })

  it('drops a subscriber that falls further behind than the queue allows', () => {
    // Guide 6.1: a queue of 200; falling behind by more silently drops you.
    const emitter = new MockEmitter()
    const slow = vi.fn()
    emitter.subscribe(slow, { maxQueue: 3 })

    // Backpressure: the listener cannot drain, so events accumulate.
    emitter.pauseDelivery()
    for (let i = 0; i < 5; i++) emitter.emit('simulation.tick', { tick: i })
    emitter.resumeDelivery()

    expect(slow.mock.calls.length).toBeLessThan(5)
    expect(emitter.subscriberCount()).toBe(0)
  })

  it('keeps other subscribers flowing when one is dropped', () => {
    const emitter = new MockEmitter()
    const slow = vi.fn()
    const fast = vi.fn()
    emitter.subscribe(slow, { maxQueue: 2 })
    emitter.subscribe(fast)

    emitter.pauseDelivery()
    for (let i = 0; i < 6; i++) emitter.emit('simulation.tick', { tick: i })
    emitter.resumeDelivery()

    expect(fast).toHaveBeenCalledTimes(6)
    expect(emitter.subscriberCount()).toBe(1)
  })

  it('never throws when a listener throws', () => {
    const emitter = new MockEmitter()
    const good = vi.fn()
    emitter.subscribe(() => { throw new Error('listener exploded') })
    emitter.subscribe(good)
    expect(() => emitter.emit('simulation.tick', {})).not.toThrow()
    expect(good).toHaveBeenCalledTimes(1)
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm test -- src/lib/api/mock/emitter.test.ts`
Expected: FAIL — cannot resolve `./emitter`.

- [ ] **Step 3: Implement the emitter**

`frontend/src/lib/api/mock/emitter.ts`:

```ts
/** Event names from Guide 6.3. */
export type StreamEventName =
  | 'simulation.tick'
  | 'allocation.status_changed'
  | 'inventory.updated'
  | 'simulator.notice'

export interface StreamEvent {
  name: StreamEventName
  data: unknown
  receivedAt: number
}

type Listener = (event: StreamEvent) => void

interface Subscriber {
  listener: Listener
  maxQueue: number
  queue: StreamEvent[]
  overflowed: boolean
}

const DEFAULT_MAX_QUEUE = 200

/**
 * A stand-in for the simulator's SSE stream.
 *
 * It reproduces the one behaviour from Guide 6.1 that callers must actually
 * handle: a subscriber whose queue exceeds its capacity is silently dropped,
 * and must reconnect and refetch. The other subscribers keep flowing.
 */
export class MockEmitter {
  private subscribers = new Set<Subscriber>()
  private delivering = true

  /** Simulates a stalled consumer without blocking the test thread. */
  pauseDelivery(): void { this.delivering = false }
  resumeDelivery(): void { this.drain() }

  subscriberCount(): number { return this.subscribers.size }

  subscribe(listener: Listener, options: { maxQueue?: number } = {}): () => void {
    const subscriber: Subscriber = {
      listener,
      maxQueue: options.maxQueue ?? DEFAULT_MAX_QUEUE,
      queue: [],
      overflowed: false,
    }
    this.subscribers.add(subscriber)
    return () => { this.subscribers.delete(subscriber) }
  }

  emit(name: StreamEventName, data: unknown): void {
    const event: StreamEvent = { name, data, receivedAt: Date.now() }
    for (const subscriber of this.subscribers) {
      subscriber.queue.push(event)
      if (subscriber.queue.length > subscriber.maxQueue) {
        // Guide 6.1: falling behind by more than the queue capacity drops the
        // subscriber. It is removed rather than stalling the emitter.
        subscriber.overflowed = true
      }
    }
    if (this.delivering) this.drain()
  }

  private drain(): void {
    for (const subscriber of [...this.subscribers]) {
      if (subscriber.overflowed) {
        this.subscribers.delete(subscriber)
        continue
      }
      const pending = subscriber.queue
      subscriber.queue = []
      for (const event of pending) {
        try {
          subscriber.listener(event)
        } catch {
          // One misbehaving listener must not stop delivery to the others.
        }
      }
    }
  }
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `npm test -- src/lib/api/mock/emitter.test.ts`
Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/api/mock/emitter.ts frontend/src/lib/api/mock/emitter.test.ts
git commit -m "feat(frontend): add SSE-shaped emitter with queue-overflow drop semantics"
```

---

## Task 12: Client interface and mock client

**Files:**
- Create: `frontend/src/lib/api/client.ts`, `frontend/src/lib/api/mock/mockClient.ts`, `frontend/src/lib/api/mock/mockClient.test.ts`

**Interfaces:**
- Consumes: Tasks 3–11
- Produces: `SimulatorClient` interface, `SimResponse<T>`, `StreamHandle`; `createMockClient(options?): SimulatorClient & { world: World }`

Every method returns `SimResponse<T>` so freshness travels with the data rather than being reconstructed later.

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/api/mock/mockClient.test.ts`:

```ts
import { describe, it, expect, vi } from 'vitest'
import { createMockClient } from './mockClient'
import { SimulatorError } from '../errors'

describe('mock client responses', () => {
  it('wraps data with a non-stale flag and a receipt time by default', async () => {
    const client = createMockClient({ seed: 12345 })
    const res = await client.getDepots()
    expect(res.stale).toBe(false)
    expect(res.receivedAt).toBeGreaterThan(0)
    expect(res.data).toHaveLength(2)
  })

  it('marks responses stale while a stale_data fault is active', async () => {
    const client = createMockClient({ seed: 12345 })
    client.world.injectFault({ type: 'stale_data', duration_seconds: 60 })
    expect((await client.getDepots()).stale).toBe(true)
  })

  it('rejects with FAULT_INJECTED while an unavailable fault is active', async () => {
    const client = createMockClient({ seed: 12345 })
    client.world.injectFault({ type: 'unavailable', duration_seconds: 60 })
    await expect(client.getDepots()).rejects.toBeInstanceOf(SimulatorError)
    await expect(client.getDepots()).rejects.toMatchObject({ code: 'FAULT_INJECTED', status: 503 })
  })

  it('does not fail getHealth even while unavailable, since health bypasses faults', async () => {
    const client = createMockClient({ seed: 12345 })
    client.world.injectFault({ type: 'unavailable', duration_seconds: 60 })
    await expect(client.getHealth()).resolves.toMatchObject({ data: { status: 'ok' } })
  })

  it('exposes every documented read endpoint', async () => {
    const client = createMockClient({ seed: 12345 })
    await expect(client.getInstance()).resolves.toBeTruthy()
    await expect(client.getRegions()).resolves.toBeTruthy()
    await expect(client.getStations()).resolves.toBeTruthy()
    await expect(client.getRoutes()).resolves.toBeTruthy()
    await expect(client.getSupplyArrivals()).resolves.toBeTruthy()
    await expect(client.getEvents()).resolves.toBeTruthy()
    await expect(client.getAllocations()).resolves.toBeTruthy()
    await expect(client.getDemandHistory({})).resolves.toBeTruthy()
    await expect(client.getMetrics()).resolves.toBeTruthy()
    await expect(client.getAudit()).resolves.toBeTruthy()
  })

  it('creates an allocation through the client', async () => {
    const client = createMockClient({ seed: 12345 })
    const res = await client.postAllocation({
      idempotency_key: 'c-1',
      source_depot_id: 'depot-gazipur',
      destination_station_id: 'station-mirpur',
      route_id: 'route-gazipur-mirpur',
      fuel_type: 'DIESEL',
      quantity: 3000,
    })
    expect(res.data.status).toBe('PENDING')
  })

  it('surfaces a validation failure as a SimulatorError with its code', async () => {
    const client = createMockClient({ seed: 12345 })
    await expect(
      client.postAllocation({
        idempotency_key: 'c-2',
        source_depot_id: 'depot-gazipur',
        destination_station_id: 'station-mirpur',
        route_id: 'route-gazipur-mirpur',
        fuel_type: 'DIESEL',
        quantity: 999999,
      }),
    ).rejects.toMatchObject({ code: 'ROUTE_CAPACITY_EXCEEDED' })
  })
})

describe('mock client streaming', () => {
  it('emits a tick event when the world steps', () => {
    const client = createMockClient({ seed: 12345 })
    const onTick = vi.fn()
    const handle = client.stream()
    handle.on('simulation.tick', onTick)
    client.world.step()
    client.publishTick()
    expect(onTick).toHaveBeenCalled()
  })

  it('reports the stream as unavailable while stream_disconnect is active', () => {
    const client = createMockClient({ seed: 12345 })
    client.world.injectFault({ type: 'stream_disconnect', duration_seconds: 60 })
    expect(() => client.stream()).toThrow(SimulatorError)
  })

  it('stops delivering after close and is safe to close twice', () => {
    const client = createMockClient({ seed: 12345 })
    const onTick = vi.fn()
    const handle = client.stream()
    handle.on('simulation.tick', onTick)
    handle.close()
    handle.close()
    client.publishTick()
    expect(onTick).not.toHaveBeenCalled()
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm test -- src/lib/api/mock/mockClient.test.ts`
Expected: FAIL — cannot resolve `./mockClient`.

- [ ] **Step 3: Write the client interface**

`frontend/src/lib/api/client.ts`:

```ts
import type {
  Allocation, AllocationRequest, AuditEntry, DemandObservation, Depot,
  DomainEvent, EventRequest, Fault, FaultRequest, Health, Metrics, Region,
  Route, SimInstance, Station, SupplyArrival,
} from '../types/simulator'
import type { StreamEventName } from './mock/emitter'

export interface StreamEvent { name: StreamEventName; data: unknown; receivedAt: number }

/**
 * Every read carries its own freshness.
 *
 * Guide 10.2 requires that a stale-data fault invalidate any local cache. By
 * putting `stale` on the response rather than in a side channel, a screen
 * physically cannot render stale data without having been handed the flag.
 */
export interface SimResponse<T> {
  data: T
  stale: boolean
  receivedAt: number
}

export interface StreamHandle {
  on(name: StreamEventName, listener: (event: StreamEvent) => void): () => void
  close(): void
}

export interface SimulatorClient {
  getHealth(): Promise<SimResponse<Health>>
  getInstance(): Promise<SimResponse<SimInstance>>
  getRegions(): Promise<SimResponse<Region[]>>
  getDepots(): Promise<SimResponse<Depot[]>>
  getStations(): Promise<SimResponse<Station[]>>
  getRoutes(): Promise<SimResponse<Route[]>>
  getSupplyArrivals(): Promise<SimResponse<SupplyArrival[]>>
  getEvents(): Promise<SimResponse<DomainEvent[]>>
  getAllocations(): Promise<SimResponse<Allocation[]>>
  getDemandHistory(params: { station_id?: string; limit?: number }): Promise<SimResponse<DemandObservation[]>>
  getMetrics(): Promise<SimResponse<Metrics>>

  postAllocation(body: AllocationRequest): Promise<SimResponse<Allocation>>
  cancelAllocation(id: number): Promise<SimResponse<Allocation>>

  stream(): StreamHandle

  adminRun(): Promise<SimResponse<SimInstance>>
  adminPause(): Promise<SimResponse<SimInstance>>
  adminStep(): Promise<SimResponse<{ tick: number; sim_time: string }>>
  adminReset(): Promise<SimResponse<{ status: string }>>
  adminInjectEvent(body: EventRequest): Promise<SimResponse<DomainEvent>>
  adminInjectFault(body: FaultRequest): Promise<SimResponse<Fault>>
  adminClearFaults(): Promise<SimResponse<{ status: string }>>
  adminGetFaults(): Promise<SimResponse<Fault[]>>
  adminGetEvents(): Promise<SimResponse<DomainEvent[]>>
  getAudit(limit?: number): Promise<SimResponse<AuditEntry[]>>
}
```

- [ ] **Step 4: Write the mock client**

`frontend/src/lib/api/mock/mockClient.ts`:

```ts
import type {
  Allocation, AllocationRequest, AuditEntry, DemandObservation, Depot, DomainEvent,
  EventRequest, Fault, FaultRequest, Health, Metrics, Region, Route, SimInstance,
  Station, SupplyArrival,
} from '../../types/simulator'
import type { SimulatorClient, SimResponse, StreamHandle } from '../client'
import { SimulatorError } from '../errors'
import { World, type WorldOptions } from './world'
import { MockEmitter, type StreamEventName } from './emitter'

export interface MockClient extends SimulatorClient {
  /** Exposed for the Scenario and Resilience screens, which drive the world directly. */
  world: World
  /** Emits a simulation.tick on the bus; the world has no timer of its own. */
  publishTick(): void
}

export function createMockClient(options: WorldOptions = {}): MockClient {
  const world = new World(options)
  const emitter = new MockEmitter()

  function ok<T>(data: T): SimResponse<T> {
    return { data, stale: world.isStale(), receivedAt: Date.now() }
  }

  /**
   * Mirrors the Guide's fault surface: /admin/* and /v1/health bypass fault
   * injection entirely, everything else is subject to it.
   */
  function guard(): void {
    const failure = world.shouldFailRequest()
    if (failure) throw new SimulatorError(failure.status, failure.code, failure.message)
  }

  async function v1<T>(produce: () => T): Promise<SimResponse<T>> {
    guard()
    const delay = world.requestDelayMs()
    if (delay > 0) await new Promise((resolve) => setTimeout(resolve, delay))
    return ok(produce())
  }

  async function admin<T>(produce: () => T): Promise<SimResponse<T>> {
    return ok(produce())
  }

  function notFound(entity: string, id: string): never {
    throw new SimulatorError(404, 'NOT_FOUND', `Unknown ${entity} "${id}".`)
  }

  return {
    world,
    publishTick() {
      emitter.emit('simulation.tick', { tick: world.tick, sim_time: world.simTime })
    },

    getHealth: () => admin<Health>(() => ({
      status: 'ok',
      database: 'ok',
      simulation: { status: world.status, tick: world.tick },
    })),
    getInstance: () => v1<SimInstance>(() => world.instance()),
    getRegions: () => v1<Region[]>(() => world.regions()),
    getDepots: () => v1<Depot[]>(() => world.depots()),
    getStations: () => v1<Station[]>(() => world.stations()),
    getRoutes: () => v1<Route[]>(() => world.routes()),
    getSupplyArrivals: () => v1<SupplyArrival[]>(() => world.supplyArrivals()),
    getEvents: () => v1<DomainEvent[]>(() => world.events()),
    getAllocations: () => v1<Allocation[]>(() => world.allocations()),
    getDemandHistory: (params) => v1<DemandObservation[]>(() => world.demandHistory(params)),
    getMetrics: () => v1<Metrics>(() => world.metrics()),

    postAllocation: (body: AllocationRequest) => v1<Allocation>(() => {
      const created = world.createAllocation(body)
      emitter.emit('allocation.status_changed', created)
      return created
    }),
    cancelAllocation: (id: number) => v1<Allocation>(() => {
      const cancelled = world.cancelAllocation(id)
      emitter.emit('allocation.status_changed', cancelled)
      return cancelled
    }),

    stream(): StreamHandle {
      // Guide 6.4: while stream_disconnect is active the stream returns 503
      // rather than opening.
      if (world.isStreamDisconnected()) {
        throw new SimulatorError(503, 'FAULT_INJECTED', 'Simulator stream temporarily unavailable.')
      }
      const subscriptions: Array<() => void> = []
      let closed = false
      return {
        on(name: StreamEventName, listener) {
          const off = emitter.subscribe((event) => {
            if (closed || event.name !== name) return
            listener(event)
          })
          subscriptions.push(off)
          return off
        },
        close() {
          if (closed) return
          closed = true
          subscriptions.forEach((off) => off())
          subscriptions.length = 0
        },
      }
    },

    adminRun: () => admin(() => { world.run(); return world.instance() }),
    adminPause: () => admin(() => { world.pause(); return world.instance() }),
    adminStep: () => admin(() => {
      world.step()
      emitter.emit('simulation.tick', { tick: world.tick, sim_time: world.simTime })
      return { tick: world.tick, sim_time: world.simTime }
    }),
    adminReset: () => admin(() => {
      world.reset()
      emitter.emit('simulator.notice', { message: 'Simulation reset' })
      return { status: 'reset' }
    }),
    adminInjectEvent: (body: EventRequest) => admin(() => world.injectEvent(body)),
    adminInjectFault: (body: FaultRequest) => admin(() => world.injectFault(body)),
    adminClearFaults: () => admin(() => { world.clearFaults(); return { status: 'cleared' } }),
    adminGetFaults: () => admin(() => world.activeFaults()),
    adminGetEvents: () => admin(() => world.events()),
    getAudit: (limit?: number) => admin(() => world.audit(limit)),
  }
}
```

- [ ] **Step 5: Add `reset()` and `audit()` to the World**

Add to `frontend/src/lib/api/mock/world.ts`:

```ts
  private auditRows: AuditEntry[] = []
  private nextAuditId = 1

  /** Guide 7.6: wipe everything and reload the scenario. */
  reset(): void {
    this.tick = 0
    this.status = 'PAUSED'
    this.observed = []
    this.nextObservationId = 1
    this.servedTotalLiters = 0
    this.unmetTotalLiters = 0
    this.allocationLiters = 0
    this.allocationFailures = 0
    this.allocationRows = []
    this.allocationKeys = new Map()
    this.nextAllocationId = 1
    this.eventRows = []
    this.nextEventId = 1
    this.faultRows = []
    this.nextFaultId = 1
    this.auditRows = []
    this.nextAuditId = 1

    this.regionsState = REGIONS.map((r) => ({ ...r }))
    this.depotsState = DEPOTS.map((d) => ({
      ...d, capacity: cloneFuelMap(d.capacity), inventory: cloneFuelMap(d.inventory),
    }))
    this.stationsState = STATIONS.map((s) => ({
      ...s, capacity: cloneFuelMap(s.capacity), inventory: cloneFuelMap(s.inventory),
    }))
    this.routesState = ROUTES.map((r) => ({ ...r }))
    this.arrivals = SUPPLY_SCHEDULE.map((a) => ({
      id: a.id, depot_id: a.depot_id, fuel_type: a.fuel_type, quantity: a.quantity,
      planned_tick: a.planned_tick, actual_tick: null, status: 'SCHEDULED' as const,
    }))
  }

  log(action: string, entityType: string, entityId: string, result = 'OK', metadata: Record<string, unknown> = {}): void {
    this.auditRows.push({
      id: this.nextAuditId++,
      wall_time: new Date().toISOString(),
      sim_time: this.simTime,
      tick: this.tick,
      action, entity_type: entityType, entity_id: entityId, result,
      metadata_json: metadata,
    })
    if (this.auditRows.length > 1000) this.auditRows = this.auditRows.slice(-1000)
  }

  /** Guide 7.12: limit clamped to [1, 1000], sorted id-desc. */
  audit(limit = 200): AuditEntry[] {
    const clamped = Math.min(1000, Math.max(1, Math.trunc(limit)))
    return [...this.auditRows].sort((a, b) => b.id - a.id).slice(0, clamped)
  }
```

Call `this.log(...)` at the end of `createAllocation` (`this.log('allocation.created', 'allocation', String(allocation.id), 'OK', { quantity: allocation.quantity, fuel_type: allocation.fuel_type })`), at the end of `cancelAllocation` (`this.log('allocation.cancelled', ...)`) and inside `injectEvent` (`this.log('event.created', 'event', String(event.id), 'OK', { type: event.type })`).

Add `AuditEntry` to the type imports.

- [ ] **Step 6: Run the test to verify it passes**

Run: `npm test -- src/lib/api/mock/mockClient.test.ts`
Expected: PASS, 11 tests.

- [ ] **Step 7: Run the whole suite and commit**

Run: `npm test`

```bash
git add frontend/src/lib/api
git commit -m "feat(frontend): add SimulatorClient interface and mock implementation"
```

---

## Task 13: HTTP client

**Files:**
- Create: `frontend/src/lib/api/http/httpClient.ts`, `frontend/src/lib/api/http/httpClient.test.ts`

**Interfaces:**
- Consumes: Task 12's `SimulatorClient`, `SimResponse`; Task 8's `SimulatorError`
- Produces: `createHttpClient({ baseUrl }): SimulatorClient`

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/api/http/httpClient.test.ts`:

```ts
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createHttpClient } from './httpClient'
import { SimulatorError } from '../errors'

function jsonResponse(body: unknown, init: { status?: number; headers?: Record<string, string> } = {}) {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
  })
}

describe('http client response handling', () => {
  beforeEach(() => { vi.stubGlobal('fetch', vi.fn()) })
  afterEach(() => { vi.unstubAllGlobals() })

  it('unwraps a successful body', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse([{ id: 'depot-gazipur' }]))
    const client = createHttpClient({ baseUrl: 'http://localhost:8000' })
    const res = await client.getDepots()
    expect(res.data).toEqual([{ id: 'depot-gazipur' }])
    expect(res.stale).toBe(false)
  })

  it('reads the X-Simulator-Stale header', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse([], { headers: { 'X-Simulator-Stale': 'true' } }),
    )
    const client = createHttpClient({ baseUrl: 'http://localhost:8000' })
    expect((await client.getDepots()).stale).toBe(true)
  })

  it('normalises the {"detail":{code,message}} envelope', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse({ detail: { code: 'ROUTE_DISRUPTED', message: 'Route is DISRUPTED.' } }, { status: 409 }),
    )
    const client = createHttpClient({ baseUrl: 'http://localhost:8000' })
    await expect(client.postAllocation({
      idempotency_key: 'x', source_depot_id: 'a', destination_station_id: 'b',
      route_id: 'c', fuel_type: 'DIESEL', quantity: 1,
    })).rejects.toMatchObject({ code: 'ROUTE_DISRUPTED', status: 409, message: 'Route is DISRUPTED.' })
  })

  it('normalises the {"error":{code,message}} envelope used by injected faults', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(
        { error: { code: 'FAULT_INJECTED', message: 'Simulator API temporarily unavailable.' } },
        { status: 503 },
      ),
    )
    const client = createHttpClient({ baseUrl: 'http://localhost:8000' })
    await expect(client.getDepots()).rejects.toMatchObject({
      code: 'FAULT_INJECTED', status: 503,
    })
  })

  it('normalises FastAPI validation errors, whose detail is an array', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse(
        { detail: [{ loc: ['body', 'quantity'], msg: 'ensure this value is greater than 0', type: 'value_error' }] },
        { status: 422 },
      ),
    )
    const client = createHttpClient({ baseUrl: 'http://localhost:8000' })
    await expect(client.postAllocation({
      idempotency_key: 'x', source_depot_id: 'a', destination_station_id: 'b',
      route_id: 'c', fuel_type: 'DIESEL', quantity: 0,
    })).rejects.toMatchObject({ code: 'VALIDATION_ERROR', status: 422 })
  })

  it('survives a non-JSON error body without throwing a parse error', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response('<html>502 Bad Gateway</html>', { status: 502 }),
    )
    const client = createHttpClient({ baseUrl: 'http://localhost:8000' })
    await expect(client.getDepots()).rejects.toBeInstanceOf(SimulatorError)
    await expect(client.getDepots()).rejects.toMatchObject({ status: 502 })
  })

  it('reports a network failure as a SimulatorError rather than a raw TypeError', async () => {
    vi.mocked(fetch).mockRejectedValue(new TypeError('Failed to fetch'))
    const client = createHttpClient({ baseUrl: 'http://localhost:8000' })
    await expect(client.getDepots()).rejects.toMatchObject({ code: 'NETWORK_ERROR' })
  })

  it('builds the documented URL for demand history', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse([]))
    const client = createHttpClient({ baseUrl: 'http://localhost:8000' })
    await client.getDemandHistory({ station_id: 'station-mirpur', limit: 50 })
    const url = vi.mocked(fetch).mock.calls[0]![0] as string
    expect(url).toContain('/v1/demand-history')
    expect(url).toContain('station_id=station-mirpur')
    expect(url).toContain('limit=50')
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm test -- src/lib/api/http/httpClient.test.ts`
Expected: FAIL — cannot resolve `./httpClient`.

- [ ] **Step 3: Implement the HTTP client**

`frontend/src/lib/api/http/httpClient.ts`:

```ts
import type { SimulatorClient, SimResponse, StreamHandle } from '../client'
import type { StreamEventName } from '../mock/emitter'
import { SimulatorError } from '../errors'

export interface HttpOptions { baseUrl: string }

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

/**
 * Normalises the Guide's three distinct error envelopes (Guide 9) into one
 * SimulatorError. Getting this wrong turns a clear 409 into `undefined`, so
 * each shape is handled explicitly rather than probed with a generic unwrap.
 */
function toSimulatorError(status: number, body: unknown): SimulatorError {
  // Injected faults: {"error": {"code", "message"}}
  if (isRecord(body) && isRecord(body.error)) {
    const code = typeof body.error.code === 'string' ? body.error.code : 'FAULT_INJECTED'
    const message = typeof body.error.message === 'string' ? body.error.message : 'Simulator fault.'
    return new SimulatorError(status, code, message)
  }
  // Allocation and domain errors: {"detail": {"code", "message"}}
  if (isRecord(body) && isRecord(body.detail)) {
    const code = typeof body.detail.code === 'string' ? body.detail.code : `HTTP_${status}`
    const message = typeof body.detail.message === 'string' ? body.detail.message : 'Request failed.'
    return new SimulatorError(status, code, message)
  }
  // FastAPI validation: {"detail": [ { loc, msg, type }, ... ]}
  if (isRecord(body) && Array.isArray(body.detail)) {
    const first = body.detail[0]
    const msg = isRecord(first) && typeof first.msg === 'string'
      ? first.msg
      : 'Request failed validation.'
    return new SimulatorError(status, 'VALIDATION_ERROR', msg)
  }
  return new SimulatorError(status, `HTTP_${status}`, `Request failed with status ${status}.`)
}

export function createHttpClient(options: HttpOptions): SimulatorClient {
  const base = options.baseUrl.replace(/\/+$/, '')

  async function request<T>(path: string, init?: RequestInit): Promise<SimResponse<T>> {
    let response: Response
    try {
      response = await fetch(`${base}${path}`, {
        ...init,
        headers: {
          ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
          ...init?.headers,
        },
      })
    } catch (cause) {
      // A transport failure is a different condition from an HTTP error and
      // must not surface as an unhandled TypeError.
      throw new SimulatorError(0, 'NETWORK_ERROR',
        cause instanceof Error ? cause.message : 'Network request failed.')
    }

    const stale = response.headers.get('X-Simulator-Stale') === 'true'
    const text = await response.text()

    let body: unknown = null
    if (text) {
      try {
        body = JSON.parse(text)
      } catch {
        // A non-JSON body (a proxy error page, say) must not throw a parse
        // error that masks the real status.
        if (!response.ok) throw new SimulatorError(response.status, `HTTP_${response.status}`,
          `Request failed with status ${response.status}.`)
        throw new SimulatorError(response.status, 'INVALID_RESPONSE', 'Response was not valid JSON.')
      }
    }

    if (!response.ok) throw toSimulatorError(response.status, body)
    return { data: body as T, stale, receivedAt: Date.now() }
  }

  const get = <T,>(path: string) => request<T>(path)
  const post = <T,>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', ...(body === undefined ? {} : { body: JSON.stringify(body) }) })

  return {
    getHealth: () => get('/v1/health'),
    getInstance: () => get('/v1/instance'),
    getRegions: () => get('/v1/regions'),
    getDepots: () => get('/v1/depots'),
    getStations: () => get('/v1/stations'),
    getRoutes: () => get('/v1/routes'),
    getSupplyArrivals: () => get('/v1/supply-arrivals'),
    getEvents: () => get('/v1/events'),
    getAllocations: () => get('/v1/allocations'),
    getDemandHistory: (params) => {
      const query = new URLSearchParams()
      if (params.station_id) query.set('station_id', params.station_id)
      if (params.limit !== undefined) query.set('limit', String(params.limit))
      const suffix = query.toString()
      return get(`/v1/demand-history${suffix ? `?${suffix}` : ''}`)
    },
    getMetrics: () => get('/v1/metrics'),

    postAllocation: (body) => post('/v1/allocations', body),
    cancelAllocation: (id) => post(`/v1/allocations/${id}/cancel`),

    stream(): StreamHandle {
      const source = new EventSource(`${base}/v1/stream`)
      const listeners = new Map<StreamEventName, Set<(e: { name: StreamEventName; data: unknown; receivedAt: number }) => void>>()

      const names: StreamEventName[] = [
        'simulation.tick', 'allocation.status_changed', 'inventory.updated', 'simulator.notice',
      ]
      for (const name of names) {
        source.addEventListener(name, (event) => {
          const message = event as MessageEvent<string>
          let data: unknown = null
          try { data = JSON.parse(message.data) } catch { data = message.data }
          const payload = { name, data, receivedAt: Date.now() }
          listeners.get(name)?.forEach((listener) => listener(payload))
        })
      }

      return {
        on(name, listener) {
          const set = listeners.get(name) ?? new Set()
          set.add(listener)
          listeners.set(name, set)
          return () => set.delete(listener)
        },
        close() {
          source.close()
          listeners.clear()
        },
      }
    },

    adminRun: () => post('/admin/run'),
    adminPause: () => post('/admin/pause'),
    adminStep: () => post('/admin/step'),
    adminReset: () => post('/admin/reset'),
    adminInjectEvent: (body) => post('/admin/events', body),
    adminInjectFault: (body) => post('/admin/faults', body),
    adminClearFaults: () => post('/admin/faults/clear'),
    adminGetFaults: () => get('/admin/faults'),
    adminGetEvents: () => get('/admin/events'),
    getAudit: (limit) => get(`/admin/audit${limit === undefined ? '' : `?limit=${limit}`}`),
  }
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `npm test -- src/lib/api/http/httpClient.test.ts`
Expected: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/api/http
git commit -m "feat(frontend): add HTTP client with three-envelope error normalisation"
```

---

## Task 14: Client selector and environment wiring

**Files:**
- Create: `frontend/src/lib/api/index.ts`, `frontend/src/lib/api/index.test.ts`

**Interfaces:**
- Consumes: Tasks 12–13
- Produces: `api: SimulatorClient`, `createApi(mode, baseUrl?): SimulatorClient`, `SIMULATOR_MODE`

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/api/index.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { createApi } from './index'

describe('createApi', () => {
  it('returns a working mock client in mock mode', async () => {
    const api = createApi('mock')
    await expect(api.getDepots()).resolves.toMatchObject({ data: expect.any(Array) })
  })

  it('returns an http client in live mode', () => {
    const api = createApi('live', 'http://localhost:8000')
    // Distinguishable by the mock-only world handle.
    expect((api as { world?: unknown }).world).toBeUndefined()
  })

  it('treats an unrecognised mode as mock rather than crashing', () => {
    const api = createApi('nonsense' as 'mock')
    expect((api as { world?: unknown }).world).toBeDefined()
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm test -- src/lib/api/index.test.ts`
Expected: FAIL — cannot resolve `./index`.

- [ ] **Step 3: Implement the selector**

`frontend/src/lib/api/index.ts`:

```ts
import type { SimulatorClient } from './client'
import { createMockClient, type MockClient } from './mock/mockClient'
import { createHttpClient } from './http/httpClient'

export type SimulatorMode = 'mock' | 'live'

const DEFAULT_BASE_URL = 'http://localhost:8000'

export function createApi(mode: SimulatorMode | string, baseUrl?: string): SimulatorClient {
  // An unrecognised mode falls back to mock rather than throwing: a typo in an
  // environment variable must not leave the operator with a blank console.
  if (mode === 'live') {
    return createHttpClient({ baseUrl: baseUrl ?? DEFAULT_BASE_URL })
  }
  return createMockClient()
}

export const SIMULATOR_MODE: SimulatorMode =
  import.meta.env.VITE_SIMULATOR_MODE === 'live' ? 'live' : 'mock'

export const api: SimulatorClient = createApi(
  SIMULATOR_MODE,
  import.meta.env.VITE_SIMULATOR_BASE_URL,
)

/** Narrowing helper for the Scenario and Resilience screens, which need the world. */
export function asMockClient(client: SimulatorClient): MockClient | null {
  return (client as MockClient).world ? (client as MockClient) : null
}

export type { SimulatorClient, SimResponse, StreamHandle } from './client'
export { SimulatorError } from './errors'
```

Create `frontend/src/vite-env.d.ts` so `import.meta.env` is typed:

```ts
/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_SIMULATOR_MODE?: string
  readonly VITE_SIMULATOR_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `npm test -- src/lib/api/index.test.ts`
Expected: PASS, 3 tests.

- [ ] **Step 5: Verify the layering rule still holds**

Run: `npm run lint`
Expected: pass. Note that `lib/api/index.ts` importing from `mock/` is allowed — the restriction applies to `features/**`.

If ESLint reports the restriction firing on `lib/api/index.ts`, narrow the rule's files to `src/features/**/*.{ts,tsx}`.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/lib/api/index.ts frontend/src/lib/api/index.test.ts frontend/src/vite-env.d.ts
git commit -m "feat(frontend): add simulator client selector driven by VITE_SIMULATOR_MODE"
```

---

## Task 15: Formatting utilities

**Files:**
- Create: `frontend/src/lib/format.ts`, `frontend/src/lib/format.test.ts`

**Interfaces:**
- Consumes: nothing
- Produces: `formatLiters`, `formatPercent`, `formatTick`, `formatSimTime`, `formatHours`, `formatDelta`, `tickToSimDate`

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/format.test.ts`:

```ts
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm test -- src/lib/format.test.ts`
Expected: FAIL — cannot resolve `./format`.

- [ ] **Step 3: Implement the formatters**

`frontend/src/lib/format.ts`:

```ts
const EM_DASH = '—'

/**
 * Every formatter returns an em dash for a non-finite input rather than the
 * string "NaN". A missing measurement must read as missing, not as a value.
 */
function finite(value: number): boolean {
  return Number.isFinite(value)
}

export function formatLiters(value: number, options: { compact?: boolean } = {}): string {
  if (!finite(value)) return EM_DASH
  const normalized = value === 0 ? 0 : value // collapses -0
  if (options.compact) {
    const abs = Math.abs(normalized)
    if (abs >= 1_000_000) return `${(normalized / 1_000_000).toFixed(1)}M L`
    if (abs >= 1_000) return `${(normalized / 1_000).toFixed(1)}k L`
  }
  return `${Math.round(normalized).toLocaleString('en-US')} L`
}

export function formatPercent(fraction: number, decimals = 0): string {
  if (!finite(fraction)) return EM_DASH
  return `${(fraction * 100).toFixed(decimals)}%`
}

export function tickToSimDate(tick: number, tickMinutes: number): Date {
  return new Date(new Date('2026-01-01T00:00:00.000Z').getTime() + tick * tickMinutes * 60_000)
}

function clockTime(date: Date): string {
  const hh = String(date.getUTCHours()).padStart(2, '0')
  const mm = String(date.getUTCMinutes()).padStart(2, '0')
  return `${hh}:${mm}`
}

export function formatTick(tick: number, tickMinutes: number): string {
  if (!finite(tick)) return EM_DASH
  return `T${tick} · ${clockTime(tickToSimDate(tick, tickMinutes))}`
}

export function formatSimTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return EM_DASH
  return clockTime(date)
}

export function formatHours(hours: number): string {
  if (!finite(hours)) return EM_DASH
  if (Math.abs(hours) < 1) return `${Math.round(hours * 60)} min`
  return `${hours.toFixed(1)} h`
}

/** Renders a before/after pair, as used by the decision-impact card. */
export function formatDelta(before: number, after: number, decimals = 0): string {
  return `${formatPercent(before, decimals)} → ${formatPercent(after, decimals)}`
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `npm test -- src/lib/format.test.ts`
Expected: PASS, 16 tests.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/format.ts frontend/src/lib/format.test.ts
git commit -m "feat(frontend): add formatters that render missing values as em dashes"
```

---

## Task 16: Data hooks — query, freshness, stream, health

**Files:**
- Create: `frontend/src/lib/hooks/useSimulatorQuery.ts`, `frontend/src/lib/hooks/useFreshness.ts`, `frontend/src/lib/hooks/useStream.ts`, `frontend/src/lib/hooks/useHealth.ts`, `frontend/src/lib/hooks/hooks.test.tsx`, `frontend/src/lib/hooks/ApiProvider.tsx`

**Interfaces:**
- Consumes: Task 14's `api`
- Produces: `<ApiProvider client>`, `useApi()`, `useSimulatorQuery(key, fetcher, options?)` returning `{ data, error, loading, stale, receivedAt, refresh }`, `useFreshness(receivedAt)`, `useStream()`, `useHealth()`

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/hooks/hooks.test.tsx`:

```tsx
import { render, screen, waitFor, act } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { ApiProvider } from './ApiProvider'
import { useSimulatorQuery } from './useSimulatorQuery'
import { createMockClient } from '../api/mock/mockClient'
import type { Depot } from '../types/simulator'

function DepotList() {
  const { data, loading, error, stale, refresh } = useSimulatorQuery<Depot[]>(
    'depots',
    (client) => client.getDepots(),
  )
  if (loading) return <p>loading</p>
  if (error) return <p role="alert">{error.message}</p>
  return (
    <div>
      <span data-testid="stale">{String(stale)}</span>
      <span data-testid="count">{data?.length ?? 0}</span>
      <ul>{data?.map((d) => <li key={d.id}>{d.name}</li>)}</ul>
      <button onClick={refresh}>refresh</button>
    </div>
  )
}

function wrap(client: ReturnType<typeof createMockClient>) {
  return render(<ApiProvider client={client}><DepotList /></ApiProvider>)
}

describe('useSimulatorQuery', () => {
  it('loads data and clears the loading flag', async () => {
    wrap(createMockClient({ seed: 12345 }))
    expect(screen.getByText('loading')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('count')).toHaveTextContent('2'))
  })

  it('surfaces a fault as an error rather than an empty list', async () => {
    const client = createMockClient({ seed: 12345 })
    client.world.injectFault({ type: 'unavailable', duration_seconds: 60 })
    wrap(client)
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())
  })

  it('reports staleness from the response', async () => {
    const client = createMockClient({ seed: 12345 })
    client.world.injectFault({ type: 'stale_data', duration_seconds: 60 })
    wrap(client)
    await waitFor(() => expect(screen.getByTestId('stale')).toHaveTextContent('true'))
  })

  it('refetches on demand', async () => {
    const client = createMockClient({ seed: 12345 })
    const spy = vi.spyOn(client, 'getDepots')
    wrap(client)
    await waitFor(() => expect(screen.getByTestId('count')).toHaveTextContent('2'))
    await act(async () => { screen.getByRole('button', { name: 'refresh' }).click() })
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(2))
  })

  it('does not render stale data as fresh after an error', async () => {
    const client = createMockClient({ seed: 12345 })
    wrap(client)
    await waitFor(() => expect(screen.getByTestId('count')).toHaveTextContent('2'))
    client.world.injectFault({ type: 'unavailable', duration_seconds: 60 })
    await act(async () => { screen.getByRole('button', { name: 'refresh' }).click() })
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm test -- src/lib/hooks/hooks.test.tsx`
Expected: FAIL — cannot resolve `./ApiProvider`.

- [ ] **Step 3: Write the provider**

`frontend/src/lib/hooks/ApiProvider.tsx`:

```tsx
import { createContext, useContext, type ReactNode } from 'react'
import type { SimulatorClient } from '../api/client'

const ApiContext = createContext<SimulatorClient | null>(null)

export function ApiProvider({
  client, children,
}: { client: SimulatorClient; children: ReactNode }) {
  return <ApiContext.Provider value={client}>{children}</ApiContext.Provider>
}

export function useApi(): SimulatorClient {
  const client = useContext(ApiContext)
  if (!client) throw new Error('useApi must be used inside an ApiProvider')
  return client
}
```

- [ ] **Step 4: Write the query hook**

`frontend/src/lib/hooks/useSimulatorQuery.ts`:

```ts
import { useCallback, useEffect, useRef, useState } from 'react'
import type { SimulatorClient, SimResponse } from '../api/client'
import { SimulatorError } from '../api/errors'
import { useApi } from './ApiProvider'

export interface QueryState<T> {
  data: T | null
  error: SimulatorError | null
  loading: boolean
  stale: boolean
  receivedAt: number | null
  refresh: () => void
}

/**
 * Loads one resource from the simulator.
 *
 * `refreshKey` lets a caller re-run the query when something external changes —
 * an SSE event, a manual world step, an operator action — without the hook
 * needing to know what that something was.
 */
export function useSimulatorQuery<T>(
  key: string,
  fetcher: (client: SimulatorClient) => Promise<SimResponse<T>>,
  options: { refreshKey?: number; enabled?: boolean } = {},
): QueryState<T> {
  const client = useApi()
  const enabled = options.enabled ?? true
  const refreshKey = options.refreshKey ?? 0

  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<SimulatorError | null>(null)
  const [loading, setLoading] = useState(enabled)
  const [stale, setStale] = useState(false)
  const [receivedAt, setReceivedAt] = useState<number | null>(null)
  const [manualNonce, setManualNonce] = useState(0)

  // Guards against a slow response from an earlier request overwriting a
  // newer one, which is exactly what a latency fault produces.
  const requestId = useRef(0)

  useEffect(() => {
    if (!enabled) {
      setLoading(false)
      return
    }
    const id = ++requestId.current
    let cancelled = false

    setLoading(true)
    fetcher(client)
      .then((response) => {
        if (cancelled || id !== requestId.current) return
        setData(response.data)
        setStale(response.stale)
        setReceivedAt(response.receivedAt)
        setError(null)
      })
      .catch((cause: unknown) => {
        if (cancelled || id !== requestId.current) return
        // The previous data is deliberately kept so a degraded screen can show
        // the last known state, but `error` is set so it is never presented as
        // current.
        setError(
          cause instanceof SimulatorError
            ? cause
            : new SimulatorError(0, 'UNKNOWN', cause instanceof Error ? cause.message : 'Unknown error'),
        )
      })
      .finally(() => {
        if (cancelled || id !== requestId.current) return
        setLoading(false)
      })

    return () => { cancelled = true }
    // `fetcher` is intentionally not a dependency: callers pass inline arrows,
    // which would refetch on every render. `key` identifies the resource.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, key, enabled, refreshKey, manualNonce])

  const refresh = useCallback(() => setManualNonce((n) => n + 1), [])

  return { data, error, loading, stale, receivedAt, refresh }
}
```

- [ ] **Step 5: Write the freshness, stream and health hooks**

`frontend/src/lib/hooks/useFreshness.ts`:

```ts
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
```

`frontend/src/lib/hooks/useStream.ts`:

```ts
import { useEffect, useRef, useState } from 'react'
import type { StreamHandle } from '../api/client'
import { SimulatorError } from '../api/errors'
import { useApi } from './ApiProvider'

export type StreamStatus = 'connecting' | 'connected' | 'reconnecting' | 'unavailable'

/**
 * Owns the SSE connection.
 *
 * Guide 6.2: there is no Last-Event-ID replay, so a reconnect only delivers
 * events from that moment forward. `onReconnect` is therefore not a nicety —
 * it is the only way the app learns what it missed.
 */
export function useStream(options: { onEvent?: (name: string) => void; onReconnect?: () => void } = {}) {
  const client = useApi()
  const [status, setStatus] = useState<StreamStatus>('connecting')
  const [lastEventAt, setLastEventAt] = useState<number | null>(null)
  const [error, setError] = useState<SimulatorError | null>(null)

  const handlers = useRef(options)
  handlers.current = options

  useEffect(() => {
    let handle: StreamHandle | null = null
    let disposed = false

    const connect = (isReconnect: boolean) => {
      if (disposed) return
      setStatus(isReconnect ? 'reconnecting' : 'connecting')
      try {
        handle = client.stream()
      } catch (cause) {
        setStatus('unavailable')
        setError(cause instanceof SimulatorError ? cause : null)
        return
      }
      setStatus('connected')
      setError(null)
      if (isReconnect) handlers.current.onReconnect?.()

      handle.on('simulation.tick', () => {
        setLastEventAt(Date.now())
        handlers.current.onEvent?.('simulation.tick')
      })
      handle.on('allocation.status_changed', () => {
        setLastEventAt(Date.now())
        handlers.current.onEvent?.('allocation.status_changed')
      })
      handle.on('inventory.updated', () => {
        setLastEventAt(Date.now())
        handlers.current.onEvent?.('inventory.updated')
      })
      handle.on('simulator.notice', () => {
        setLastEventAt(Date.now())
        handlers.current.onEvent?.('simulator.notice')
      })
    }

    const onVisibility = () => { if (!document.hidden) connect(true) }
    document.addEventListener('visibilitychange', onVisibility)
    connect(false)

    return () => {
      disposed = true
      document.removeEventListener('visibilitychange', onVisibility)
      handle?.close()
    }
  }, [client])

  return { status, lastEventAt, error }
}
```

`frontend/src/lib/hooks/useHealth.ts`:

```ts
import { useSimulatorQuery } from './useSimulatorQuery'
import type { Health } from '../types/simulator'

export type SystemState = 'normal' | 'degraded' | 'offline'

export interface HealthState {
  state: SystemState
  health: Health | null
  error: Error | null
  refresh: () => void
}

/**
 * Polls liveness and reachability, which are two different questions.
 *
 * Guide 7.10 exempts /v1/health from fault injection, so health alone reports
 * "normal" for the entire duration of an `unavailable` fault — exactly when
 * the degraded-mode behaviour is being judged. Health is therefore paired with
 * a real data route:
 *
 *   health ok  + data ok   -> normal
 *   health ok  + data fails -> degraded  (the API is up; requests are being faulted)
 *   health fails            -> offline   (the simulator is genuinely gone)
 */
export function useHealth(): HealthState {
  const health = useSimulatorQuery<Health>('health', (client) => client.getHealth())
  const probe = useSimulatorQuery<unknown>('health-probe', (client) => client.getInstance())

  const state: SystemState = health.error
    ? 'offline'
    : probe.error
      ? 'degraded'
      : 'normal'

  return { state, health: health.data, error: health.error ?? probe.error, refresh: health.refresh }
}
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `npm test -- src/lib/hooks/hooks.test.tsx`
Expected: PASS, 5 tests.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/lib/hooks
git commit -m "feat(frontend): add simulator query, freshness, stream and health hooks"
```

---

## Task 17: Icon set

**Files:**
- Create: `frontend/src/icons/index.tsx`, `frontend/src/icons/icons.test.tsx`

**Interfaces:**
- Consumes: nothing
- Produces: an `Icon` wrapper plus named glyphs — `CheckCircle`, `AlertTriangle`, `XOctagon`, `Clock`, `Fuel`, `Depot`, `Station`, `Route`, `Activity`, `Shield`, `Gauge`, `Scroll`, `Play`, `Pause`, `StepForward`, `RotateCcw`, `Sun`, `Moon`, `Wifi`, `WifiOff`, `ChevronRight`, `Search`, `Plus`, `X`, `ArrowRight`

- [ ] **Step 1: Write the failing test**

`frontend/src/icons/icons.test.tsx`:

```tsx
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm test -- src/icons/icons.test.tsx`
Expected: FAIL — cannot resolve `./index`.

- [ ] **Step 3: Implement the icon set**

`frontend/src/icons/index.tsx`:

```tsx
import type { SVGProps } from 'react'

export interface IconProps extends SVGProps<SVGSVGElement> {
  size?: number
  /** Set when the icon is the only label for a control. */
  title?: string
}

/**
 * One wrapper for the whole set, so stroke width, cap style and sizing stay
 * consistent. Icons are decorative by default and hidden from assistive
 * technology; a control that is icon-only supplies its own aria-label.
 */
function Icon({ size = 16, title, children, ...rest }: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden={title ? undefined : true}
      role={title ? 'img' : undefined}
      focusable="false"
      {...rest}
    >
      {title ? <title>{title}</title> : null}
      {children}
    </svg>
  )
}

const paths = {
  CheckCircle: <><circle cx="12" cy="12" r="9" /><path d="m8.5 12.5 2.5 2.5 4.5-5" /></>,
  AlertTriangle: <><path d="M10.3 3.9 2.6 17.2A2 2 0 0 0 4.3 20h15.4a2 2 0 0 0 1.7-2.8L13.7 3.9a2 2 0 0 0-3.4 0Z" /><path d="M12 9v4" /><path d="M12 17h.01" /></>,
  XOctagon: <><path d="M7.9 2h8.2L22 7.9v8.2L16.1 22H7.9L2 16.1V7.9Z" /><path d="m15 9-6 6" /><path d="m9 9 6 6" /></>,
  Clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
  Fuel: <><path d="M3 22h12" /><path d="M5 22V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v17" /><path d="M5 12h8" /><path d="M17 8h2a2 2 0 0 1 2 2v6a1.5 1.5 0 0 0 3 0v-7l-3-4" /></>,
  Depot: <><path d="M3 21V9l9-6 9 6v12" /><path d="M9 21v-7h6v7" /><path d="M3 21h18" /></>,
  Station: <><path d="M12 21s7-5.6 7-11a7 7 0 1 0-14 0c0 5.4 7 11 7 11Z" /><circle cx="12" cy="10" r="2.5" /></>,
  Route: <><circle cx="6" cy="19" r="2.5" /><circle cx="18" cy="5" r="2.5" /><path d="M8.5 19H14a4 4 0 0 0 0-8H9a4 4 0 0 1 0-8h0" /></>,
  Activity: <path d="M3 12h4l3-8 4 16 3-8h4" />,
  Shield: <><path d="M12 22s8-3.5 8-10V5l-8-3-8 3v7c0 6.5 8 10 8 10Z" /></>,
  Gauge: <><path d="M12 21a9 9 0 1 0-9-9" /><path d="m12 12 4-4" /><circle cx="12" cy="12" r="1.5" /></>,
  Scroll: <><path d="M6 4h11a2 2 0 0 1 2 2v12a2 2 0 0 0 2 2H8a2 2 0 0 1-2-2Z" /><path d="M6 4a2 2 0 0 0-2 2v2h2" /><path d="M9 9h6" /><path d="M9 13h6" /></>,
  Play: <path d="M7 4.5v15l12-7.5Z" />,
  Pause: <><path d="M9 4.5v15" /><path d="M15 4.5v15" /></>,
  StepForward: <><path d="M5 4.5v15l10-7.5Z" /><path d="M19 4.5v15" /></>,
  RotateCcw: <><path d="M3 12a9 9 0 1 0 2.6-6.4" /><path d="M3 4v5h5" /></>,
  Sun: <><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></>,
  Moon: <path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z" />,
  Wifi: <><path d="M2.5 9a15 15 0 0 1 19 0" /><path d="M5.5 12.5a11 11 0 0 1 13 0" /><path d="M8.5 16a7 7 0 0 1 7 0" /><path d="M12 19.5h.01" /></>,
  WifiOff: <><path d="m2 2 20 20" /><path d="M8.5 16a7 7 0 0 1 7 0" /><path d="M5.5 12.5a11 11 0 0 1 4-2.4" /><path d="M14.5 10.1a11 11 0 0 1 5 2.4" /><path d="M2.5 9a15 15 0 0 1 6-3.7" /><path d="M15.5 5.4A15 15 0 0 1 21.5 9" /><path d="M12 19.5h.01" /></>,
  ChevronRight: <path d="m9 5 7 7-7 7" />,
  Search: <><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></>,
  Plus: <><path d="M12 5v14" /><path d="M5 12h14" /></>,
  X: <><path d="m6 6 12 12" /><path d="m18 6-12 12" /></>,
  ArrowRight: <><path d="M4 12h16" /><path d="m14 6 6 6-6 6" /></>,
} as const

export type IconName = keyof typeof paths

export const CheckCircle = (p: IconProps) => <Icon {...p}>{paths.CheckCircle}</Icon>
export const AlertTriangle = (p: IconProps) => <Icon {...p}>{paths.AlertTriangle}</Icon>
export const XOctagon = (p: IconProps) => <Icon {...p}>{paths.XOctagon}</Icon>
export const Clock = (p: IconProps) => <Icon {...p}>{paths.Clock}</Icon>
export const Fuel = (p: IconProps) => <Icon {...p}>{paths.Fuel}</Icon>
export const Depot = (p: IconProps) => <Icon {...p}>{paths.Depot}</Icon>
export const Station = (p: IconProps) => <Icon {...p}>{paths.Station}</Icon>
export const Route = (p: IconProps) => <Icon {...p}>{paths.Route}</Icon>
export const Activity = (p: IconProps) => <Icon {...p}>{paths.Activity}</Icon>
export const Shield = (p: IconProps) => <Icon {...p}>{paths.Shield}</Icon>
export const Gauge = (p: IconProps) => <Icon {...p}>{paths.Gauge}</Icon>
export const Scroll = (p: IconProps) => <Icon {...p}>{paths.Scroll}</Icon>
export const Play = (p: IconProps) => <Icon {...p}>{paths.Play}</Icon>
export const Pause = (p: IconProps) => <Icon {...p}>{paths.Pause}</Icon>
export const StepForward = (p: IconProps) => <Icon {...p}>{paths.StepForward}</Icon>
export const RotateCcw = (p: IconProps) => <Icon {...p}>{paths.RotateCcw}</Icon>
export const Sun = (p: IconProps) => <Icon {...p}>{paths.Sun}</Icon>
export const Moon = (p: IconProps) => <Icon {...p}>{paths.Moon}</Icon>
export const Wifi = (p: IconProps) => <Icon {...p}>{paths.Wifi}</Icon>
export const WifiOff = (p: IconProps) => <Icon {...p}>{paths.WifiOff}</Icon>
export const ChevronRight = (p: IconProps) => <Icon {...p}>{paths.ChevronRight}</Icon>
export const Search = (p: IconProps) => <Icon {...p}>{paths.Search}</Icon>
export const Plus = (p: IconProps) => <Icon {...p}>{paths.Plus}</Icon>
export const X = (p: IconProps) => <Icon {...p}>{paths.X}</Icon>
export const ArrowRight = (p: IconProps) => <Icon {...p}>{paths.ArrowRight}</Icon>
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `npm test -- src/icons/icons.test.tsx`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/icons
git commit -m "feat(frontend): add consistent 24px stroke icon set"
```

---

## Task 18: Shared UI primitives

**Files:**
- Create: `frontend/src/components/Panel.tsx`, `frontend/src/components/StatusPill.tsx`, `frontend/src/components/StatTile.tsx`, `frontend/src/components/DataTable.tsx`, `frontend/src/components/EmptyState.tsx`, `frontend/src/components/Skeleton.tsx`, `frontend/src/components/ErrorState.tsx`, `frontend/src/components/FreshnessBadge.tsx`, `frontend/src/components/primitives.test.tsx`

**Interfaces:**
- Consumes: Tasks 2, 16, 17
- Produces: `<Panel title actions>`, `<StatusPill status>`, `<StatTile label value unit>`, `<DataTable columns rows getRowKey>`, `<EmptyState title description action>`, `<Skeleton variant>`, `<ErrorState error onRetry>`, `<FreshnessBadge receivedAt stale>`

- [ ] **Step 1: Write the failing test**

`frontend/src/components/primitives.test.tsx`:

```tsx
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, it, expect, vi } from 'vitest'
import { Panel } from './Panel'
import { StatusPill } from './StatusPill'
import { StatTile } from './StatTile'
import { DataTable, type Column } from './DataTable'
import { EmptyState } from './EmptyState'
import { ErrorState } from './ErrorState'
import { FreshnessBadge } from './FreshnessBadge'
import { SimulatorError } from '../lib/api/errors'

describe('Panel', () => {
  it('renders a heading and its content', () => {
    render(<Panel title="Depot inventory"><p>body</p></Panel>)
    expect(screen.getByRole('heading', { name: 'Depot inventory' })).toBeInTheDocument()
    expect(screen.getByText('body')).toBeInTheDocument()
  })
})

describe('StatusPill', () => {
  it('carries a text label as well as colour, never colour alone', () => {
    render(<StatusPill status="OPEN" kind="depot" />)
    expect(screen.getByText('Open')).toBeInTheDocument()
  })

  it('renders a distinguishable glyph per status', () => {
    const { container: open } = render(<StatusPill status="OPEN" kind="depot" />)
    const { container: outage } = render(<StatusPill status="OUTAGE" kind="station" />)
    expect(open.querySelector('svg')!.innerHTML).not.toBe(outage.querySelector('svg')!.innerHTML)
  })

  it('renders a constrained depot with the warning tone, not the critical one', () => {
    const { container } = render(<StatusPill status="CONSTRAINED" kind="depot" />)
    // CONSTRAINED is still shippable; the colour must not overstate it.
    expect(container.firstChild).toHaveAttribute('data-tone', 'warning')
  })

  it('renders a fallback for an unrecognised status rather than crashing', () => {
    render(<StatusPill status={'SOMETHING_NEW' as 'OPEN'} kind="depot" />)
    expect(screen.getByText('SOMETHING_NEW')).toBeInTheDocument()
  })
})

describe('StatTile', () => {
  it('renders a labelled value with tabular figures', () => {
    render(<StatTile label="Service level" value="98%" />)
    expect(screen.getByText('Service level')).toBeInTheDocument()
    expect(screen.getByText('98%')).toHaveClass('mono')
  })

  it('renders a placeholder when the value is unavailable', () => {
    render(<StatTile label="Unmet demand" value={null} />)
    expect(screen.getByText('—')).toBeInTheDocument()
  })
})

describe('DataTable', () => {
  interface Row { id: string; qty: number }

  const columns: Array<Column<Row>> = [
    { key: 'id', header: 'ID', render: (r: Row) => r.id },
    { key: 'qty', header: 'Quantity', numeric: true, render: (r: Row) => String(r.qty) },
  ]

  it('renders a header row and one row per record', () => {
    render(<DataTable columns={columns} rows={[{ id: 'a', qty: 1 }, { id: 'b', qty: 2 }]} getRowKey={(r) => r.id} caption="Loads" />)
    expect(screen.getAllByRole('columnheader')).toHaveLength(2)
    expect(screen.getAllByRole('row')).toHaveLength(3) // header + 2
  })

  it('renders an empty state instead of an empty table body', () => {
    render(<DataTable columns={columns} rows={[]} getRowKey={(r: Row) => r.id} emptyMessage="No allocations yet" />)
    expect(screen.getByText('No allocations yet')).toBeInTheDocument()
  })

  it('does not crash on an undefined rows prop', () => {
    render(<DataTable columns={columns} rows={undefined} getRowKey={(r: Row) => r.id} emptyMessage="Nothing" />)
    expect(screen.getByText('Nothing')).toBeInTheDocument()
  })
})

describe('EmptyState', () => {
  it('explains the emptiness rather than leaving a blank panel', () => {
    render(<EmptyState title="No alerts" description="No station is projected to stock out." />)
    expect(screen.getByText('No alerts')).toBeInTheDocument()
    expect(screen.getByText('No station is projected to stock out.')).toBeInTheDocument()
  })
})

describe('ErrorState', () => {
  it('states the cause and offers a retry', async () => {
    const onRetry = vi.fn()
    render(<ErrorState error={new SimulatorError(409, 'ROUTE_DISRUPTED', 'Route is DISRUPTED.')} onRetry={onRetry} />)
    expect(screen.getByText('Route is DISRUPTED.')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /retry/i }))
    expect(onRetry).toHaveBeenCalled()
  })

  it('announces itself to assistive technology', () => {
    render(<ErrorState error={new SimulatorError(503, 'FAULT_INJECTED', 'Unavailable.')} />)
    expect(screen.getByRole('alert')).toBeInTheDocument()
  })
})

describe('FreshnessBadge', () => {
  it('reports recent data as current', () => {
    render(<FreshnessBadge receivedAt={Date.now()} stale={false} />)
    expect(screen.getByText(/just now/i)).toBeInTheDocument()
  })

  it('flags stale data explicitly', () => {
    render(<FreshnessBadge receivedAt={Date.now() - 60_000} stale />)
    expect(screen.getByText(/stale/i)).toBeInTheDocument()
  })

  it('renders without data rather than claiming freshness', () => {
    render(<FreshnessBadge receivedAt={null} stale={false} />)
    expect(screen.queryByText(/just now/i)).not.toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm test -- src/components/primitives.test.tsx`
Expected: FAIL — cannot resolve `./Panel`.

- [ ] **Step 3: Write Panel, StatusPill and StatTile**

`frontend/src/components/Panel.tsx`:

```tsx
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
```

`frontend/src/components/StatusPill.tsx`:

```tsx
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
  // the screen.
  const tone = STATUS_TONES[status] ?? 'neutral'
  const Glyph = TONE_ICON[tone]
  const padding = size === 'sm' ? 'px-1.5 py-0.5 text-[11px]' : 'px-2 py-0.5 text-[11px]'

  return (
    <span
      data-tone={tone}
      data-kind={kind}
      className={`inline-flex items-center gap-1 rounded-pill border font-medium ${TONE_CLASS[tone]} ${padding}`}
    >
      <Glyph />
      {humanize(status)}
    </span>
  )
}
```

`frontend/src/components/StatTile.tsx`:

```tsx
import type { ReactNode } from 'react'

export interface StatTileProps {
  label: string
  value: string | number | null
  unit?: string
  hint?: string
  trend?: ReactNode
  tone?: 'default' | 'good' | 'warning' | 'critical'
}

const TONE_TEXT: Record<string, string> = {
  default: 'text-ink',
  good: 'text-status-good',
  warning: 'text-status-warning',
  critical: 'text-status-critical',
}

/**
 * A single headline figure. Per spec 9.6, one number is a stat tile, not a
 * chart of one bar.
 */
export function StatTile({ label, value, unit, hint, trend, tone = 'default' }: StatTileProps) {
  const display = value === null || value === undefined || value === '' ? '—' : value

  return (
    <div className="rounded-panel border border-hairline bg-surface px-3.5 py-3">
      <div className="text-[11px] font-medium uppercase tracking-wide text-subtle">{label}</div>
      <div className="mt-1 flex items-baseline gap-1.5">
        <span className={`mono text-[22px] leading-7 font-semibold ${TONE_TEXT[tone]}`}>{display}</span>
        {unit ? <span className="text-[12px] text-muted">{unit}</span> : null}
      </div>
      {hint ? <div className="mt-0.5 text-[12px] text-muted">{hint}</div> : null}
      {trend ? <div className="mt-1">{trend}</div> : null}
    </div>
  )
}
```

- [ ] **Step 4: Write DataTable, EmptyState, Skeleton, ErrorState, FreshnessBadge**

`frontend/src/components/DataTable.tsx`:

```tsx
import type { ReactNode } from 'react'

export interface Column<T> {
  key: string
  header: string
  numeric?: boolean
  render: (row: T) => ReactNode
}

export interface DataTableProps<T> {
  columns: Array<Column<T>>
  rows: T[] | undefined
  getRowKey: (row: T) => string | number
  caption?: string
  emptyMessage?: string
  minWidth?: number
}

/**
 * A dense table. `rows` is typed as possibly undefined because a real
 * simulator can return nothing where the guide's example always has data;
 * an absent collection renders an empty state rather than crashing on .map.
 */
export function DataTable<T>({
  columns, rows, getRowKey, caption, emptyMessage = 'No records', minWidth = 640,
}: DataTableProps<T>) {
  const safeRows = Array.isArray(rows) ? rows : []

  if (safeRows.length === 0) {
    return <p className="px-1 py-6 text-center text-[13px] text-muted">{emptyMessage}</p>
  }

  return (
    <div className="-mx-4 overflow-x-auto px-4">
      <table className="w-full border-collapse text-[13px]" style={{ minWidth }}>
        {caption ? <caption className="sr-only">{caption}</caption> : null}
        <thead>
          <tr className="border-b border-hairline">
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                className={`px-2.5 py-2 text-[11px] font-medium uppercase tracking-wide text-subtle ${
                  column.numeric ? 'text-right' : 'text-left'
                }`}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {safeRows.map((row) => (
            <tr key={getRowKey(row)} className="border-b border-hairline last:border-0 hover:bg-surface-raised">
              {columns.map((column) => (
                <td
                  key={column.key}
                  className={`px-2.5 py-2 align-middle text-ink ${column.numeric ? 'mono text-right' : ''}`}
                >
                  {column.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
```

`frontend/src/components/EmptyState.tsx`:

```tsx
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
```

`frontend/src/components/Skeleton.tsx`:

```tsx
export function Skeleton({
  variant = 'line', count = 1,
}: { variant?: 'line' | 'block' | 'tile'; count?: number }) {
  const height = variant === 'tile' ? 'h-20' : variant === 'block' ? 'h-32' : 'h-4'
  return (
    <div role="status" aria-label="Loading" className="flex flex-col gap-2">
      {Array.from({ length: count }, (_, i) => (
        <div
          key={i}
          className={`${height} w-full animate-pulse rounded-control bg-surface-raised`}
          style={variant === 'line' && i === count - 1 ? { width: '60%' } : undefined}
        />
      ))}
    </div>
  )
}
```

`frontend/src/components/ErrorState.tsx`:

```tsx
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
          className="cursor-pointer rounded-control border border-hairline-strong px-2.5 py-1 text-[12px] font-medium text-ink transition-colors duration-150 hover:bg-surface-raised"
        >
          Retry
        </button>
      ) : null}
    </div>
  )
}
```

`frontend/src/components/FreshnessBadge.tsx`:

```tsx
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
      <span className="inline-flex items-center gap-1 text-[11px] font-medium text-status-warning">
        Stale · {label}
      </span>
    )
  }

  return <span className="text-[11px] text-subtle">Updated {label}</span>
}
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `npm test -- src/components/primitives.test.tsx`
Expected: PASS, 17 tests.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components
git commit -m "feat(frontend): add shared panel, status, stat, table and state primitives"
```

---

## Task 19: App shell — navigation, top bar, status strip

**Files:**
- Create: `frontend/src/app/shell/navigation.ts`, `frontend/src/app/shell/SimulationClock.tsx`, `frontend/src/app/shell/StatusStrip.tsx`, `frontend/src/app/shell/DegradedBanner.tsx`, `frontend/src/app/shell/SideNav.tsx`, `frontend/src/app/shell/TopBar.tsx`, `frontend/src/app/shell/AppShell.tsx`, `frontend/src/app/shell/AppShell.test.tsx`

**Interfaces:**
- Consumes: Tasks 2, 16, 17, 18
- Produces: `<AppShell>`; `NAV_ITEMS: NavItem[]` where `NavItem = { to, label, icon, primary? }`

- [ ] **Step 1: Write the failing test**

`frontend/src/app/shell/AppShell.test.tsx`:

```tsx
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi } from 'vitest'
import { AppShell } from './AppShell'
import { ApiProvider } from '../../lib/hooks/ApiProvider'
import { ThemeProvider } from '../../design/theme'
import { createMockClient } from '../../lib/api/mock/mockClient'
import { SimulatorError } from '../../lib/api/errors'

function renderShell(client = createMockClient({ seed: 12345 })) {
  return render(
    <ThemeProvider>
      <ApiProvider client={client}>
        <MemoryRouter initialEntries={['/dashboard']}>
          <AppShell />
        </MemoryRouter>
      </ApiProvider>
    </ThemeProvider>,
  )
}

describe('AppShell', () => {
  it('renders the primary navigation with text labels, not icon-only', () => {
    renderShell()
    const nav = screen.getByRole('navigation', { name: /primary/i })
    expect(nav).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /dashboard/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /network/i })).toBeInTheDocument()
  })

  it('marks the current location in the navigation', () => {
    renderShell()
    expect(screen.getByRole('link', { name: /dashboard/i })).toHaveAttribute('aria-current', 'page')
  })

  it('offers a skip link to the main region', () => {
    renderShell()
    expect(screen.getByRole('link', { name: /skip to main content/i })).toBeInTheDocument()
  })

  it('marks the console as simulated', () => {
    renderShell()
    expect(screen.getByText(/simulated/i)).toBeInTheDocument()
  })

  it('shows the simulation clock controls', () => {
    renderShell()
    expect(screen.getByRole('button', { name: /step/i })).toBeInTheDocument()
  })

  it('exposes a theme toggle with an accessible name', async () => {
    renderShell()
    const toggle = screen.getByRole('button', { name: /theme/i })
    await userEvent.click(toggle)
    expect(document.documentElement).toHaveAttribute('data-theme', 'light')
  })

  it('raises a degraded banner when data routes are faulted but health still answers', async () => {
    // Guide 7.10 exempts /v1/health from faults, so this state is only
    // detectable by also probing a real data route.
    const client = createMockClient({ seed: 12345 })
    client.world.injectFault({ type: 'unavailable', duration_seconds: 60 })
    renderShell(client)
    await waitFor(() =>
      expect(screen.getByText(/responding slowly or intermittently/i)).toBeInTheDocument(),
    )
  })

  it('raises the offline banner when health itself fails', async () => {
    const client = createMockClient({ seed: 12345 })
    vi.spyOn(client, 'getHealth').mockRejectedValue(
      new SimulatorError(0, 'NETWORK_ERROR', 'Network request failed.'),
    )
    renderShell(client)
    await waitFor(() =>
      expect(screen.getByText(/simulator is not responding/i)).toBeInTheDocument(),
    )
  })

  it('does not show a degraded banner while healthy', () => {
    renderShell()
    expect(screen.queryByText(/responding slowly or intermittently/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/simulator is not responding/i)).not.toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm test -- src/app/shell/AppShell.test.tsx`
Expected: FAIL — cannot resolve `./AppShell`.

- [ ] **Step 3: Write the navigation model**

`frontend/src/app/shell/navigation.ts`:

```ts
import { Activity, Depot, Fuel, Gauge, Route, Scroll, Shield, Station } from '../../icons'
import type { ComponentType } from 'react'

export interface NavItem {
  to: string
  label: string
  icon: ComponentType<{ size?: number }>
  /** Shown in the mobile bottom bar. Material caps bottom nav at five items. */
  primary?: boolean
}

/**
 * Twelve destinations from spec 8. The first five are `primary` because they
 * appear in the mobile bottom bar; the rest live behind the overflow sheet.
 * Screens marked "(planned)" render a stub until Plans 2 and 3 land.
 */
export const NAV_ITEMS: NavItem[] = [
  { to: '/dashboard', label: 'Dashboard', icon: Gauge, primary: true },
  { to: '/network', label: 'Network', icon: Route, primary: true },
  { to: '/risk', label: 'Risk & Alerts', icon: Shield, primary: true },
  { to: '/allocations', label: 'Allocations', icon: Fuel, primary: true },
  { to: '/inventory', label: 'Inventory', icon: Depot, primary: true },
  { to: '/explain', label: 'Decisions', icon: Scroll },
  { to: '/scenarios', label: 'Scenarios', icon: Activity },
  { to: '/observability', label: 'Observability', icon: Gauge },
  { to: '/resilience', label: 'Resilience', icon: Shield },
  { to: '/audit', label: 'Audit', icon: Scroll },
  { to: '/loadtest', label: 'Load Test', icon: Station },
  { to: '/', label: 'Overview', icon: Depot },
]
```

- [ ] **Step 4: Write the simulation clock and status strip**

`frontend/src/app/shell/SimulationClock.tsx`:

```tsx
import { useState } from 'react'
import { Pause, Play, RotateCcw, StepForward } from '../../icons'
import { useApi } from '../../lib/hooks/ApiProvider'
import { useSimulatorQuery } from '../../lib/hooks/useSimulatorQuery'
import { formatSimTime, formatTick } from '../../lib/format'
import type { SimInstance } from '../../lib/types/simulator'

/**
 * The simulation clock and its controls.
 *
 * Manual stepping is given equal billing with run/pause because Guide 7.5
 * identifies it as the recommended way to drive a deterministic demo: pause,
 * issue allocations, then step a known number of ticks for a reproducible
 * result.
 */
export function SimulationClock({ onChanged }: { onChanged?: () => void }) {
  const api = useApi()
  const { data, refresh } = useSimulatorQuery<SimInstance>('instance', (c) => c.getInstance())
  const [busy, setBusy] = useState(false)

  const running = data?.status === 'RUNNING'

  async function act(action: () => Promise<unknown>) {
    setBusy(true)
    try {
      await action()
      refresh()
      onChanged?.()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex items-center gap-2">
      <div className="hidden items-baseline gap-2 sm:flex">
        <span className="mono text-[12px] font-medium text-ink">
          {data ? formatTick(data.tick, data.tick_minutes) : '—'}
        </span>
        <span className="mono text-[11px] text-subtle">
          {data ? formatSimTime(data.sim_time) : '—'}
        </span>
      </div>

      <div className="flex items-center gap-1">
        <ClockButton
          label={running ? 'Pause simulation' : 'Run simulation'}
          disabled={busy || !data}
          onClick={() => act(() => (running ? api.adminPause() : api.adminRun()))}
        >
          {running ? <Pause size={14} /> : <Play size={14} />}
        </ClockButton>
        <ClockButton
          label="Step one tick"
          disabled={busy || !data}
          onClick={() => act(() => api.adminStep())}
        >
          <StepForward size={14} />
        </ClockButton>
        <ClockButton
          label="Reset simulation"
          disabled={busy || !data}
          onClick={() => act(() => api.adminReset())}
        >
          <RotateCcw size={14} />
        </ClockButton>
      </div>
    </div>
  )
}

function ClockButton({
  label, disabled, onClick, children,
}: { label: string; disabled: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onClick}
      className="inline-flex h-8 w-8 cursor-pointer items-center justify-center rounded-control border border-hairline text-muted transition-colors duration-150 hover:bg-surface-raised hover:text-ink disabled:cursor-not-allowed disabled:opacity-40"
    >
      {children}
    </button>
  )
}
```

`frontend/src/app/shell/StatusStrip.tsx`:

```tsx
import { Wifi, WifiOff } from '../../icons'
import { useHealth } from '../../lib/hooks/useHealth'
import { useStream } from '../../lib/hooks/useStream'

const STREAM_LABEL = {
  connecting: 'Connecting',
  connected: 'Live',
  reconnecting: 'Reconnecting',
  unavailable: 'Stream down',
} as const

/**
 * The always-visible system state strip.
 *
 * It carries three facts an operator must never have to hunt for: whether the
 * platform is talking to the simulator, whether the change stream is live, and
 * that everything on screen is simulated (Brief 24 requires the distinction to
 * be explicit).
 */
export function StatusStrip({ onStreamEvent }: { onStreamEvent?: () => void }) {
  const { state } = useHealth()
  const { status } = useStream({ onEvent: onStreamEvent })

  const stateTone =
    state === 'normal' ? 'text-status-good'
      : state === 'degraded' ? 'text-status-warning'
        : 'text-status-critical'

  return (
    <div className="flex items-center gap-3 border-t border-hairline bg-surface px-3 py-1.5 text-[11px]">
      <span className={`inline-flex items-center gap-1.5 font-medium ${stateTone}`}>
        <span className="h-1.5 w-1.5 rounded-pill bg-current" aria-hidden="true" />
        Simulator {state === 'normal' ? 'healthy' : state}
      </span>

      <span className="inline-flex items-center gap-1.5 text-muted">
        {status === 'connected' ? <Wifi size={12} /> : <WifiOff size={12} />}
        {STREAM_LABEL[status]}
      </span>

      <span className="ml-auto inline-flex items-center gap-1.5 rounded-pill border border-hairline-strong px-2 py-0.5 font-medium text-subtle">
        SIMULATED
      </span>
    </div>
  )
}
```

- [ ] **Step 5: Write the degraded banner**

`frontend/src/app/shell/DegradedBanner.tsx`:

```tsx
import { AlertTriangle } from '../../icons'
import type { SystemState } from '../../lib/hooks/useHealth'

/**
 * Names the lost capability and what replaced it (Brief 11). The brief scores
 * degraded behaviour at 10%, and a banner that only says "error" does not
 * demonstrate anything — it has to say which capability is gone.
 */
export function DegradedBanner({ state }: { state: SystemState }) {
  if (state === 'normal') return null

  const offline = state === 'offline'

  return (
    <div
      role="status"
      aria-live="polite"
      className={`flex items-start gap-2 border-b px-4 py-2 text-[12px] ${
        offline
          ? 'border-status-critical/40 bg-status-critical/10 text-status-critical'
          : 'border-status-warning/40 bg-status-warning/10 text-status-warning'
      }`}
    >
      <AlertTriangle size={14} />
      <div>
        <p className="font-medium">
          {offline
            ? 'The simulator is not responding.'
            : 'The simulator is responding slowly or intermittently.'}
        </p>
        <p className="text-muted">
          {offline
            ? 'Showing the last known state. Predictions and allocations are paused until the connection returns.'
            : 'Live updates may lag. Figures shown carry their age; retries are in progress.'}
        </p>
      </div>
    </div>
  )
}
```

- [ ] **Step 6: Write the side nav, top bar and shell**

`frontend/src/app/shell/SideNav.tsx`:

```tsx
import { NavLink } from 'react-router-dom'
import { NAV_ITEMS } from './navigation'

export function SideNav() {
  return (
    <nav aria-label="Primary" className="hidden w-56 shrink-0 border-r border-hairline bg-surface md:block">
      <ul className="flex flex-col gap-0.5 p-2">
        {NAV_ITEMS.map(({ to, label, icon: Glyph }) => (
          <li key={to}>
            <NavLink
              to={to}
              className={({ isActive }) =>
                `flex items-center gap-2.5 rounded-control px-2.5 py-2 text-[13px] transition-colors duration-150 ${
                  isActive
                    ? 'bg-surface-raised font-medium text-ink'
                    : 'text-muted hover:bg-surface-raised hover:text-ink'
                }`
              }
            >
              <Glyph size={16} />
              {label}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  )
}
```

`frontend/src/app/shell/TopBar.tsx`:

```tsx
import { Moon, Sun } from '../../icons'
import { useTheme } from '../../design/theme'
import { SimulationClock } from './SimulationClock'

export function TopBar({ onWorldChanged }: { onWorldChanged?: () => void }) {
  const { theme, toggle } = useTheme()

  return (
    <header className="flex h-14 shrink-0 items-center gap-3 border-b border-hairline bg-surface px-4">
      <div className="flex items-baseline gap-2">
        <span className="text-[14px] font-semibold tracking-tight text-ink">Fuel Supply</span>
        <span className="hidden text-[11px] text-subtle sm:inline">
          Intelligence &amp; Resilience
        </span>
      </div>

      <div className="ml-auto flex items-center gap-3">
        <SimulationClock onChanged={onWorldChanged} />
        <button
          type="button"
          aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
          title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
          onClick={toggle}
          className="inline-flex h-8 w-8 cursor-pointer items-center justify-center rounded-control border border-hairline text-muted transition-colors duration-150 hover:bg-surface-raised hover:text-ink"
        >
          {theme === 'dark' ? <Sun size={14} /> : <Moon size={14} />}
        </button>
      </div>
    </header>
  )
}
```

`frontend/src/app/shell/AppShell.tsx`:

```tsx
import { useCallback, useState } from 'react'
import { Outlet } from 'react-router-dom'
import { useHealth } from '../../lib/hooks/useHealth'
import { SideNav } from './SideNav'
import { TopBar } from './TopBar'
import { StatusStrip } from './StatusStrip'
import { DegradedBanner } from './DegradedBanner'
import { MobileNav } from './MobileNav'

export function AppShell() {
  const { state } = useHealth()
  // Bumped whenever the world changes, so every screen re-reads its data.
  const [worldVersion, setWorldVersion] = useState(0)

  const bump = useCallback(() => setWorldVersion((v) => v + 1), [])

  return (
    <div className="flex min-h-dvh flex-col bg-bg text-ink">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-3 focus:top-3 focus:z-modal focus:rounded-control focus:bg-surface focus:px-3 focus:py-2 focus:text-[13px]"
      >
        Skip to main content
      </a>

      <TopBar onWorldChanged={bump} />
      <DegradedBanner state={state} />

      <div className="flex min-h-0 flex-1">
        <SideNav />
        <main id="main" tabIndex={-1} className="min-w-0 flex-1 overflow-y-auto px-4 py-4 pb-20 md:px-6 md:py-5 md:pb-5">
          <Outlet context={{ worldVersion, bumpWorld: bump }} />
        </main>
      </div>

      <MobileNav />
      <StatusStrip onStreamEvent={bump} />
    </div>
  )
}
```

- [ ] **Step 7: Write the mobile navigation**

`frontend/src/app/shell/MobileNav.tsx`:

```tsx
import { useState } from 'react'
import { NavLink } from 'react-router-dom'
import { X } from '../../icons'
import { NAV_ITEMS } from './navigation'

const PRIMARY = NAV_ITEMS.filter((item) => item.primary).slice(0, 5)
const OVERFLOW = NAV_ITEMS.filter((item) => !PRIMARY.includes(item))

/**
 * Material caps a bottom bar at five destinations, all top-level, each with a
 * label as well as an icon (spec 11). Everything else lives behind More.
 */
export function MobileNav() {
  const [open, setOpen] = useState(false)

  return (
    <nav aria-label="Primary mobile" className="border-t border-hairline bg-surface md:hidden">
      <ul className="flex">
        {PRIMARY.map(({ to, label, icon: Glyph }) => (
          <li key={to} className="flex-1">
            <NavLink
              to={to}
              className={({ isActive }) =>
                `flex min-h-[44px] flex-col items-center justify-center gap-0.5 py-1.5 text-[10px] ${
                  isActive ? 'text-primary' : 'text-muted'
                }`
              }
            >
              <Glyph size={18} />
              {label}
            </NavLink>
          </li>
        ))}
        <li className="flex-1">
          <button
            type="button"
            onClick={() => setOpen(true)}
            aria-expanded={open}
            className="flex min-h-[44px] w-full cursor-pointer flex-col items-center justify-center gap-0.5 py-1.5 text-[10px] text-muted"
          >
            <span className="text-[18px] leading-none">···</span>
            More
          </button>
        </li>
      </ul>

      {open ? (
        <div className="fixed inset-0 z-modal flex flex-col justify-end bg-black/50" role="dialog" aria-modal="true" aria-label="All destinations">
          <div className="rounded-t-panel border-t border-hairline bg-surface p-3">
            <div className="mb-2 flex items-center justify-between">
              <span className="text-[13px] font-medium text-ink">All destinations</span>
              <button
                type="button"
                aria-label="Close"
                onClick={() => setOpen(false)}
                className="inline-flex h-11 w-11 cursor-pointer items-center justify-center rounded-control text-muted"
              >
                <X size={16} />
              </button>
            </div>
            <ul className="grid grid-cols-2 gap-1">
              {OVERFLOW.map(({ to, label, icon: Glyph }) => (
                <li key={to}>
                  <NavLink
                    to={to}
                    onClick={() => setOpen(false)}
                    className="flex min-h-[44px] items-center gap-2 rounded-control px-2.5 text-[13px] text-muted"
                  >
                    <Glyph size={16} />
                    {label}
                  </NavLink>
                </li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}
    </nav>
  )
}
```

- [ ] **Step 8: Run the test to verify it passes**

Run: `npm test -- src/app/shell/AppShell.test.tsx`
Expected: PASS, 9 tests.

- [ ] **Step 9: Commit**

```bash
git add frontend/src/app/shell
git commit -m "feat(frontend): add app shell with nav, sim clock, status strip and degraded banner"
```

---

## Task 20: Routing and stub screens

**Files:**
- Create: `frontend/src/app/router.tsx`, `frontend/src/features/_stub/PlannedScreen.tsx`, `frontend/src/app/router.test.tsx`
- Modify: `frontend/src/app/App.tsx`, `frontend/src/main.tsx`

**Interfaces:**
- Consumes: Tasks 14, 19
- Produces: `<AppRoutes>`; `useWorldVersion()` returning `{ worldVersion, bumpWorld }` from the outlet context

- [ ] **Step 1: Write the failing test**

`frontend/src/app/router.test.tsx`:

```tsx
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect } from 'vitest'
import { AppRoutes } from './router'
import { ApiProvider } from '../lib/hooks/ApiProvider'
import { ThemeProvider } from '../design/theme'
import { createMockClient } from '../lib/api/mock/mockClient'

function renderAt(path: string) {
  return render(
    <ThemeProvider>
      <ApiProvider client={createMockClient({ seed: 12345 })}>
        <MemoryRouter initialEntries={[path]}>
          <AppRoutes />
        </MemoryRouter>
      </ApiProvider>
    </ThemeProvider>,
  )
}

describe('routing', () => {
  it('renders the dashboard at /dashboard', () => {
    renderAt('/dashboard')
    expect(screen.getByRole('heading', { level: 1, name: /operations dashboard/i })).toBeInTheDocument()
  })

  it('renders a planned screen for a route owned by a later plan', () => {
    renderAt('/loadtest')
    expect(screen.getByText(/load test/i)).toBeInTheDocument()
    expect(screen.getByText(/planned/i)).toBeInTheDocument()
  })

  it('renders a not-found screen for an unknown route', () => {
    renderAt('/does-not-exist')
    expect(screen.getByText(/not found/i)).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm test -- src/app/router.test.tsx`
Expected: FAIL — cannot resolve `./router`.

- [ ] **Step 3: Write the stub screen**

`frontend/src/features/_stub/PlannedScreen.tsx`:

```tsx
import { EmptyState } from '../../components/EmptyState'
import { Scroll } from '../../icons'

/**
 * A placeholder for a screen delivered in a later plan. It states what will
 * live here rather than rendering an empty page, so the navigation can be
 * reviewed honestly before every screen exists.
 */
export function PlannedScreen({ title, summary, plan }: { title: string; summary: string; plan: string }) {
  return (
    <div className="mx-auto max-w-3xl">
      <h1 className="text-[24px] leading-8 font-semibold tracking-tight text-ink">{title}</h1>
      <div className="mt-4">
        <EmptyState
          icon={<Scroll size={20} />}
          title="Planned for a later phase"
          description={`${summary} This screen is delivered in ${plan}.`}
        />
      </div>
    </div>
  )
}
```

- [ ] **Step 4: Write the router**

`frontend/src/app/router.tsx`:

```tsx
import { Navigate, Route, Routes, useOutletContext } from 'react-router-dom'
import { AppShell } from './shell/AppShell'
import { DashboardScreen } from '../features/dashboard/DashboardScreen'
import { PlannedScreen } from '../features/_stub/PlannedScreen'

export interface ShellContext {
  worldVersion: number
  bumpWorld: () => void
}

/**
 * A screen rendered outside the shell — in isolation, or in a test that mounts
 * it directly — must still render. `useOutletContext` returns null when there
 * is no parent outlet, and destructuring that null is a crash on first paint.
 */
const DETACHED_CONTEXT: ShellContext = { worldVersion: 0, bumpWorld: () => {} }

export function useWorldVersion(): ShellContext {
  return useOutletContext<ShellContext>() ?? DETACHED_CONTEXT
}

export function AppRoutes() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<Navigate to="/dashboard" replace />} />
        <Route path="dashboard" element={<DashboardScreen />} />

        <Route
          path="network"
          element={<PlannedScreen title="Network Map" plan="Plan 2"
            summary="A flow graph of both divisions, their depots, stations and routes, with live inventory and in-transit shipments." />}
        />
        <Route
          path="inventory"
          element={<PlannedScreen title="Inventory & Demand" plan="Plan 2"
            summary="Per-node fuel levels against capacity, demand history and a forecast with a confidence band." />}
        />
        <Route
          path="risk"
          element={<PlannedScreen title="Risk & Alerts" plan="Plan 2"
            summary="Projected stockout hours per station with severity, confidence and the signals behind each alert." />}
        />
        <Route
          path="allocations"
          element={<PlannedScreen title="Allocation Console" plan="Plan 2"
            summary="Recommended and manual allocations, with the Guide 5.2 constraints shown inline before dispatch." />}
        />
        <Route
          path="explain"
          element={<PlannedScreen title="Decision Explainability" plan="Plan 2"
            summary="Why a specific allocation was recommended: the signals, the binding constraints, the expected impact and the rejected alternatives." />}
        />
        <Route
          path="scenarios"
          element={<PlannedScreen title="Scenario & Crisis Injection" plan="Plan 3"
            summary="Run, pause, step and reset the simulation, and inject any of the six crisis events." />}
        />
        <Route
          path="observability"
          element={<PlannedScreen title="Observability & Health" plan="Plan 3"
            summary="Component health, latency percentiles, error rate, intelligence metrics and the log stream." />}
        />
        <Route
          path="resilience"
          element={<PlannedScreen title="Resilience Center" plan="Plan 3"
            summary="Inject each fault type and watch retries, circuit-breaker state and fallback activation." />}
        />
        <Route
          path="audit"
          element={<PlannedScreen title="Audit & Decision History" plan="Plan 3"
            summary="The simulator audit log as a filterable timeline alongside decision history and replay." />}
        />
        <Route
          path="loadtest"
          element={<PlannedScreen title="Load Test Evidence" plan="Plan 3"
            summary="The workload definition and measured latency percentiles, throughput and error rate." />}
        />
        <Route
          path="*"
          element={<PlannedScreen title="Not Found" plan="a later phase"
            summary="No screen is registered at this address." />}
        />
      </Route>
    </Routes>
  )
}
```

- [ ] **Step 5: Wire App and main**

`frontend/src/app/App.tsx`:

```tsx
import { BrowserRouter } from 'react-router-dom'
import { ThemeProvider } from '../design/theme'
import { ApiProvider } from '../lib/hooks/ApiProvider'
import { api } from '../lib/api'
import { AppRoutes } from './router'

export function App({ client = api }: { client?: typeof api }) {
  return (
    <ThemeProvider>
      <ApiProvider client={client}>
        <BrowserRouter>
          <AppRoutes />
        </BrowserRouter>
      </ApiProvider>
    </ThemeProvider>
  )
}
```

- [ ] **Step 6: Run the router test**

Run: `npm test -- src/app/router.test.tsx`
Expected: FAIL — cannot resolve `../features/dashboard/DashboardScreen`. That is Task 21. Create a minimal placeholder now so this task's tests pass, and Task 21 replaces it:

`frontend/src/features/dashboard/DashboardScreen.tsx`:

```tsx
export function DashboardScreen() {
  return <h1 className="text-[24px] font-semibold text-ink">Operations Dashboard</h1>
}
```

Run again: `npm test -- src/app/router.test.tsx`
Expected: PASS, 3 tests.

- [ ] **Step 7: Update the Task 1 smoke test**

`frontend/src/app/App.test.tsx` now needs a router-safe client and will render the dashboard:

```tsx
import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { AppRoutes } from './router'
import { ThemeProvider } from '../design/theme'
import { ApiProvider } from '../lib/hooks/ApiProvider'
import { createMockClient } from '../lib/api/mock/mockClient'

describe('App', () => {
  it('renders the operator console', () => {
    render(
      <ThemeProvider>
        <ApiProvider client={createMockClient({ seed: 12345 })}>
          <MemoryRouter initialEntries={['/dashboard']}>
            <AppRoutes />
          </MemoryRouter>
        </ApiProvider>
      </ThemeProvider>,
    )
    expect(screen.getByRole('navigation', { name: /primary/i })).toBeInTheDocument()
  })
})
```

- [ ] **Step 8: Run the whole suite**

Run: `npm test`
Expected: All pass.

- [ ] **Step 9: Commit**

```bash
git add frontend/src/app frontend/src/features
git commit -m "feat(frontend): add routing, planned-screen stubs and app wiring"
```

---

## Task 21: Operations Dashboard

**Files:**
- Modify: `frontend/src/features/dashboard/DashboardScreen.tsx`
- Create: `frontend/src/features/dashboard/useDashboardData.ts`, `frontend/src/features/dashboard/NetworkHealthStrip.tsx`, `frontend/src/features/dashboard/DashboardScreen.test.tsx`

**Interfaces:**
- Consumes: Tasks 14, 16, 18, 19, 20
- Produces: `<DashboardScreen>`; `useDashboardData(): { metrics, depots, stations, routes, events, allocations, instance, loading, error, stale, receivedAt, refresh }`

- [ ] **Step 1: Write the failing test**

`frontend/src/features/dashboard/DashboardScreen.test.tsx`:

```tsx
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect } from 'vitest'
import { DashboardScreen } from './DashboardScreen'
import { ApiProvider } from '../../lib/hooks/ApiProvider'
import { ThemeProvider } from '../../design/theme'
import { createMockClient } from '../../lib/api/mock/mockClient'

function renderDashboard(client = createMockClient({ seed: 12345 })) {
  return render(
    <ThemeProvider>
      <ApiProvider client={client}>
        <MemoryRouter>
          <DashboardScreen />
        </MemoryRouter>
      </ApiProvider>
    </ThemeProvider>,
  )
}

describe('DashboardScreen', () => {
  it('renders the page heading', async () => {
    renderDashboard()
    expect(screen.getByRole('heading', { level: 1, name: /operations dashboard/i })).toBeInTheDocument()
  })

  it('shows a service-level stat once metrics load', async () => {
    renderDashboard()
    await waitFor(() => expect(screen.getByText(/service level/i)).toBeInTheDocument())
  })

  it('lists every network node in the health strip', async () => {
    renderDashboard()
    await waitFor(() => expect(screen.getByText('Gazipur Depot')).toBeInTheDocument())
    expect(screen.getByText('Patiya Depot')).toBeInTheDocument()
    expect(screen.getByText('Mirpur Fuel Station')).toBeInTheDocument()
    expect(screen.getByText("Cox's Bazar Fuel Station")).toBeInTheDocument()
  })

  it('renders on a freshly reset world without crashing', async () => {
    const client = createMockClient({ seed: 12345 })
    client.world.reset()
    renderDashboard(client)
    await waitFor(() => expect(screen.getByRole('heading', { level: 1 })).toBeInTheDocument())
  })

  it('shows an error state rather than a blank page when the simulator is unavailable', async () => {
    const client = createMockClient({ seed: 12345 })
    client.world.injectFault({ type: 'unavailable', duration_seconds: 60 })
    renderDashboard(client)
    await waitFor(() => expect(screen.getAllByRole('alert').length).toBeGreaterThan(0))
  })

  it('marks its panels stale rather than showing stale figures as current', async () => {
    const client = createMockClient({ seed: 12345 })
    client.world.injectFault({ type: 'stale_data', duration_seconds: 60 })
    renderDashboard(client)
    await waitFor(() => expect(screen.getAllByText(/stale/i).length).toBeGreaterThan(0))
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm test -- src/features/dashboard/DashboardScreen.test.tsx`
Expected: FAIL — the placeholder heading has no stats or health strip.

- [ ] **Step 3: Write the dashboard data hook**

`frontend/src/features/dashboard/useDashboardData.ts`:

```ts
import { useSimulatorQuery } from '../../lib/hooks/useSimulatorQuery'
import { useWorldVersion } from '../../app/router'
import type {
  Allocation, Depot, DomainEvent, Metrics, Route, SimInstance, Station,
} from '../../lib/types/simulator'

/**
 * One read per resource, all keyed on the shell's world version so a tick, an
 * SSE event or a manual step re-reads everything at once.
 */
export function useDashboardData() {
  const { worldVersion } = useWorldVersion()

  const metrics = useSimulatorQuery<Metrics>('metrics', (c) => c.getMetrics(), { refreshKey: worldVersion })
  const depots = useSimulatorQuery<Depot[]>('depots', (c) => c.getDepots(), { refreshKey: worldVersion })
  const stations = useSimulatorQuery<Station[]>('stations', (c) => c.getStations(), { refreshKey: worldVersion })
  const routes = useSimulatorQuery<Route[]>('routes', (c) => c.getRoutes(), { refreshKey: worldVersion })
  const events = useSimulatorQuery<DomainEvent[]>('events', (c) => c.getEvents(), { refreshKey: worldVersion })
  const allocations = useSimulatorQuery<Allocation[]>('allocations', (c) => c.getAllocations(), { refreshKey: worldVersion })
  const instance = useSimulatorQuery<SimInstance>('instance', (c) => c.getInstance(), { refreshKey: worldVersion })

  const queries = [metrics, depots, stations, routes, events, allocations, instance]
  const loading = queries.some((q) => q.loading && q.data === null)
  const error = queries.find((q) => q.error)?.error ?? null
  const stale = queries.some((q) => q.stale)
  const receivedAt = Math.max(...queries.map((q) => q.receivedAt ?? 0)) || null

  return {
    metrics: metrics.data, depots: depots.data, stations: stations.data,
    routes: routes.data, events: events.data, allocations: allocations.data,
    instance: instance.data, loading, error, stale, receivedAt,
    refresh: () => queries.forEach((q) => q.refresh()),
  }
}
```

- [ ] **Step 4: Write the network health strip**

`frontend/src/features/dashboard/NetworkHealthStrip.tsx`:

```tsx
import { StatusPill } from '../../components/StatusPill'
import { Depot, Station } from '../../icons'
import { formatLiters, formatPercent } from '../../lib/format'
import type { Depot as DepotType, FuelType, Station as StationType } from '../../lib/types/simulator'

const FUELS: FuelType[] = ['DIESEL', 'PETROL', 'OCTANE']

function fillRatio(inventory: Record<FuelType, number>, capacity: Record<FuelType, number>): number {
  const totalCapacity = FUELS.reduce((sum, f) => sum + capacity[f], 0)
  if (totalCapacity <= 0) return 0
  const totalInventory = FUELS.reduce((sum, f) => sum + inventory[f], 0)
  return Math.min(1, Math.max(0, totalInventory / totalCapacity))
}

/**
 * A node's fill level is shown as a bar with a numeric percentage beside it —
 * never as colour alone, and never as a bare visual gauge that a reader has to
 * estimate.
 */
function NodeCard({
  name, id, status, inventory, capacity, kind,
}: {
  name: string; id: string; status: string; kind: 'depot' | 'station'
  inventory: Record<FuelType, number>; capacity: Record<FuelType, number>
}) {
  const ratio = fillRatio(inventory, capacity)
  const critical = ratio < 0.2
  const warning = ratio < 0.4 && !critical

  return (
    <div className="rounded-panel border border-hairline bg-surface px-3 py-2.5">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 text-muted">
            {kind === 'depot' ? <Depot size={13} /> : <Station size={13} />}
            <span className="mono truncate text-[11px]">{id}</span>
          </div>
          <p className="truncate text-[13px] font-medium text-ink">{name}</p>
        </div>
        <StatusPill status={status} kind={kind} size="sm" />
      </div>

      <div className="mt-2">
        <div className="flex items-baseline justify-between">
          <span className="text-[11px] text-subtle">Fill level</span>
          <span className="mono text-[12px] font-medium text-ink">{formatPercent(ratio)}</span>
        </div>
        <div className="mt-1 h-1.5 w-full overflow-hidden rounded-pill bg-surface-raised" role="presentation">
          <div
            className={`h-full rounded-pill ${
              critical ? 'bg-status-critical' : warning ? 'bg-status-warning' : 'bg-status-good'
            }`}
            style={{ width: `${Math.round(ratio * 100)}%` }}
          />
        </div>
        <p className="mt-1 text-[11px] text-muted">
          {formatLiters(FUELS.reduce((s, f) => s + inventory[f], 0), { compact: true })} of{' '}
          {formatLiters(FUELS.reduce((s, f) => s + capacity[f], 0), { compact: true })}
        </p>
      </div>
    </div>
  )
}

export function NetworkHealthStrip({ depots, stations }: { depots: DepotType[]; stations: StationType[] }) {
  const safeDepots = Array.isArray(depots) ? depots : []
  const safeStations = Array.isArray(stations) ? stations : []

  return (
    <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2 xl:grid-cols-3">
      {safeDepots.map((d) => (
        <NodeCard key={d.id} kind="depot" id={d.id} name={d.name} status={d.status}
          inventory={d.inventory} capacity={d.capacity} />
      ))}
      {safeStations.map((s) => (
        <NodeCard key={s.id} kind="station" id={s.id} name={s.name} status={s.status}
          inventory={s.inventory} capacity={s.capacity} />
      ))}
    </div>
  )
}
```

- [ ] **Step 5: Write the dashboard screen**

Replace `frontend/src/features/dashboard/DashboardScreen.tsx`:

```tsx
import { Panel } from '../../components/Panel'
import { StatTile } from '../../components/StatTile'
import { StatusPill } from '../../components/StatusPill'
import { DataTable, type Column } from '../../components/DataTable'
import { EmptyState } from '../../components/EmptyState'
import { ErrorState } from '../../components/ErrorState'
import { Skeleton } from '../../components/Skeleton'
import { FreshnessBadge } from '../../components/FreshnessBadge'
import { Shield } from '../../icons'
import { formatLiters, formatPercent, formatTick } from '../../lib/format'
import type { Allocation } from '../../lib/types/simulator'
import { useDashboardData } from './useDashboardData'
import { NetworkHealthStrip } from './NetworkHealthStrip'

export function DashboardScreen() {
  const {
    metrics, depots, stations, routes, events, allocations, instance,
    loading, error, stale, receivedAt, refresh,
  } = useDashboardData()

  const activeEvents = (events ?? []).filter((e) => e.status === 'ACTIVE')
  const scheduledEvents = (events ?? []).filter((e) => e.status === 'SCHEDULED')
  const disruptedRoutes = (routes ?? []).filter((r) => r.status === 'DISRUPTED')
  const failedAllocations = (allocations ?? []).filter((a) => a.status === 'FAILED').length
  const inTransit = (allocations ?? []).filter((a) => a.status === 'IN_TRANSIT')

  const allocationColumns: Array<Column<Allocation>> = [
    { key: 'id', header: 'ID', render: (a) => <span className="mono">{a.id}</span> },
    { key: 'route', header: 'Route', render: (a) => <span className="mono text-[12px]">{a.route_id}</span> },
    { key: 'fuel', header: 'Fuel', render: (a) => a.fuel_type },
    { key: 'qty', header: 'Quantity', numeric: true, render: (a) => formatLiters(a.quantity, { compact: true }) },
    { key: 'status', header: 'Status', render: (a) => <StatusPill status={a.status} size="sm" /> },
  ]

  if (error && !metrics) {
    return (
      <div className="mx-auto max-w-3xl">
        <h1 className="text-[24px] leading-8 font-semibold tracking-tight text-ink">Operations Dashboard</h1>
        <div className="mt-4"><ErrorState error={error} onRetry={refresh} /></div>
      </div>
    )
  }

  return (
    <div className="mx-auto flex max-w-[1600px] flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-[24px] leading-8 font-semibold tracking-tight text-ink">Operations Dashboard</h1>
          <p className="text-[13px] text-muted">
            Live state of the simulated Bangladesh fuel network.
          </p>
        </div>
        <FreshnessBadge receivedAt={receivedAt} stale={stale} />
      </div>

      {loading ? (
        <div className="grid grid-cols-2 gap-2.5 lg:grid-cols-5"><Skeleton variant="tile" count={5} /></div>
      ) : (
        <div className="grid grid-cols-2 gap-2.5 lg:grid-cols-5">
          <StatTile
            label="Service level"
            value={metrics ? formatPercent(metrics.service_level, 1) : null}
            tone={metrics && metrics.service_level >= 0.95 ? 'good'
              : metrics && metrics.service_level >= 0.85 ? 'warning' : 'critical'}
            hint="Served ÷ total demand"
          />
          <StatTile
            label="Unmet demand"
            value={metrics ? formatLiters(metrics.unmet_demand_liters, { compact: true }) : null}
            tone={metrics && metrics.unmet_demand_liters > 0 ? 'warning' : 'good'}
          />
          <StatTile
            label="Allocated"
            value={metrics ? formatLiters(metrics.allocation_liters, { compact: true }) : null}
            hint={`${inTransit.length} in transit`}
          />
          <StatTile
            label="Allocation failures"
            value={metrics ? metrics.allocation_failures : null}
            tone={metrics && metrics.allocation_failures > 0 ? 'critical' : 'default'}
          />
          <StatTile
            label="Disrupted routes"
            value={disruptedRoutes.length}
            tone={disruptedRoutes.length > 0 ? 'critical' : 'good'}
            hint={instance ? formatTick(instance.tick, instance.tick_minutes) : undefined}
          />
        </div>
      )}

      <Panel
        title="Network health"
        actions={<FreshnessBadge receivedAt={receivedAt} stale={stale} />}
        stale={stale}
      >
        {loading ? <Skeleton variant="block" /> : (
          <NetworkHealthStrip depots={depots ?? []} stations={stations ?? []} />
        )}
      </Panel>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <Panel title="Active events" stale={stale} className="xl:col-span-1">
          {activeEvents.length === 0 && scheduledEvents.length === 0 ? (
            <EmptyState
              icon={<Shield size={18} />}
              title="No active disruptions"
              description="No crisis event is currently affecting the network. Inject one from the Scenarios screen to exercise the response path."
            />
          ) : (
            <ul className="flex flex-col gap-2">
              {[...activeEvents, ...scheduledEvents].map((event) => (
                <li key={event.id} className="flex items-center justify-between gap-2 border-b border-hairline pb-2 last:border-0 last:pb-0">
                  <div className="min-w-0">
                    <p className="mono truncate text-[12px] text-ink">{event.type}</p>
                    <p className="text-[11px] text-muted">
                      Ticks {event.start_tick}–{event.end_tick}
                    </p>
                  </div>
                  <StatusPill status={event.status} size="sm" />
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="Recent allocations" stale={stale} className="xl:col-span-2">
          <DataTable
            columns={allocationColumns}
            rows={(allocations ?? []).slice(0, 8)}
            getRowKey={(a) => a.id}
            caption="Most recent fuel allocations"
            emptyMessage="No allocations have been issued yet"
          />
        </Panel>
      </div>

      {failedAllocations > 0 ? (
        <p className="text-[12px] text-status-critical">
          {failedAllocations} allocation{failedAllocations === 1 ? '' : 's'} failed in transit.
        </p>
      ) : null}
    </div>
  )
}
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `npm test -- src/features/dashboard/DashboardScreen.test.tsx`
Expected: PASS, 6 tests.

- [ ] **Step 7: Run the whole suite, typecheck, lint and build**

Run: `npm test && npm run typecheck && npm run lint && npm run build`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/features/dashboard
git commit -m "feat(frontend): add operations dashboard with KPI row and network health strip"
```

---

## Task 22: Verification pass

**Files:**
- Create: `frontend/src/test/contrast.test.ts`, `frontend/README.md`
- Modify: `frontend/src/app/App.test.tsx` (only if verification surfaces a gap)

**Interfaces:**
- Consumes: everything above
- Produces: evidence that the phase's definition of done holds

- [ ] **Step 1: Write the token-contrast test**

`frontend/src/test/contrast.test.ts` — parses `tokens.css` and asserts the required ratios in **each theme independently**, since assuming light-mode values work in dark is the classic mistake.

```ts
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const css = readFileSync(resolve(__dirname, '../design/tokens.css'), 'utf8')

function block(selector: string): string {
  const start = css.indexOf(selector)
  if (start === -1) throw new Error(`selector not found: ${selector}`)
  const open = css.indexOf('{', start)
  const close = css.indexOf('}', open)
  return css.slice(open, close)
}

function token(source: string, name: string): string {
  const match = source.match(new RegExp(`--${name}:\\s*([^;]+);`))
  if (!match) throw new Error(`token not found: --${name}`)
  return match[1]!.trim()
}

function srgbToLinear(channel: number): number {
  return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
}

function luminance(hex: string): number {
  const value = hex.replace('#', '')
  const full = value.length === 3 ? value.split('').map((c) => c + c).join('') : value
  const r = parseInt(full.slice(0, 2), 16) / 255
  const g = parseInt(full.slice(2, 4), 16) / 255
  const b = parseInt(full.slice(4, 6), 16) / 255
  return 0.2126 * srgbToLinear(r) + 0.7152 * srgbToLinear(g) + 0.0722 * srgbToLinear(b)
}

function contrast(a: string, b: string): number {
  const [lighter, darker] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number]
  return (lighter + 0.05) / (darker + 0.05)
}

describe.each([
  ['dark', ':root,\n:root[data-theme=\'dark\']'],
  ['light', ":root[data-theme='light']"],
])('%s theme contrast', (_theme, selector) => {
  const theme = block(selector)

  it('renders body text at 4.5:1 or better against the page background', () => {
    expect(contrast(token(theme, 'text'), token(theme, 'bg'))).toBeGreaterThanOrEqual(4.5)
  })

  it('renders body text at 4.5:1 or better against surfaces', () => {
    expect(contrast(token(theme, 'text'), token(theme, 'surface'))).toBeGreaterThanOrEqual(4.5)
  })

  it('renders muted text at 4.5:1 or better against surfaces', () => {
    expect(contrast(token(theme, 'text-muted'), token(theme, 'surface'))).toBeGreaterThanOrEqual(4.5)
  })

  it('renders the primary colour at 3:1 or better against surfaces for UI glyphs', () => {
    expect(contrast(token(theme, 'primary'), token(theme, 'surface'))).toBeGreaterThanOrEqual(3)
  })
})

describe('theme-invariant status tokens', () => {
  it('defines status colours outside both theme blocks so they cannot drift', () => {
    const dark = block(':root,\n:root[data-theme=\'dark\']')
    const light = block(":root[data-theme='light']")
    for (const name of ['status-good', 'status-warning', 'status-serious', 'status-critical']) {
      expect(dark).not.toContain(`--${name}`)
      expect(light).not.toContain(`--${name}`)
    }
  })
})
```

- [ ] **Step 2: Run the contrast test**

Run: `npm test -- src/test/contrast.test.ts`
Expected: PASS, 9 tests. If any ratio fails, adjust that token in `tokens.css` — do not weaken the assertion.

- [ ] **Step 3: Verify the layout at every breakpoint**

Run: `npm run dev`, then in the browser check 375, 768, 1024 and 1440 px widths on `/dashboard`:
- no horizontal scrollbar at any width
- the bottom bar appears below 768px and the sidebar above it
- the sidebar disappears below 768px
- every chart and panel reflows rather than clipping

- [ ] **Step 4: Verify both themes**

Toggle the theme with the top-bar button. Confirm the stat tiles, status pills, panel borders and the health-strip bars all remain legible, and that the status pills use the same colours in both themes.

- [ ] **Step 5: Verify keyboard traversal and reduced motion**

Tab from the top of the page: the skip link must appear first, then the clock controls, then the theme toggle, then the navigation. Enable the OS "reduce motion" setting and confirm the pulse animations on the skeletons stop.

- [ ] **Step 6: Verify the fault paths by hand**

In the running app, open a console and run:

```js
// Confirm the app degrades rather than blanking.
const { asMockClient } = await import('/src/lib/api/index.ts')
```

If the dev-server module path is inconvenient, add a temporary button on the Scenarios stub instead. Confirm three states visually: `stale_data` greys the panels and flags staleness, `unavailable` raises the degraded banner while last-known values remain, and `stream_disconnect` shows the stream as down.

- [ ] **Step 7: Write the README**

`frontend/README.md`:

```markdown
# Fuel Supply Intelligence & Resilience Platform — Frontend

Operator console for the simulated Bangladesh fuel supply network.
Built against the BUP Fuel Supply Simulator.

## Running

```bash
npm install
npm run dev      # http://localhost:5173
```

By default the app runs against a deterministic in-browser model of the
simulator's world, so it works with nothing else running.

## Pointing at the real simulator

```bash
cp .env.example .env
# set VITE_SIMULATOR_MODE=live
docker compose up -d         # the simulator, from the integration guide
npm run dev
```

No application code changes between the two modes — only this variable.

## Scripts

| Command | Purpose |
|---|---|
| `npm run dev` | Development server |
| `npm run build` | Production build |
| `npm run typecheck` | Type check without emitting |
| `npm test` | Test suite |
| `npm run lint` | Lint, including the layering rule |

## Architecture

All simulator access goes through `SimulatorClient` in `src/lib/api/client.ts`.
Two implementations sit behind it: `mock/` (the world engine) and `http/` (the
real simulator). Feature code imports from `lib/api` and never from `mock/` or
fixtures — enforced by an ESLint rule, because without it the mock-to-live swap
stops being a config change.

Every read returns `SimResponse<T>`, which carries `stale` and `receivedAt`.
Freshness travels with the data rather than being reconstructed, so a screen
cannot render stale figures as current without having been handed the flag.

## Simulated data

Everything shown is simulated. No real fuel infrastructure is read or
controlled.
```

- [ ] **Step 8: Final verification**

Run: `npm test && npm run typecheck && npm run lint && npm run build`
Expected: all four pass with no warnings.

- [ ] **Step 9: Commit**

```bash
git add frontend
git commit -m "test(frontend): add token contrast checks and frontend README"
```

---

## Plan completion

At the end of Task 22 the following must be true:

1. `npm test`, `npm run typecheck`, `npm run lint`, `npm run build` all pass clean
2. `/dashboard` renders live network data; every other route renders an honest "planned" screen
3. The dashboard renders correctly with zero data, with healthy data, and under an active fault
4. Both themes verified, contrast asserted in each independently
5. No horizontal scroll at 375 / 768 / 1024 / 1440
6. Reduced-motion respected
7. `VITE_SIMULATOR_MODE=live` points at a real simulator with no code change

**Plans 2 and 3** then build the remaining eleven screens on this foundation.
