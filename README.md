# Fuel Supply Intelligence & Resilience Platform

A full-stack decision-support platform for the **BUP CSE Fest 2026 Hackathon
Finals** challenge. It surfaces the state of a simulated national fuel supply
network — depots, stations, routes, inventories, allocations and disruption
events — and gives an operator the tools to see degradation, understand its
cause, and review the simulated response.

The repository holds three parts:

- **`frontend/`** — the React + Vite + TypeScript operator console, served by
  nginx in the deployment (port `8080`).
- **`backend/`** — the FastAPI intelligence service (port `9000`). It is the
  single gateway to the simulator, and it owns the forecasting, detection and
  allocation engines, the explanation service, the persistence layer and the
  observability surface.
- **`simulator-api`** — the organisers' published Docker image, pulled
  unmodified. It is never patched, wrapped or replaced.

> **All figures shown by this system are simulated.** The network, the
> inventories, the disruption events, every forecast, every recommendation and
> every allocation decision come from the supplied BUP Fuel Supply Simulator,
> or (in the frontend's default development configuration) from a deterministic
> model running in the browser. Nothing here reflects real-world fuel
> conditions, and nothing here contacts real fuel infrastructure. See
> [Simulated-data boundary](#simulated-data-boundary) below.

---

## Quick start

### With Docker (recommended — this is the submitted deployment)

Requires only Docker with the Compose plugin. Nothing else — no Node, no local
Python, no simulator source.

```bash
docker compose up --build
```

This starts **three** containers and wires them together:

| Service | Container | Published on | Purpose |
| --- | --- | --- | --- |
| `simulator-api` | `fuel-simulator-api` | `8000` | The published BUP simulator image, unmodified |
| `backend` | `fuel-supply-backend` | `9000` | The FastAPI intelligence service |
| `frontend` | `fuel-supply-frontend` | `8080` | The operator console, served by nginx |

Once it is up:

| What | URL |
| --- | --- |
| Operator console | <http://localhost:8080> |
| Backend API — Swagger UI | <http://localhost:9000/docs> |
| Backend metrics (Prometheus exposition) | <http://localhost:9000/metrics> |
| Backend component status | <http://localhost:9000/api/v1/status> |
| Simulator's own dashboard | <http://localhost:8000/admin> |
| Simulator's own Swagger UI | <http://localhost:8000/docs> |

To stop everything:

```bash
docker compose down
```

The backend waits on `simulator-api` being *healthy*, not merely started, so the
API does not come up against a simulator that is still initialising. Its own
healthcheck probes `GET /api/v1/health` — deliberately not `/api/v1/status`,
which probes the simulator, the database and the LLM. An orchestrator must not
restart a healthy API because the simulator blipped.

### If port 8000 is already in use

The simulator's host port is configurable, because a machine frequently already
has something on `8000`. Neither the console nor the backend uses the
simulator's host port inside Compose — both reach it over the Compose network —
so moving it changes nothing about how the deployed application works:

```bash
SIMULATOR_HOST_PORT=8001 docker compose up --build
```

The console is still at <http://localhost:8080>. (This is *not* true in the
direct-run mode below, where the host port is exactly what the backend must be
told about.)

### Without Docker (development)

#### Backend

The backend has a virtualenv at `backend/.venv` and reads its configuration
from `backend/.env`. Run it **from the `backend/` directory**, because
`app.config` loads `.env` relative to the working directory:

```bash
cd backend
cp .env.example .env      # then edit .env
```

On Windows:

```bash
./.venv/Scripts/python.exe -m uvicorn app.main:app --port 9000
```

On POSIX:

```bash
.venv/bin/python -m uvicorn app.main:app --port 9000
```

It then serves on <http://localhost:9000>, with Swagger UI at `/docs` and
Prometheus metrics at `/metrics`.

**The simulator's host port matters in this mode.** The committed default for
`SIMULATOR_BASE_URL` is `http://simulator-api:8000`, which is the Compose
service name and does **not** resolve on the host. Running directly, the backend
reaches the simulator over the host network, so point it at whichever host port
the simulator is published on — for example, if you started the stack with
`SIMULATOR_HOST_PORT=8001`, set:

```
SIMULATOR_BASE_URL=http://localhost:8001
```

in `backend/.env`. Reaching the simulator over the Compose network from a
directly-run backend is not possible; only the published host port is.

A note on what "running" means here: the backend boots even when the simulator
and the database are both unreachable. A failed `Repository.init()` or
`SimulatorClient.start()` is logged and tolerated, `GET /api/v1/health` still
answers, and `/api/v1/status` reports the affected component as degraded. That
is deliberate (see [Architecture](#architecture) below), not an accident.

#### Frontend

```bash
cd frontend
npm install
npm run dev
```

The dev server runs at <http://localhost:5173>. By default it needs no simulator
and no backend at all — it runs the in-browser world engine (see
[Two data modes](#two-data-modes)).

---

## Configuration

There are two configuration paths because there are two ways to run the backend.

- **The Docker path** reads the root **`.env`**, which `docker compose` loads
  automatically and passes into the containers.
- **The direct-run path** reads **`backend/.env`**, which `app.config` loads
  relative to the working directory.

In both cases **`.env.example` is the committed, secret-free template** and
**`.env` is git-ignored**. `backend/.env.example` and the root `.env.example`
are committed; neither may ever contain a real value. Copy the template, then
edit the copy:

```bash
cp .env.example .env            # Docker path
cp backend/.env.example backend/.env   # direct-run path
```

### Docker path (root `.env`)

These are passed to the containers by `docker-compose.yml`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | *(empty)* | The one secret. Empty is a supported mode — see below. |
| `LLM_ENABLED` | `true` | Set to `false` to force the deterministic explanation path even when a key is present. |
| `BACKEND_HOST_PORT` | `9000` | Host port the backend API is published on. |
| `SIMULATOR_HOST_PORT` | `8000` | Host port the simulator is published on. Convenience only inside Compose. |
| `SIMULATION_SPEED` | `8` | Wall-clock ticks per second (read by the simulator). |
| `TICK_MINUTES` | `15` | Simulated minutes advanced per tick (read by the simulator). |
| `SIMULATOR_START_MODE` | `running` | The simulator's own default is `paused`, which leaves the clock at tick 0 until someone resumes it. Defaulted to `running` here so a fresh `docker compose up` is immediately useful. |
| `LOG_LEVEL` | `INFO` | Log verbosity for the backend's structlog JSON pipeline. |

`docker-compose.yml` also fixes, rather than templating, `SIMULATOR_BASE_URL`
(`http://simulator-api:8000`) and `DATABASE_URL`
(`sqlite+aiosqlite:////app/data/fuel.db`) inside the backend container.

### Direct-run path (`backend/.env`)

Every one of these has a default in `backend/app/config.py`, so the service
boots with none of them set.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SIMULATOR_BASE_URL` | `http://simulator-api:8000` | Root URL of the simulator. Use `http://localhost:8001` (or the published port) for a host-run simulator. |
| `SIMULATOR_TIMEOUT_SECONDS` | `10` | Per-request timeout for every simulator call. |
| `SIMULATOR_MAX_RETRIES` | `3` | Retries on 5xx and transport errors only — never on a 4xx, which is not a transient failure. |
| `CIRCUIT_FAILURE_THRESHOLD` | `5` | Consecutive failures tolerated before the circuit breaker opens. |
| `CIRCUIT_RESET_SECONDS` | `30` | How long the breaker stays open before a single half-open trial. |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/fuel.db` | SQLAlchemy async URL. SQLite needs no infrastructure; the scheme can be swapped for Postgres without touching code. |
| `DEEPSEEK_API_KEY` | *(empty)* | The one secret. |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | DeepSeek's OpenAI-compatible API root; the client appends `/chat/completions`. |
| `DEEPSEEK_MODEL` | `deepseek-chat` | Model id sent in the request body. |
| `DEEPSEEK_TIMEOUT_SECONDS` | `30` | Timeout for one completion. On timeout the client retries once, then the service falls back. |
| `LLM_ENABLED` | `true` | Master switch for the LLM path. `true` is not enough on its own — the key must also be present. |
| `API_PORT` | `9000` | Port uvicorn listens on (used by the container's healthcheck; the command line can override it). |
| `LOG_LEVEL` | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `CORS_ALLOW_ORIGINS` | `http://localhost:8080,http://localhost:5173` | Comma-separated browser origins allowed to call the API. Only relevant when the console is served from a different origin than the API. |

### Build-time (baked into the frontend bundle)

Vite inlines `import.meta.env` **at build time**, so these are build arguments,
not runtime environment variables. Changing them requires a rebuild.

| Build arg | Value in the image | Meaning |
| --- | --- | --- |
| `VITE_SIMULATOR_MODE` | `live` | Use the HTTP client rather than the in-browser model. |
| `VITE_SIMULATOR_BASE_URL` | *(empty)* | Base URL for API calls. Empty means same-origin. |

### Why the base URL is empty, and why nginx proxies

The integration guide documents no CORS policy for the simulator. A page served
from `:8080` calling `:8000` is a cross-origin request, and if the simulator does
not send the matching CORS headers the browser blocks it.

So the image is built with an empty base URL, which makes the app issue
same-origin requests to `/v1/*` on its own origin, and nginx proxies those to the
simulator over the Compose network. The browser never makes a cross-origin
request, so the console works whether or not the simulator sends CORS headers.

Two details make this work and are easy to break:

- The client falls back with `baseUrl ?? DEFAULT_BASE_URL`. Nullish coalescing
  does **not** fire on the empty string, which is exactly what keeps `""`
  meaning "same origin". Changing that `??` to `||` would silently route the
  containerised app at `http://localhost:8000` — i.e. the operator's own machine
  rather than the simulator container.
- nginx resolves the upstream through a variable (`set $upstream ...`), which
  defers the lookup to request time. With a literal hostname nginx resolves once
  at startup and **refuses to start** if the name is unknown, so an unreachable
  simulator would crash-loop the console instead of letting it render its own
  error state.

To call the simulator directly instead — which *does* require it to send CORS
headers — build with:

```bash
docker build -f frontend/Dockerfile \
  --build-arg VITE_SIMULATOR_BASE_URL=http://localhost:8000 \
  -t fuel-supply-frontend frontend
```

---

## The DeepSeek API key

`DEEPSEEK_API_KEY` is the only secret in the system. It is handled as follows,
and each of these is a property of the code rather than an intention:

- **Read only from the environment.** It comes from the process environment or
  from a git-ignored `.env` through `pydantic-settings`. There is deliberately
  no plausible-looking default: the default in `app/config.py` is the empty
  string.
- **Never hard-coded.** No key literal appears anywhere in the repository; the
  committed `*.env.example` files carry an empty value.
- **Never logged.** The field is declared `repr=False`, so it cannot leak
  through `repr(settings)`; the client's own `__repr__` excludes it; and every
  failure message the client builds is passed through a redaction helper that
  strips the configured key and credential-shaped substrings before it can reach
  a log line.
- **Never returned by any endpoint.** `/api/v1/status` reports the LLM component
  with a status, a human-readable detail and the model id — never the key.
- **Never sent to the model.** The request body contains only the system and
  user messages. The key is used solely in the `Authorization` header of the
  outbound DeepSeek request.

To use it, put your own value in `backend/.env` (direct-run) or the root `.env`
(Docker), never in a committed file:

```
DEEPSEEK_API_KEY=sk-your-key-here
```

**Leaving it empty is a supported mode, not a broken one.** When the key is
absent, `Settings.llm_available` is `False` and no request is attempted. Every
explanation is instead produced by a deterministic templated explanation built
from the *same structured facts* the model would have been given, and the
response reports `source: "fallback"` with `degraded: true`. The operator can
therefore always tell which path produced the text. `/api/v1/status` reports the
LLM component as `degraded`, never `down`, because the fallback path is working
as designed rather than failing. Setting `LLM_ENABLED=false` forces the same
fallback even when a key is present, which is useful for demonstrating the
degradation behaviour deliberately.

(This is verified against `backend/app/llm/`: `explain.py` selects the fallback
whenever the client reports itself unavailable, and `deepseek.py` reports
`unavailable_reason="api_key_missing"` when the key is empty.)

---

## API surface

All paths are served by the backend on port `9000`. The table below is taken
from the routers themselves, not from a design document. There are 18 paths.

### Health and observability

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/health` | Liveness probe. Deliberately dependency-free: it touches no simulator, database, LLM or metrics registry, so nothing that can fail sits behind it. |
| `GET` | `/api/v1/status` | Per-component status (`api`, `simulator`, `database`, `llm`, `decision_engine`) plus p95 latency and error rate. Never fails because one component is down. |
| `GET` | `/metrics` | Prometheus text exposition. Mounted by the observability layer and excluded from the OpenAPI schema. |

### Network

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/network/snapshot` | The current simulated network: instance, depots, stations, routes, regions, supply arrivals, events and simulator metrics, with staleness and age. |
| `GET` | `/api/v1/network/demand-history` | Proxied and validated demand history, optionally filtered by station, fuel type and limit. |

### Intelligence

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/forecast` | Demand forecast and stockout risk per station and fuel, over a tick horizon. |
| `GET` | `/api/v1/risk` | Current risk signals, optionally filtered by severity and kind. |
| `GET` | `/api/v1/recommendations` | Ranked, inspectable allocation recommendations, with an optional budget. |
| `GET` | `/api/v1/recommendations/{id}/explanation` | Explanation of one recommendation — LLM-backed, or the deterministic fallback. |
| `POST` | `/api/v1/recommendations/{id}/submit` | The **only** write path. Submits an allocation to the simulator and requires an explicit `{"confirm": true}`. |

### Events

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/events` | Crisis events from the simulator, optionally filtered by status and type. |
| `POST` | `/api/v1/events/summary` | A network-state summary (LLM or deterministic fallback). |
| `POST` | `/api/v1/investigate` | Answer an operator's investigation question (LLM or deterministic fallback). |

### Decisions

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/decisions` | The decision audit history, paged. |
| `GET` | `/api/v1/decisions/{id}` | One decision and its recorded outcome. |

### Admin (simulator scenario control)

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/admin/faults` | List the faults currently injected into the simulator. |
| `POST` | `/api/v1/admin/faults` | Inject a fault (delay or error mode) — the demonstration of the resilience path. |
| `POST` | `/api/v1/admin/simulation/{action}` | `run`, `pause`, `step` or `reset` the simulation. An unknown action is rejected with 422. |

### What needs what

- `/api/v1/health` needs **no dependencies at all**.
- `/metrics` needs no simulator — it is a local Prometheus exposition.
- `/api/v1/status` probes the simulator, the database and the LLM but does not
  fail when they are unavailable; it reports them.
- `/api/v1/decisions` and `/api/v1/decisions/{id}` need the **database only** —
  no simulator.
- Everything else needs the **simulator**, directly or to assemble the network
  snapshot. Where the simulator is unreachable, an appropriate degraded
  behaviour applies (see [Architecture](#architecture)); none of them invent a
  value.
- The three explanation routes (`…/explanation`, `/events/summary`,
  `/investigate`) need the **LLM** for their preferred path but fall back to the
  deterministic template, so they do not fail when it is unavailable.

---

## Architecture

### The arrow that matters

The backend is what stands between the simulator and everything else. It is the
only component that talks to the simulator, the only one that validates what the
simulator says, and the only one that can submit an allocation. That is what
makes the degradation behaviour below possible at all — a component that does
not own its dependency cannot degrade gracefully around it.

### Layering

```
backend/app/
  api/            FastAPI routers — the /api/v1 surface
  intelligence/   forecast.py, detect.py, allocate.py — the pure engines
  sim/            SimulatorClient, CircuitBreaker, typed models, error normalisation
  store/          SQLAlchemy 2.0 + SQLite repository: snapshots, demand points,
                  decisions, alerts, llm_calls
  llm/            DeepSeekClient, ExplanationService, prompt construction
  resilience/     policy.py — the degradation policy, declared as data
  observability/  metrics, structlog, request middleware, /metrics
  ingest.py       the background poller that persists what the simulator returns
  config.py       settings
  main.py         app factory, lifespan, router mount
```

Requests flow `api` → `intelligence` → the `sim` gateway → the simulator.
`store` provides persistence and is read by the API and the engines. The
intelligence engines are pure: none of them touches the network or the database,
which is what makes a forecast reproducible and a recommendation inspectable.
`observability` is cross-cutting — the metrics middleware wraps every request,
and each engine and gateway records into the same registry. `resilience/policy.py`
is a leaf module: it imports nothing from the rest of the application, so any
layer can import it without creating a cycle.

### Ingestion

The API is a reader. The thing that actually writes to the database is a
background task, `SnapshotIngestor`, started in the application lifespan. It
polls the simulator on an interval (2 seconds by default) and persists what it
finds:

- **Snapshots, but only when the tick advanced.** The simulator steps several
  times a second, so recording unconditionally would insert thousands of
  near-identical rows a minute. The tick of the last persisted snapshot is
  remembered and an unchanged tick is skipped.
- **Demand history, de-duplicated per `(station_id, fuel_type)`.** The
  simulator's history is a sliding window, so the same ticks come back on every
  poll. The highest persisted tick is tracked per series and re-seeded from the
  database on first sight, so a restart does not re-insert the whole window.

Every poll is wrapped. A simulator outage, a decode error, a database error or
an open circuit breaker is logged and the loop continues on the next interval.
If the repository is absent, or its database never initialised, the poller
records nothing and says so once rather than hammering a database already known
to be broken. This module owns the *schedule* only — the simulator client owns
resilience, and the repository owns the storage API; neither is reimplemented
here.

Without ingestion the repository is inert: every table stays empty and the
forecaster reads an empty series. It is the caller the write paths needed.

### Resilience and degradation

The brief requires the platform to define what happens when something goes
wrong. Four rules are implemented, each verified in the code:

- **Simulator circuit breaker → last-good cached snapshot, marked stale.**
  After `CIRCUIT_FAILURE_THRESHOLD` (default 5) consecutive failures the breaker
  opens for `CIRCUIT_RESET_SECONDS` (default 30). While it is open, reads serve
  the last-good value cached from a successful read, with `stale=true` and
  `age_seconds` set to the age of that value. With nothing ever cached — a cold
  start into a dead simulator — the request raises a typed error rather than
  fabricating a snapshot: degrade, do not invent. (`app/sim/client.py`,
  `app/sim/circuit.py`.)
- **LLM unavailable → templated explanation.** A missing key, `LLM_ENABLED=false`,
  a timeout, a transport error or a response that fails validation all produce a
  deterministic explanation built from the same structured facts, marked
  `source="fallback"` and `degraded=true`. The explanation routes never return a
  500 because the model is missing. (`app/llm/explain.py`, `app/llm/deepseek.py`.)
- **Optimiser infeasible → priority heuristic.** The allocation engine tries
  `scipy.optimize.linprog` first. If the solver is unavailable, infeasible,
  unbounded or raises, it falls back to a transparent severity × probability ×
  volume ranking and reports `policy="priority_heuristic"` in the response, so
  the answer itself says the optimiser did not run. The same caps are enforced
  either way. (`app/intelligence/allocate.py`.)
- **Database unavailable → serve degraded rather than crash.** A failed
  `Repository.init()` at boot is logged and tolerated: the process still starts,
  `/api/v1/health` still answers, and `/api/v1/status` reports the database
  component as `down`. Reads that do reach a broken database raise a typed error
  rather than a stack trace. (`app/main.py`, `app/api/decisions.py`,
  `app/api/health.py`.)

There is one further thing worth stating plainly: the degradation policy is also
declared as a table of data in `app/resilience/policy.py`, but the API call
sites do **not** yet consult it. See [Known gaps](#known-gaps-and-honest-limits).

<a id="two-data-modes"></a>

### Two data modes, one interface

Every screen reads its data through a single `SimulatorClient` interface. Two
implementations satisfy it, and which one is used is a build-time decision:

| Mode | Implementation | What it does |
| --- | --- | --- |
| `mock` (default in development) | `src/lib/api/mock/` | A deterministic in-browser model of the network. Seeded PRNG, simulated clock, demand model, allocation ledger, fault injection. No network. |
| `live` (used in the Docker image) | `src/lib/api/http/` | Real HTTP and Server-Sent Events against the BUP Simulator. |

This is the reason the console can be demonstrated with no simulator running, and
the reason a judge can point it at the published image and see identical screens
driven by real responses. The screens do not know which mode they are in.

The mock engine is deterministic — the same seed produces the same network, the
same disruptions and the same allocations — so a bug is reproducible and a
screenshot can be reproduced.

### The three error envelopes

The simulator can fail in three shapes, and all three are normalised into one
`SimulatorError` before any screen sees them:

```jsonc
{ "detail": { "code": "...", "message": "..." } }   // structured
{ "error":  { "code": "...", "message": "..." } }   // alternative
{ "detail": [ { "loc": [...], "msg": "..." } ] }    // FastAPI/Pydantic 422
```

A screen that only understood the first would render a validation failure as a
blank list.

### REST is the source of truth; the event stream is only a hint

The console subscribes to the simulator's Server-Sent Events stream, but an event
never carries the data a screen renders. Every event triggers a fresh REST
request. The stream is a low-latency *signal that something changed*, and the
response body is the fact.

This is deliberate. It means a dropped, duplicated or out-of-order event cannot
put a wrong number on the screen — only a slightly late one. It also means there
is no event replay to reason about: the console does not depend on
`Last-Event-ID` or on receiving every event.

### Freshness travels with the data

Every response is wrapped as `{ data, stale, receivedAt }`. Staleness and age are
properties of *that response*, not global state, so a screen can show a fresh
figure from one endpoint beside a stale one from another — which is what actually
happens when the simulator degrades partially.

A response marked stale is presented as stale. It is never silently redrawn as
current. The backend expresses the same idea with `stale` and `age_seconds` on
the snapshot it serves, so a stale value is labelled at the API boundary as well
as in the console.

### Frontend layering

```
src/
  app/          shell, navigation, routing
  features/     one directory per screen
  components/   shared presentational primitives
  design/       design tokens and the theme provider
  icons/        inline SVG icon set
  lib/
    api/        SimulatorClient, the http and mock implementations
    hooks/      data hooks
    types/      the simulator contract, transcribed from the integration guide
```

Feature code may not reach into `lib/api/mock` or `lib/api/http` directly — it
goes through the client interface. An ESLint `no-restricted-imports` rule scoped
to `src/features/**` enforces this, so the boundary is a build failure rather
than a convention.

---

## Testing

### Backend

```bash
cd backend
./.venv/Scripts/python.exe -m pytest tests -q    # Windows
.venv/bin/python -m pytest tests -q              # POSIX
```

Run as observed on this repository:

```
727 passed, 1 warning in 25.71s
```

The single warning is a third-party deprecation notice from Starlette's test
client, not a failure in this code. The suite covers the simulator client and
its error normalisation, the circuit breaker, the store, the three engines, the
LLM client and its fallback path, the resilience policy table, observability,
configuration, and every API route.

### Frontend

```bash
cd frontend
npm test          # vitest run
npm run lint      # eslint, zero warnings tolerated
npm run typecheck # tsc --noEmit
npm run build     # typecheck + production bundle
```

The first three were run as part of writing this document: `npm test` reported
`167 passed` across 13 files, and `npm run lint` and `npm run typecheck` both
completed cleanly. `npm run build` is listed but was not run in that pass.

The frontend suite runs against the mock client, so it needs no simulator and no
network. It covers the data layer, the error normalisation, the world engine,
the shared components and each screen's behaviour — including the failure paths:
faults surfaced as errors rather than empty lists, stale data marked stale, and
the degraded states.

### Load testing

A Locust workload exists at `backend/loadtest/locustfile.py`. It targets the
recommendations endpoint (the most expensive path, because that one call
composes the whole intelligence chain) with `/api/v1/health` as a cheap
baseline, and it is read-only by design — it never calls the submission
endpoint. To run it:

```bash
python -m locust -f backend/loadtest/locustfile.py --headless \
    --users 50 --spawn-rate 5 --run-time 2m \
    --host http://localhost:9000 \
    --csv backend/loadtest/results
```

**No load-test figures are recorded in this document.** The workload is
runnable, but it has not been run against a fully started stack as part of this
write-up, and no numbers will be invented. `backend/loadtest/README.md` explains
how to read the results.

---

## Simulated-data boundary

This is a decision-support exercise against a simulator, and the whole system
says so rather than leaving it implied.

- The console is labelled as simulated in the UI at all times.
- Nothing here reads from, writes to, or authenticates against real fuel
  infrastructure. There are no real credentials anywhere in this repository.
- "Allocations" are simulated assignments. No purchase, order or dispatch is
  ever placed.
- Figures are presented as the simulator reports them, with their age and
  staleness visible, so a number is never mistaken for a live reading of the
  real world.
- Consequential simulated decisions keep a human in the loop: the platform
  proposes and explains, and the operator confirms. This is structural, not
  advisory — the only write endpoint, `POST /api/v1/recommendations/{id}/submit`,
  rejects any request without an explicit `{"confirm": true}`, before it reads
  or writes anything.
- Every API response that carries a prediction, recommendation or explanation
  includes `"simulated": true`.

### Deliberate scope limits

Both halves of the system stay inside the simulation. The **backend** holds the
forecasting, anomaly-detection and constrained-allocation models, but they run
against the simulator's data only, and they advise rather than act. The
**frontend** contains no forecasting or optimisation model of its own — it
presents what it is given and makes it legible to an operator. Its in-browser
world engine exists so the interface can be developed, demonstrated and tested
without the simulator running; it is a stand-in for the simulator, not a second
implementation of the domain.

The simulator image is used **unmodified**. It is pulled as published:

```
asifmahmoud414/bup-fuel-supply-simulator:1.0.0
```

No source in this repository patches, wraps or replaces it.

---

## Known gaps and honest limits

These are recorded plainly, because a judge will find them anyway and a
documented limitation is worth more than an undocumented one.

1. **The console does not yet consume the backend API.** This was checked, not
   assumed: no backend URL, no port `9000` and no `/api/v1` path appears
   anywhere in `frontend/src`, and `frontend/nginx/default.conf.template`
   proxies `/v1/*` directly to the simulator. The console therefore renders the
   simulator's own data, and does **not** show the backend's forecasts, risk
   signals, recommendations or degradation state. The backend API is complete
   and reachable on `:9000`, but the deployed operator experience does not yet
   depend on it. A screen or two pointing at `/api/v1/*` would close this.

2. **`backend/app/resilience/policy.py` declares the degradation policy as a
   table but is not imported by the API layer.** The policy module and its tests
   exist; the call sites in `app/api/` reproduce the same decisions inline
   rather than looking them up. So the declared table is, today, a
   documentation-and-test artefact rather than the single source the call sites
   consult. This is a wiring gap, not a missing design.

3. **The policy names a generic `fallback_activated_total` counter that the
   metrics layer and the contract do not declare** (they declare the LLM and
   forecast fallback counters and the circuit-breaker gauge specifically). A
   fallback activation outside the LLM and forecast paths would be visible in
   the logs but not counted in the metrics.

4. **The ingestion task records no metrics.** `SnapshotIngestor` accepts a
   metrics object for symmetry with the other collaborators but does not use it,
   because the metrics layer declares no ingestion hook; inventing one was
   judged worse than recording nothing. Ingestion activity is visible in the
   structured logs only.

5. **The deployment is single-node.** One backend container, SQLite on a named
   volume. The circuit breaker and last-good cache protect one process's view of
   one dependency; there is no replication, no distributed failover and no queue
   between the API and the simulator. A swap to PostgreSQL is a `DATABASE_URL`
   change, but it has not been tested.
