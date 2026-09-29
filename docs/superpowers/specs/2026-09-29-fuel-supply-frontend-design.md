# Fuel Supply Intelligence & Resilience Platform — Frontend Design Spec

**Date:** 2026-09-29
**Status:** Draft for review
**Scope:** Operator-facing frontend only. No backend code in this phase.
**Event:** BUP CSE Fest 2026 — Hackathon Finals

---

---

## Citation conventions

Section references in this document are disambiguated by prefix:

| Form | Refers to |
|---|---|
| `Brief §N` | `Fuel_Supply_Intelligence_Resilience_Platform.md` — the challenge brief |
| `Guide §N` | `BUP_Fuel_Supply_Simulator_Integration_Guide_Final.md` — the simulator contract |
| `§N` (bare) | A section of **this** document |

This matters because the numbering collides: the brief's §9 is Decision Support and its
§11 is Application Resilience, while this document's §9 is Design Tokens and its §11 is
Responsive Strategy. A bare `§5.2` likewise means this spec's Staleness section, while
`Guide §5.2` means the allocation validation order.

---


## 1. Purpose

Build the operator-facing application for an intelligent fuel supply decision-support
platform operating against the organizer-provided BUP Fuel Supply Simulator.

The brief is explicit that a notebook is not a submission, and that judges must be
able to interact with a working system (Brief §6).
"Working Product & User Experience" carries **20% of the total score** — the single
heaviest criterion. This phase therefore builds the entire operator experience against
a faithful mock of the simulator, so that the backend can be dropped in behind an
unchanged interface later.

**Success looks like:** a judge opens the app, sees a live fuel network, watches a
shortage emerge, inspects why the system recommends a specific allocation, injects a
failure, and watches the platform degrade and recover — without anyone touching a
terminal.

---

## 2. Scope

### In scope

- Twelve screens covering every operator capability listed in Brief §6
- A swappable simulator client: deterministic in-browser world engine, plus a real
  HTTP client for the published simulator
- Global systems: freshness tracking, degraded-mode banner, SSE connection indicator,
  simulation clock, demo mode, human-review gate
- Design token system with full light and dark themes
- Custom SVG illustration, icon set, and architecture diagram
- Responsive behaviour from 375px to 1440px+
- Accessibility to WCAG 2.1 AA

### Out of scope

- **Backend, database, ML models, deployment, CI/CD.** Phase two.
- **Authentication.** The brief never requests it; it would consume demo time.
- **Real map tiles.** A tile provider needs an API key and network access at judging
  time. A hand-drawn SVG network graph conveys identical topology with no external
  dependency.
- **Real-world fuel data.** Brief §24 forbids it, and the platform must distinguish simulated
  from real results.

### Explicitly deferred to phase two

Prediction models, optimization solvers, LLM explanation generation, Prometheus/Grafana
wiring, load-test execution. **This phase builds the surfaces those systems will report
into, populated by the mock.** Every screen must therefore render correctly against a
mock that is deliberately made to misbehave, so the surfaces are proven before the real
data arrives.

---

## 3. Design principles

### 3.1 This must not look AI-generated

The single largest risk to this project is a UI that reads as generic template output.
Judges see dozens of submissions; a purple-gradient SaaS landing page signals a weekend
of copy-paste, not an operations platform. The following are **hard rules**, not
preferences.

**Banned outright:**

| Banned | Why |
|---|---|
| Indigo/violet gradient backgrounds | The primary visual tell of generated UI. Palette is blue + amber, deliberately. |
| Gradient text on headings | Decorative; conveys nothing. |
| `rounded-2xl` on every surface | Uniform large radii flatten hierarchy. Radii are small and differentiated. |
| Soft drop shadows on cards | Ops software uses hairline borders. Shadows imply floating cards, not panels. |
| Glassmorphism / backdrop-blur as decoration | Reserved strictly for modal scrims, where blur signals dismissal. |
| Emoji as icons | Font-dependent, unstyleable, unprofessional. SVG only. |
| Centered hero + three feature cards | The default template layout. Landing is asymmetric and content-led. |
| Placeholder copy ("Feature One", "Lorem ipsum") | Every string is real domain content. |
| Filler metric tiles that show nothing | Every number on screen is derived from the world engine. |
| Animations with no causal meaning | Motion must express a state change. |

**Required instead:**

1. **Density is a feature.** This is an operations console. Information per screen should
   approach Grafana/Datadog/Bloomberg terminal density, not a marketing page. Spacing
   scale tuned to density 9/10.
2. **Colour carries meaning or it doesn't appear.** The interface is ~90% neutral greys.
   Blue, amber, red, and green appear *only* to encode state. If a surface is coloured,
   a reader can name what the colour means.
3. **Hairline borders, not elevation.** 1px borders at low opacity define panels. At most
   two elevation levels exist in the whole application: base and overlay.
4. **Small, differentiated radii.** 4px controls, 6px panels, 999px pills only for status
   and fuel-type chips. Never one radius everywhere.
5. **Numbers are monospaced and tabular.** Every figure — liters, ticks, timestamps,
   percentages, IDs — uses Fira Code with `font-variant-numeric: tabular-nums`. Columns
   must not jitter as values tick.
6. **Domain-specific detail everywhere.** Real entity IDs (`depot-gazipur`,
   `station-mirpur`, `route-patiya-coxsbazar`), real units (L, L/tick, ticks), real
   simulation time. Never "Item 1".
7. **Asymmetric, left-anchored layout.** Persistent sidebar, content anchored left. No
   centred compositions except deliberate empty states.
8. **Hierarchy via size + weight + colour together**, not size alone.
9. **Dark theme is genuinely dark.** Base `#0B1220` is a desaturated navy-black, not
   purple-navy. Surfaces step up in lightness, they don't glow.
10. **Every chart looks like an instrument.** Axis labels with units, visible gridlines
    at low contrast, real scales, legends adjacent to the plot.

### 3.2 Honesty over polish

The brief repeatedly rewards systems that behave well when things break. Where a number
is stale, uncertain, or unavailable, **the UI says so** rather than showing a confident
stale value. Uncertainty is rendered, not hidden.

### 3.3 Simulated, and visibly so

Brief §24 requires distinguishing simulated results from real-world conditions. Every screen
carries a persistent "SIMULATED" indicator in the status strip, and the landing page
states it plainly.

### 3.4 Human review is preserved

Brief §24 requires human review for consequential simulated decisions. Allocation dispatch
requires explicit operator confirmation. The system recommends; it never auto-dispatches.

---

## 4. Architecture

### 4.1 Directory layout

```
frontend/
  index.html
  vite.config.ts
  tailwind.config.ts
  tsconfig.json
  package.json
  .env.example
  src/
    main.tsx
    app/
      App.tsx                 providers + router
      router.tsx              route table
      shell/
        AppShell.tsx          sidebar + topbar + status strip + outlet
        TopBar.tsx            clock, sim controls, theme toggle, SSE indicator
        SideNav.tsx           primary navigation
        StatusStrip.tsx       system health + freshness + SIMULATED marker
        DegradedBanner.tsx    app-wide degradation notice
    design/
      tokens.css              CSS custom properties, light + dark
      theme.ts                theme provider, persisted preference
      motion.ts               duration + easing tokens
    lib/
      types/
        simulator.ts          VERBATIM contract (Guide §4–§7)
        domain.ts             UI-facing derived types (risk, forecast, decision)
      api/
        client.ts             SimulatorClient interface
        index.ts              implementation selector
        http/
          httpClient.ts       fetch -> http://localhost:8000/v1/*
        mock/
          world.ts            deterministic world engine
          seed.ts             seeded PRNG
          fixtures.ts         baseline scenario (Guide §8)
          mockClient.ts       SimulatorClient implementation
          emitter.ts          SSE-shaped event bus
      hooks/
        useSimulatorQuery.ts  fetch + cache + refetch-on-event
        useStream.ts          SSE connection lifecycle
        useFreshness.ts       per-resource data age
        useHealth.ts          polled health
        useMediaQuery.ts
        useReducedMotion.ts
      format.ts               liters, ticks, sim_time, percent, duration
      validation.ts           Guide §5.2 validation order
    features/
      landing/                (1)  Landing & architecture
      dashboard/              (2)  Operations Dashboard
      network/                (3)  Network Map
      inventory/              (4)  Inventory & Demand
      risk/                   (5)  Risk & Alerts
      allocations/            (6)  Allocation Console
      explain/                (7)  Decision Explainability
      scenarios/              (8)  Scenario & Crisis Injection
      observability/          (9)  Observability & Health
      resilience/             (10) Resilience Center
      audit/                  (11) Audit & Decision History
      loadtest/               (12) Load Test Evidence
    components/               shared primitives
    icons/                    SVG icon set
    art/                      SVG illustrations + diagrams
```

### 4.2 The layering rule

**Feature modules import only from `lib/api`, never from `lib/api/mock` or any fixture.**

This single constraint is what makes the mock-to-real swap a configuration change rather
than a rewrite. Enforced by an ESLint `no-restricted-imports` rule so it cannot silently
erode.

### 4.3 Client selection

```ts
// lib/api/index.ts
export const api: SimulatorClient =
  import.meta.env.VITE_SIMULATOR_MODE === 'live' ? httpClient : mockClient
```

`.env.example` documents both modes and the `VITE_SIMULATOR_BASE_URL` override.

---

## 5. Simulator contract

`lib/types/simulator.ts` transcribes Guide §4–§7 **verbatim**. These
types are the contract; they are not to be "improved" or normalised.

Key unions that must be preserved exactly:

```ts
type DepotStatus      = 'OPEN' | 'CONSTRAINED'
type StationStatus    = 'OPEN' | 'OUTAGE'
type RouteStatus      = 'AVAILABLE' | 'DISRUPTED'
type SupplyStatus     = 'SCHEDULED' | 'DELAYED' | 'ARRIVED'
type EventStatus      = 'SCHEDULED' | 'ACTIVE' | 'RESOLVED'
type AllocationStatus = 'PENDING' | 'IN_TRANSIT' | 'ARRIVED' | 'FAILED' | 'CANCELLED'
type FuelType         = 'DIESEL' | 'PETROL' | 'OCTANE'
type InstanceStatus   = 'PAUSED' | 'RUNNING'
```

The ten `409` codes from Guide §9 each map to a distinct, operator-readable message and a
suggested recovery action. They are not collapsed into a generic error.

### 5.1 Response-shape subtlety worth encoding

The guide notes two different error envelopes: allocation errors use
`{"detail": {"code", "message"}}`, injected faults use `{"error": {"code", "message"}}`,
and Pydantic validation uses FastAPI's default `{"detail": [...]}`. The client must
discriminate these correctly — a single unwrapping helper gets this wrong.

### 5.2 Staleness

`X-Simulator-Stale: true` on any `/v1/*` GET invalidates the local cache and marks the
originating resource stale. `/v1/health` and `/admin/*` bypass faults entirely.

---

## 6. World engine

A deterministic in-browser model of the exact world in Guide §8. It exists so every screen can
be built and demonstrated against realistic, reproducible data with no running simulator.

### 6.1 Fidelity requirements

| Aspect | Requirement |
|---|---|
| Geography | 2 regions, 2 depots, 4 stations, 6 routes — values from Guide §8.1–8.4 verbatim |
| Fuels | DIESEL, PETROL, OCTANE with per-node capacity and inventory |
| Clock | 15 simulated minutes per tick; configurable speed |
| Demand | profile baseline × region `demand_factor` × hour-of-day factor (Guide §8.6) × seeded noise (Guide §8.5) |
| Supply | the 22-arrival schedule (Guide §8.7): 4 initial burst at ticks 12–20, then 18 recurring at 64-tick spacing |
| Determinism | seeded PRNG; same seed + same actions ⇒ identical state |

### 6.2 Behavioural fidelity

The mock enforces the **full Guide §5.2 validation order, first-failure-wins**:

1. Idempotency check
2. `NOT_FOUND` (404)
3. `ROUTE_MISMATCH` (409)
4. `DEPOT_CLOSED` (409)
5. `STATION_CLOSED` (409)
6. `ROUTE_DISRUPTED` (409)
7. `ROUTE_CAPACITY_EXCEEDED` (409)
8. `INSUFFICIENT_INVENTORY` (409)
9. `DISPATCH_CAPACITY_EXCEEDED` (409)
10. `DESTINATION_CAPACITY_EXCEEDED` (409)

Plus idempotency semantics: same key + same body returns the existing allocation with
201; same key + different body returns `IDEMPOTENCY_KEY_MISMATCH`; cancellation does not
free the key.

This matters because the Allocation Console's error handling is then **exercised for
real** during development rather than being written blind and never tested.

### 6.3 Events and faults

All six event types (Guide §7.8) with correct ACTIVE/RESOLVE reversal semantics — including the
detail that `shipment_delay` and `supply_shortfall` are **one-shot and do not auto-undo**,
while `demand_spike`, `route_disruption`, `station_outage`, and `depot_constraint` reverse
on resolve.

All five fault types (Guide §7.10): `latency`, `unavailable`, `error_rate`, `stale_data`,
`stream_disconnect` — each with its documented effect on `/v1/*` and none on `/admin/*`
or `/v1/health`.

---

## 7. Cross-cutting systems

These are what distinguish an operations console from a dashboard, and each maps to a
specific line in the brief.

### 7.1 Freshness

Every data panel displays its age. Under `stale_data`, panels visibly desaturate and the
status strip raises a stale flag. Directly implements Guide §10.

### 7.2 Degraded mode

An app-wide state machine: `normal → degraded → offline`. Driven by `/v1/health` polling
plus active fault state. The banner names **which capability was lost** and what the
system is doing instead — mirroring the Brief §11 table:

```
ML model unavailable          → Fallback allocation policy
Invalid simulator response    → Reject input + raise alert
Prediction confidence too low → Human review requested
Backend dependency unavailable→ Retry / cached state / degraded mode
```

### 7.3 Stream indicator

SSE connection state is always visible: connected, reconnecting, dropped. On reconnect
the client **refetches REST state**, because Guide §6.2 states there is no `Last-Event-ID`
replay. The 15-second keepalive is normal and must not render as a disconnect.

### 7.4 Simulation clock

Tick, simulated time, and RUNNING/PAUSED in the top bar, with run / pause / step / reset
controls. Manual tick advance is emphasised in the UI because Guide §7.5 identifies it as the
recommended way to drive a deterministic demo.

### 7.5 Demo mode

A scripted walkthrough of the 14-step Brief §22 demonstration story. Each step navigates,
highlights the relevant panel, and advances the world. Purpose: the live demo cannot fail
because someone forgot a step or the sim drifted.

### 7.6 Human-review gate

Dispatch requires explicit confirmation showing source depot, destination, route, fuel,
quantity, transit time, and expected arrival tick. The system recommends; the operator
commits.

---

## 8. Screens

Each screen states its purpose, its data sources, and its required states.

### 8.1 (1) Landing & Architecture

Hero with the animated supply-chain SVG (Import → Port → Depot → Distribution → Station →
Demand), the Observe→Recover loop diagram, the architecture diagram required by Brief §19.6,
and a plain-language statement of the problem and the constraints. A "Enter console" CTA
and a sandbox note. Not a marketing page — a briefing.

### 8.2 (2) Operations Dashboard

The default view. Live KPI row (service level, unmet demand, total inventory by fuel,
in-transit volume, allocation failures), network health strip across all six nodes, top
five shortage risks by projected stockout hours, active and scheduled events, and the
most recent allocations. Read-only; every element links into its detail screen.

### 8.3 (3) Network Map

SVG flow graph of the two divisions with depots, stations, and the six routes. Link
thickness encodes `transit_ticks`; link style encodes route status; node fill encodes
inventory as a fraction of capacity. Disrupted routes visibly break. Animated flow
particles along routes show in-transit allocations. Selecting a node opens a detail
inspector.

### 8.4 (4) Inventory & Demand

Per-node inventory against capacity for all three fuels, with time-series demand history
from `/v1/demand-history` and a forecast rendered as a line with a confidence band.
Actual is solid; forecast is dashed; the band is a low-opacity fill of the same hue —
distinguishable without colour. Includes the served/unmet split.

### 8.5 (5) Risk & Alerts

The alert list: projected stockout in hours, severity, confidence, and contributing
signals. Sorted by urgency. Each row states *why* it is at risk — which signals moved and
by how much. Confidence too low routes to human review rather than a confident
recommendation (Brief §11).

### 8.6 (6) Allocation Console

Recommended allocations with their reasoning, and a manual allocation form.

The form performs **client-side validation in the exact Guide §5.2 order** so errors are caught
before submission, and maps each server `409` to a specific, actionable message. It shows
the source depot's remaining dispatch capacity for the current tick, the route's
`max_shipment`, and the destination's remaining headroom — the three constraints most
likely to reject a submission.

Includes a simulate-before-dispatch path showing expected impact without committing.

### 8.7 (7) Decision Explainability

The Brief §9 alert card, built properly: station, fuel, projected stockout, current inventory vs
expected demand, recommended allocation with source, expected result as a before/after
risk comparison — plus confidence, the signals that drove the recommendation, the
constraints that bound it, and the alternatives that were rejected and why.

### 8.8 (8) Scenario & Crisis Injection

Maps the Guide §7 admin surface into a usable operator tool: run / pause / step / reset, and
forms to inject any of the six event types with their correct parameter shapes and
targeting filters. Includes an event timeline showing SCHEDULED → ACTIVE → RESOLVED.

This screen is what makes the demo possible without a terminal.

### 8.9 (9) Observability & Health

Component health for backend, database, simulator, prediction service, and decision
engine; p95 latency and error rate as in Brief §15; intelligence metrics (prediction error,
model confidence, shortage-alert rate, decision frequency, **fallback activation**); and a
log stream of important actions, integration failures, decision events, and recoveries.

### 8.10 (10) Resilience Center

Inject each of the five faults and watch the platform respond. Shows retry attempts with
backoff, circuit-breaker state, cache hits, and fallback activation, plus a recovery
timeline. This is the screen that demonstrates Brief §11 and carries 10% of the score.

### 8.11 (11) Audit & Decision History

The Guide §7.12 audit log rendered as a filterable timeline, alongside decision history: what
was recommended, what was accepted or overridden, by whom, and the resulting outcome.
Supports replay of a past decision against current state.

### 8.12 (12) Load Test Evidence

The workload definition and measured results required by Brief §17: average, p50, p95, and p99
latency, throughput, error rate, concurrency, and resource usage — with the charts to
make them legible and honest notes on where the system's limits are.

---

## 9. Design tokens

### 9.1 Typography

**Fira Sans** carries the interface and all chart text. **Fira Code** appears only where
monospace earns its place, listed below. Both self-hosted and subset, with
`font-display: swap`.

| Role | Family | Size / Line height | Weight |
|---|---|---|---|
| Display | Fira Sans | 32 / 40 | 600 |
| H1 | Fira Sans | 24 / 32 | 600 |
| H2 | Fira Sans | 18 / 26 | 600 |
| H3 | Fira Sans | 15 / 22 | 600 |
| Body | Fira Sans | 14 / 21 | 400 |
| Body strong | Fira Sans | 14 / 21 | 500 |
| Caption | Fira Sans | 12 / 18 | 400 |
| **Stat value** | **Fira Code** | **22 / 28** | **600** |
| **Table cell (numeric)** | **Fira Code** | **13 / 20** | **400–500** |
| **ID / log / route** | **Fira Code** | **12 / 18** | **400** |

Base body is 14px, not 16px. This is a dense console, and 16px body would waste half the
screen. Mobile body stays at 16px to avoid iOS auto-zoom on inputs.

**Where each face is used.** Fira Code is reserved for three cases where monospace earns
its place: live stat values (which must not jitter as they tick), numeric table columns,
and identifier-shaped strings (`depot-gazipur`, `route-patiya-coxsbazar`, audit log
lines). Everything else — including **all text inside charts**: axis ticks, data labels,
legends, and tooltips — stays in Fira Sans, so the chart layer does not mix faces.

All numeric text sets `font-variant-numeric: tabular-nums`, in both faces. Every number in
this application either aligns in a column or updates live, so tabular figures are the
default rather than the exception. The one exception is a static hero figure on the
landing page, which uses proportional figures.

### 9.2 Colour

One semantic token set, redefined wholesale per theme. **Zero raw hex in components.**

| Token | Dark | Light |
|---|---|---|
| `--bg` | `#0B1220` | `#F8FAFC` |
| `--surface` | `#131C2E` | `#FFFFFF` |
| `--surface-raised` | `#1A2438` | `#FFFFFF` |
| `--border` | `rgba(255,255,255,.08)` | `#DBEAFE` |
| `--border-strong` | `rgba(255,255,255,.14)` | `#BFDBFE` |
| `--text` | `#E8EDF5` | `#0F172A` |
| `--text-muted` | `#94A3B8` | `#475569` |
| `--text-subtle` | `#64748B` | `#64748B` |
| `--primary` | `#3B82F6` | `#1E40AF` |
| `--accent` | `#F59E0B` | `#D97706` |
| `--info` | `#38BDF8` | `#0284C7` |

Contrast is verified independently per theme: ≥4.5:1 for body text, ≥3:1 for large text
and UI glyphs. Light-mode `--accent` is darkened from `#F59E0B` to `#D97706` because the
lighter amber fails 3:1 on white.

**Theme-invariant tokens.** These four are identical in both themes and are never
redefined per mode — a critical alert must read the same in a dark control room and on a
light laptop. Destructive UI actions (cancel allocation, reset simulation) use the same
`--status-critical`, so the destructive colour is never a separate, unvalidated red.

| Token | Hex | Used for |
|---|---|---|
| `--status-good` | `#0CA30C` | healthy, arrived, available, accepted |
| `--status-warning` | `#FAB219` | constrained, delayed, degraded, stale |
| `--status-serious` | `#EC835A` | elevated risk, partial failure, fallback active |
| `--status-critical` | `#D03B3B` | outage, disrupted, failed, destructive actions |

### 9.3 Status encoding — never colour alone

Every state carries **colour + icon + text label**. This is required both by accessibility
rules and by the practical reality of a projector with poor colour fidelity.

| State | Token | Icon | Marker |
|---|---|---|---|
| `OPEN` / `AVAILABLE` / `ARRIVED` | `--status-good` | check-circle | solid fill |
| `CONSTRAINED` / `DELAYED` / `IN_TRANSIT` | `--status-warning` | alert-triangle | half fill |
| `OUTAGE` / `DISRUPTED` / `FAILED` | `--status-critical` | x-octagon | strike-through |
| `PAUSED` / `PENDING` / `SCHEDULED` | `--text-muted` | clock | dotted outline |

Note that `CONSTRAINED` and `IN_TRANSIT` are **not** failures — a constrained depot is
still shippable. They take the warning token, never the critical one, because the colour
must not overstate the condition to an operator deciding whether to dispatch.

Warning and serious sit below 3:1 on the light surface **by design** (§9.2). The icon +
label pairing is the mitigation, which is why the table above requires all three channels
together: a status colour never carries meaning alone. Where a status colour appears
inside prose or beside a value, the **text stays in an ink token** and a coloured mark or
icon carries the identity — never coloured text.

### 9.4 Fuel types — the categorical ramp

Fuel types are a categorical series, so they use the validated categorical slots 1–3.
The mapping is **fixed per fuel and never reassigned** — filtering a chart must not
repaint the surviving series.

| Fuel | Slot | Light | Dark | Glyph |
|---|---|---|---|---|
| DIESEL | 1 | `#2A78D6` | `#3987E5` | filled square |
| PETROL | 2 | `#EB6834` | `#D95926` | filled circle |
| OCTANE | 3 | `#1BAF7A` | `#199E70` | filled triangle |

**Validated on this project's actual surfaces**, not on defaults:

- Dark on `#131C2E` — all checks pass; worst all-pairs CVD ΔE 9.4, normal-vision ΔE 20.9
- Light on `#FFFFFF` — all checks pass; worst all-pairs CVD ΔE 9.2, normal-vision ΔE 24.0
- **Relief obligation:** light-mode OCTANE measures 2.82:1 against white, below the 3:1
  bar. This is satisfied by the mandatory legend + direct labels and the table
  alternative required in §9.6 — the WARN is not dismissable, so those are load-bearing,
  not nice-to-have.

The glyph channel means the three fuels remain distinguishable in greyscale, under
colour-vision deficiency, and on a projector with poor colour fidelity.

### 9.5 Spacing, radii, elevation

- Spacing on a 4px grid; scale `4 8 12 16 20 24 32 40 48 64`. Density-tuned: panel
  padding 16px, section gaps 24px, page gutters 24px (16px below 768px).
- Radii: `4px` controls, `6px` panels, `999px` status and fuel pills only.
- Elevation: exactly two levels. Base surfaces use hairline borders. Only modals,
  dropdowns, and toasts get a shadow.
- Z-index scale: `0 / 10 / 20 / 40 / 100 / 1000` — content, sticky, dropdown, overlay,
  modal, toast.

### 9.6 Charts and data visualization

Charts are instruments, not decoration. The following are non-negotiable and apply to
every plot in the application.

**Never do these:**

- **No dual-axis charts.** Two y-scales on one plot is the single most common charting
  error — the apparent crossing point is an artifact of the scales, not the data. Two
  measures of different scale become two charts, small multiples, or indexed to a common
  base.
- **No cycled colours.** The three fuel slots are assigned in fixed order and never
  cycled or re-generated. Colour follows the entity, never its rank, so filtering a chart
  never repaints the surviving series.
- **No rainbow sequential ramps.** Magnitude uses one hue, light→dark. Polarity uses two
  hues with a **neutral grey** midpoint — never a hue at the midpoint, which would read
  as a third category.
- **No numbers on every point.** Direct-label selectively, or the labels become noise.
- **No decorative gradients or shadows that obscure the data.**

**Always do these:**

- **Legend present for ≥2 series**, positioned adjacent to the plot rather than detached
  below a scroll fold. A single-series chart needs no legend box — the title names it.
- **Direct labels on ≤4 series**, so identity never rests on colour alone. Combined with
  the shape channel in §9.4, this discharges the light-mode OCTANE relief obligation.
- **A table alternative for every chart**, plus an `aria-label` summarising the chart's
  key insight for screen readers.
- **A hover layer by default.** Crosshair plus tooltip on line and area charts; per-mark
  tooltips on bar, dot, and cell. Tooltips are keyboard-reachable, not hover-only.
- **Hit targets larger than the mark** — ≥44px interactive area per §12.
- **Recessive chrome.** Gridlines at low contrast, thin marks, 2px lines, ≥8px markers,
  a 2px surface gap between adjacent fills, and a 2px surface ring on overlapping marks.
- **One filter row above the charts**, not scattered controls.

**Form follows the data's job:**

| Job | Form |
|---|---|
| Inventory against capacity per node | Horizontal bar — comparison across labelled categories |
| Demand history + forecast | Line, with the forecast dashed and a low-opacity confidence band |
| Fuel mix proportion | Stacked bar, not a pie — the data has three categories and a time axis |
| Stockout risk by station | Ranked horizontal bar with direct value labels |
| Anomalies in demand | Line with highlighted markers **plus** a text annotation per anomaly |
| A single headline figure | A stat tile — **not** a chart |

**Volume handling.** Below 1,000 points, SVG. At or above 1,000 points, Canvas with
downsampling. Above 10,000, aggregate to intervals with drill-down. Charts reflow or
simplify below 768px — horizontal bars replace vertical ones, and tick counts drop.

**Live charts** carry an explicit pause control and freeze entirely under
`prefers-reduced-motion`, rather than merely slowing.

---

## 10. Motion

Durations and easings are shared tokens so the whole application has one rhythm.

| Interaction | Duration | Easing |
|---|---|---|
| Hover, focus | 150ms | ease-out |
| Panel expand, tab switch | 200ms | ease-out |
| Modal enter | 250ms | ease-out (scale .98→1 + fade) |
| Modal exit | 150ms | ease-in |
| Route transition | 300ms | ease-in-out |
| List stagger | 40ms per item | ease-out |

Rules:

- **Transform and opacity only.** Never animate `width`, `height`, `top`, or `left`.
- One or two animated elements per view, maximum.
- Live charts **freeze** under `prefers-reduced-motion`, not merely slow down.
- Exit animations run at ~60% of enter duration.
- Every animation expresses a cause — a value that changed, a state that moved. Decorative
  motion is removed in review.
- Nothing blocks input while animating.

Signature motion, used sparingly:

- **Route transitions** — directional slide matching navigation hierarchy.
- **Flow particles** on the network map, showing fuel actually moving along a route.
- **Status pulse** — a single slow pulse on a newly-raised critical alert, then still.
- **Value tick** — a brief highlight on a number that changed, so the eye finds it.
- **Skeletons** for loads over 300ms, in the shape of the content that will arrive.

---

## 11. Responsive strategy

Breakpoints: **375 / 768 / 1024 / 1440**, mobile-first.

| Range | Layout |
|---|---|
| < 768 | Bottom bar (5 primary destinations, remainder in an overflow sheet). Panels stack full-width. Data tables become card lists. Charts reduce tick count. |
| 768–1023 | Collapsed icon rail sidebar. Two-column panel grids. |
| 1024–1439 | Full sidebar with labels. Three-column grids. |
| ≥ 1440 | Sidebar, content capped at 1600px, extra width given to charts rather than empty margin. |

Additional rules: no horizontal scroll at any width; the status strip stays visible on
mobile in condensed form; sticky elements reserve offset so content is never hidden
beneath them; `min-h-dvh` rather than `100vh`.

---

## 12. Accessibility

- WCAG 2.1 AA: 4.5:1 body text, 3:1 large text and UI glyphs, verified per theme
- Every interactive element keyboard-reachable; focus ring 2px `--primary` at 2px offset
- Focus moves to the main region on route change
- Skip-to-content link
- Logical heading order, no skipped levels
- Icon-only buttons carry `aria-label`
- Live regions: `aria-live="polite"` for value updates and toasts; `role="alert"` for
  errors. Toasts never steal focus
- All charts have a text summary and a table alternative
- Touch targets ≥44×44px with ≥8px separation
- `prefers-reduced-motion` fully honoured
- Text scales to 200% without loss of content or function
- Form fields have visible labels, helper text, errors adjacent to the field, and
  `aria-describedby` wiring

---

## 13. State handling

Every data-bearing surface implements five states. Building them is not optional — the
resilience score depends on them.

| State | Treatment |
|---|---|
| **Loading** | Skeleton in the shape of the incoming content, after 300ms |
| **Empty** | Explains what would appear here and why it is empty, with a relevant action |
| **Error** | States the cause and the recovery path — never a bare "Something went wrong" |
| **Stale** | Content desaturated, age shown, status strip flag raised |
| **Degraded** | Names the unavailable capability and what replaced it |

---

## 14. Testing

- **Unit** — world engine determinism (same seed ⇒ same state), Guide §5.2 validation order,
  `format.ts` edge cases, error-envelope discrimination
- **Component** — each screen against empty, healthy, stale, and faulted states
- **Accessibility** — axe on every route, keyboard traversal of primary flows
- **Visual** — both themes, 375px and 1440px, on the primary screens

Vitest and Testing Library. Playwright for the reduced set of end-to-end flows.

---

## 15. Definition of done

The phase is complete when:

1. `npm run build` and `npm run typecheck` pass clean
2. All 12 routes are reachable and render
3. Every screen renders correctly with **zero** data, with **healthy** data, and under an
   **active fault**
4. Both themes verified, with contrast checked independently in each
5. Verified at 375px, 768px, 1024px, and 1440px with no horizontal scroll
6. Keyboard-only traversal completes the primary flow: dashboard → risk → decision →
   allocation → confirmation
7. Reduced-motion verified, including chart freezing
8. `VITE_SIMULATOR_MODE=live` points at a real simulator with no application-code change
9. No emoji used as icons; no raw hex in components; no banned pattern from §3.1 present

---

## 16. Risks and assumptions

| Risk | Mitigation |
|---|---|
| Simulator is unavailable or misbehaves at judging | Mock mode runs the entire demo standalone; `live` is a config flag, not a dependency |
| Simulator's real response shapes differ from the guide | Types are transcribed verbatim from the guide; the HTTP client isolates any drift to one file |
| Scope is large for the remaining time | Screens are independent; the demo-critical path (2 → 5 → 7 → 6 → 8 → 10) is built first |
| Density hurts usability | Density is applied to data presentation, never to touch targets or control spacing |
| A judge views a projector with poor colour | Every status carries icon and text alongside colour |

**Assumptions:** the simulator will be reachable at `http://localhost:8000` in live mode;
the Guide §8 world definition is authoritative; no authentication is required.

---

## 17. References

- `Fuel_Supply_Intelligence_Resilience_Platform.md` — challenge brief
- `BUP_Fuel_Supply_Simulator_Integration_Guide_Final.md` — simulator contract
- Design system: *Real-Time Monitoring* style, Fira Sans / Fira Code, blue + amber accent
