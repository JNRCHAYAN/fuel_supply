# Architecture

**Fuel Supply Intelligence & Resilience Platform** — BUP CSE Fest 2026 Hackathon
Finals.

This document is brief §19 deliverable #6, the architecture diagram, plus the
documented assumptions §24 requires and the load-test evidence §19 #10 requires.
It is written to be checkable against the code: every claim below names the
module or file that backs it, and anything not yet measured says so.

> **Simulated-data boundary.** Everything in this system operates against the
> organizer-provided BUP Fuel Supply Simulator. No component contacts real fuel
> infrastructure, holds real credentials, or places a real purchase or dispatch.
> Every API response carrying a prediction, recommendation or explanation
> includes `"simulated": true` (brief §24, `CONTRACT.md` §0.6). No figure here
> describes real-world fuel conditions.

---

## 1. The pipeline

The brief asks for a clear view of **simulator → data/backend → intelligence →
decision → application → monitoring** (§19 #6). Those six bands, and what sits
in each:

```
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │ 1. SIMULATOR                                    (organizer-provided, unmodified)│
 │                                                                              │
 │    BUP Fuel Supply Simulator — asifmahmoud414/bup-fuel-supply-simulator:1.0.0 │
 │    REST /v1/*  (27 paths, verified against its own /openapi.json)             │
 │    SSE  /v1/stream                                                            │
 │    depots · stations · routes · regions · inventories · demand ·             │
 │    supply arrivals · events · allocations · metrics                           │
 └───────────────────────────────┬──────────────────────────────────────────────┘
                                 │  httpx (async) — REST is the source of truth,
                                 │  SSE is a hint that something changed
                                 │  timeout · retry(5xx/transport only) ·
                                 │  circuit breaker · last-good cache
 ┌───────────────────────────────▼──────────────────────────────────────────────┐
 │ 2. DATA / BACKEND GATEWAY                          app/sim/  ·  app/store/   │
 │                                                                              │
 │    SimulatorClient   typed reads/writes, envelope normalisation               │
 │    CircuitBreaker    closed → open → half-open                                │
 │    Last-good cache   every successful read stored as (value, monotonic_ts)     │
 │    Repository        SQLAlchemy 2.0 + SQLite: snapshots, demand_points,        │
 │                      decisions, alerts, llm_calls                              │
 │                                                                              │
 │    Validated at the boundary: the simulator's responses are external input    │
 │    and are Pydantic-checked before anything downstream trusts them.           │
 └───────────────────────────────┬──────────────────────────────────────────────┘
                                 │  Snapshot + demand history (validated, typed)
        ┌────────────────────────┼───────────────────────────┐
        ▼                        ▼                           ▼
 ┌──────────────┐      ┌──────────────────┐        ┌────────────────────┐
 │ 3. INTELLIGENCE      │     app/intelligence/        │                    │
 │              │      │                  │        │                    │
 │ forecast.py  │      │ detect.py        │        │ allocate.py        │
 │ DemandFore-  │      │ AnomalyDetector  │        │ AllocationEngine   │
 │ caster       │      │ robust z-score   │        │ linprog LP, then   │
 │ seasonal /   │      │ (median + MAD)   │        │ priority heuristic │
 │ Holt-Winters │      │ → RiskSignal[]   │        │ → Recommendation[] │
 │ → Forecast   │      │   severity from  │        │   rationale,       │
 │   confidence │      │   the numbers    │        │   constraints,     │
 │              │      │                  │        │   alternatives     │
 └──────┬───────┘      └────────┬─────────┘        └─────────┬──────────┘
        │                       │                            │
        └───────────┬───────────┴────────────┬───────────────┘
                    ▼                        ▼
        ┌───────────────────────┐   ┌───────────────────────────────┐
        │ 4. DECISION / EXPLAIN │   │  5. APPLICATION                │
        │        app/llm/       │   │        app/api/                │
        │                       │   │                               │
        │ DeepSeekClient        │   │  /api/v1/* FastAPI routers     │
        │ ExplanationService    │   │  port 9000                     │
        │  LLM explains the     │   │  · network snapshot           │
        │  decision the engines │   │  · forecast / risk            │
        │  already made — it    │   │  · recommendations            │
        │  never makes one      │   │  · decisions (audit history)  │
        │  fallback: templated  │   │  · events / investigate       │
        │  deterministic        │   │  · admin (fault injection)    │
        │  explanation          │   │  operator-initiated submit    │
        └───────────┬───────────┘   └───────────────┬───────────────┘
                    │                               │
                    │                               ▼
                    │                 ┌──────────────────────────────┐
                    │                 │  Operator console (React)    │
                    │                 │  nginx :8080                 │
                    │                 └──────────────┬───────────────┘
                    │                                │
                    └────────────┬───────────────────┘
                                 ▼
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │ 6. MONITORING                                              app/observability/ │
 │                                                                              │
 │    Application   http_requests_total{method,path,status},                     │
 │                  http_request_duration_seconds (histogram → p95)              │
 │    System        process_cpu_seconds_total, process_resident_memory_bytes      │
 │    Intelligence  forecast_confidence, forecast_fallback_total,                 │
 │                  risk_signals_total{kind,severity},                            │
 │                  recommendations_generated_total, recommendation_confidence,   │
 │                  llm_calls_total{purpose,result}, llm_latency_seconds,         │
 │                  llm_fallback_total{reason}, circuit_breaker_state{component}  │
 │    Logs          structlog JSON — one line per request, per decision,          │
 │                  per fallback activation, per recovery                         │
 │                                                                              │
 │    Scraped at GET /metrics  ·  summarised at GET /api/v1/status               │
 └──────────────────────────────────────────────────────────────────────────────┘
```

### The arrow that matters

The backend is what stands between the simulator and everything else. It is the
only component that talks to the simulator, the only one that validates what the
simulator says, and the only one that can submit an allocation. That is what
makes the degradation behaviour in §5 possible at all — a component that does
not own its dependency cannot degrade gracefully around it.

---

## 2. Deployment topology

`docker compose up --build` (brief §12, §19 #7). Three containers on one compose
network:

```
        host :8080                 host :9000              host :8000
            │                          │                       │
            ▼                          ▼                       ▼
   ┌─────────────────┐        ┌─────────────────┐      ┌──────────────────┐
   │ frontend        │        │ backend         │      │ simulator-api    │
   │ nginx + SPA     │        │ uvicorn         │      │ published image  │
   │ fuel-supply-    │        │ fuel-supply-    │      │ fuel-simulator-  │
   │ frontend        │        │ backend         │      │ api              │
   └─────────────────┘        └────────┬────────┘      └────────┬─────────┘
                                       │  SIMULATOR_BASE_URL    │
                                       └────────────────────────┘
                                          over the compose network

                                       backend-data (named volume)
                                       → /app/data/fuel.db
```

| Service | Image | Host port | Healthcheck |
| --- | --- | --- | --- |
| `simulator-api` | `asifmahmoud414/bup-fuel-supply-simulator:1.0.0` (unmodified) | `${SIMULATOR_HOST_PORT:-8000}` | `GET /v1/health` |
| `backend` | built from `backend/Dockerfile` | `${BACKEND_HOST_PORT:-9000}` | `GET /api/v1/health` |
| `frontend` | built from `frontend/Dockerfile` | `8080` | `GET /` |

The backend waits on `simulator-api` being *healthy*, not merely started, so the
API does not come up against a simulator that is still initialising.

### Why the backend container is built the way it is

`backend/Dockerfile`:

- **Multi-stage.** A builder stage installs the pinned dependency tree into a
  virtualenv; the runtime stage copies only that virtualenv. No compiler, no pip
  cache and no build artefacts reach the shipped image.
- **Dependencies before source.** `requirements.txt` is installed in its own
  layer above the source copy, so an application-code edit costs a rebuild of
  one small layer rather than a reinstall of numpy and scipy.
- **Non-root.** The container runs as UID 1001 (`app`), created numerically so
  a `runAsNonRoot` policy has something to match. `/app/data` is created and
  chowned before the user switch, so the SQLite volume is writable without ever
  running the process as root.
- **Healthcheck on `/api/v1/health`** — deliberately not `/api/v1/status`, which
  probes the simulator, the database and the LLM. An orchestrator must not
  restart a healthy API because the simulator blipped.
- **No secret in any layer.** `DEEPSEEK_API_KEY` is passed as a runtime
  environment variable and is never a build argument. `backend/.dockerignore`
  excludes `.env` from the build context, so the file cannot be copied into a
  layer even by accident. CI asserts this by running the built image and failing
  if `/app/.env` exists.

---

## 3. Where the data comes from, and what "fresh" means

`CONTRACT.md` §5.5 and the integration guide:

- **REST is the source of truth. An SSE event is only a hint that something
  changed.** Every event triggers a REST re-fetch, and nothing is computed from
  an event payload alone. A dropped, duplicated or reordered event can therefore
  make a number *late*, never *wrong*.
- **Every successful read is cached** as `(value, monotonic_ts)`. When the
  simulator is unavailable, reads serve that cached value with `stale=True` and
  `age_seconds` set — the age travels with the data, so a screen can show a
  fresh figure next to a stale one, which is what partial degradation actually
  looks like.
- **If there is no cached value, the request raises.** Degrade, do not fabricate.
- **Unknown values from the simulator are tolerated, not fatal.** The guide warns
  that organizers may change the environment mid-event, so an unrecognised enum
  value is logged and carried, never allowed to crash a request.

---

## 4. Resilience

Brief §11 requires the team to *define what happens when something goes wrong*.
That definition lives in one file — `backend/app/resilience/policy.py` — as
data, not as prose scattered across call sites. A caller asks
`policy_for(component, condition)` and gets back a named action; an undeclared
path raises rather than quietly succeeding.

### 4.1 Declared degradation paths

Transcribed from `backend/app/resilience/policy.py` (`RULES`, `ACTIONS`). Every
row is covered by `backend/tests/test_resilience_policy.py`.

| Component | Condition | Trigger | Action | Reported as | Human review |
| --- | --- | --- | --- | --- | --- |
| `llm` | `unavailable` | `DEEPSEEK_API_KEY` empty, `LLM_ENABLED=false`, timeout, transport error, or a response that fails validation | `templated_explanation` — deterministic explanation from the same structured facts, `source="fallback"`, `degraded=true` | degraded | no |
| `simulator` | `circuit_open` | `CIRCUIT_FAILURE_THRESHOLD` consecutive failures opened the breaker, and a last-good value exists | `serve_last_good_snapshot_stale` — cached snapshot with `stale=true`, `age_seconds` set | degraded | no |
| `simulator` | `circuit_open_no_cache` | Breaker open with nothing ever cached (cold start into a dead simulator) | `raise_typed_error` — a typed error, never a fabricated snapshot | down | no |
| `simulator` | `invalid_response` | Payload fails typed validation, or an error envelope cannot be normalised | `reject_and_raise_alert` — reject, log, alert, keep the last valid state | degraded | no |
| `simulator` | `stream_disconnected` | SSE read error, or the simulator drops a subscriber more than 200 events behind | `reconnect_and_refetch` — exponential backoff reconnect, re-fetch over REST | degraded | no |
| `decision_engine` | `optimizer_infeasible` | `scipy.optimize.linprog` reports infeasible/unbounded, or raises | `priority_heuristic` — transparent severity × probability × volume ranking, `policy="heuristic"` | degraded | no |
| `decision_engine` | `optimizer_unavailable` | Optimiser disabled or its dependency missing | `priority_heuristic` — the same fallback from the other trigger | degraded | no |
| `decision_engine` | `low_confidence` | Recommendation confidence below `LOW_CONFIDENCE_THRESHOLD` (0.5) | `human_review_required` — returned with its confidence and flagged, not presented as actionable | degraded | **yes** |
| `forecast` | `low_confidence` | Forecast confidence below the threshold, or `fallback_used` over a horizon the history cannot support | `human_review_required` | degraded | **yes** |
| `database` | `unavailable` | `DATABASE_URL` unreachable, or a read/write raises a `SQLAlchemyError` | `serve_from_memory_degraded` — read-only degradation; nothing is written to a database that is not there | degraded | no |

Two deliberate choices in that table:

- **An absent LLM key reports `degraded`, never `down`.** `CONTRACT.md` §4 and
  §9 make it a supported operating mode: the deterministic explanation path is a
  feature, not a failure. A status endpoint that called it "down" would be
  telling the operator to fix something that is working as designed.
- **Two of the paths refuse to substitute a value** (`raise_typed_error`,
  `reject_and_raise_alert`, and the undeclared default). The others degrade to a
  real, labelled substitute: a template, a cache, a heuristic. What none of them
  do is invent a number.

### 4.2 Demonstrating it (brief §19 #9, §22 steps 11–14)

The fault-injection endpoints make the degradation path demonstrable rather than
asserted:

1. `POST /api/v1/admin/faults` injects a simulator fault (delay or error mode).
2. `GET /api/v1/status` reports the `simulator` component separately, with the
   breaker state and p95 latency.
3. Reads continue, now serving the last-good snapshot marked `stale` with its
   age, instead of returning 5xx.
4. `POST /api/v1/admin/faults/clear`, then watch the breaker go half-open →
   closed and the data go fresh again. The recovery is logged and counted, like
   the degradation was.

A second, always-available demonstration needs no fault injection at all: run
the stack with no `DEEPSEEK_API_KEY`. Every explanation is then the templated
fallback, marked `source="fallback"`, `degraded=true`, and the system is fully
usable.

### 4.3 What is *not* covered

The resilience model is **in-process**: a circuit breaker, a last-good cache and
a typed fallback live inside one backend container. There is no distributed
failover, no replica set, and no queue between the API and the simulator. The
single-node deployment is a deliberate scope decision (see §8), not an oversight.

---

## 5. Observability

Brief §14 names four layers; all four are implemented in `app/observability/`
and all four are exposed at `GET /metrics` in Prometheus format.

| Layer | Evidence |
| --- | --- |
| **Application** | `http_requests_total{method,path,status}`, `http_request_duration_seconds` histogram, p95 derived from it. Path labels are route templates, not raw URLs. |
| **System** | `process_cpu_seconds_total`, `process_resident_memory_bytes` via `prometheus_client`'s ProcessCollector. |
| **Intelligence** | Forecast confidence and fallback counts, risk signals by kind and severity, recommendation generation and confidence, LLM calls/latency/fallback reason, circuit-breaker state per component. This is the layer most submissions omit; it is the one that shows whether the intelligence is working. |
| **Logs** | structlog JSON: one line per request, per decision, per fallback activation, per recovery. |

**`GET /api/v1/status`** is the operator-facing summary required by brief §15:
each component (`simulator`, `database`, `llm`, `decision_engine`) reported
separately, plus p95 latency and error rate from the middleware — so a judge can
read system health without a Prometheus instance.

Two hygiene rules are enforced rather than intended: no secret, API key,
credential or full request body ever reaches a log line or a metric label
(`CONTRACT.md` §0.2), and metric labels are bounded — nothing is labelled by
`station_id`, because a per-station label is unbounded cardinality and would
take the metrics endpoint down under load.

---

## 6. Delivery pipeline

Brief §12 asks for `Source Code → Build → Test → Package → Deploy → Health
Check → Running Application`. `.github/workflows/ci.yml` runs on every push and
every pull request:

```
  push / pull_request
        │
        ├── backend ──────────────────────────────────────────────────────┐
        │   install   pip install -r backend/requirements.txt             │
        │   lint      ruff check  (blocking: E9/F63/F7/F82 real errors)   │
        │   test      pytest tests -v --cov=app                           │
        │   package   docker build -t fuel-supply-backend:ci ./backend    │
        │   verify    image contains no /app/.env                         │
        │             image runs as uid != 0                              │
        │                                                                 │
        └── frontend ─────────────────────────────────────────────────────┤
            install   npm ci                                              │
            lint      eslint, zero warnings tolerated                     │
            typecheck tsc --noEmit                                        │
            test      vitest run                                          │
            build     tsc -b && vite build                                │
                                                                          ▼
                                                          reproducible deployment
                                                          docker compose up --build
```

**The workflow references no secret.** That is not a limitation to work around:
nothing in the build needs one. `DEEPSEEK_API_KEY` is injected at runtime by
compose, and the CI image build proves the image builds without it.

The two verification steps after the build are the interesting ones. The
`.dockerignore` exclusion of `.env` is a security control, so CI asserts it
against the real artefact instead of trusting the file. Same for the non-root
user.

---

## 7. Load-test results

> ### STATUS: TO BE MEASURED — this section is a placeholder
>
> **No load-test figures are recorded here yet, and none will be invented.**
> The workload exists and is runnable; it has not yet been run against a fully
> started stack, so there are no honest numbers to report.

### The workload

`backend/loadtest/locustfile.py` (brief §17, `CONTRACT.md` §11). It targets the
recommendations endpoint — the most expensive and most meaningful path, because
that single call composes the whole intelligence chain — with `/api/v1/health`
as a cheap baseline, plus the recommendation → explanation journey.

| Task | Weight | Endpoint | Why |
| --- | --- | --- | --- |
| `health` | 1 | `GET /api/v1/health` | Baseline: isolates API overhead from intelligence work |
| `recommendations` | 5 | `GET /api/v1/recommendations` | The meaningful path (§17) |
| `recommendation_explanation` | 2 | `GET /api/v1/recommendations/{id}/explanation` | End-to-end operator journey; with no key configured this measures the degraded LLM path |

Read-only by design: the workload never calls the submission endpoint, because a
load generator must not drive allocations in the simulated world (§24).

### The command that will produce the figures

```bash
python -m locust -f backend/loadtest/locustfile.py --headless \
    --users 50 --spawn-rate 5 --run-time 2m \
    --host http://localhost:9000 \
    --csv backend/loadtest/results
```

### Measured results

| Metric | Baseline `GET /api/v1/health` | `GET /api/v1/recommendations` |
| --- | --- | --- |
| Requests | _to be measured_ | _to be measured_ |
| Average latency | _to be measured_ | _to be measured_ |
| p50 | _to be measured_ | _to be measured_ |
| p95 | _to be measured_ | _to be measured_ |
| p99 | _to be measured_ | _to be measured_ |
| Max | _to be measured_ | _to be measured_ |

| Run parameter | Value |
| --- | --- |
| Concurrency | _to be measured_ (planned: 50 simulated operators) |
| Duration | _to be measured_ (planned: 2m) |
| Throughput | _to be measured_ |
| Error rate | _to be measured_ |
| Host / hardware | _to be measured_ |

Brief §17 is explicit that the point is "understanding the behavior and limits of
the implemented system rather than achieving an arbitrary benchmark", so when
these are filled in they should be reported with the workload line that produced
them and with the observed limit — the concurrency at which error rate or p95
stops being acceptable — rather than as a single flattering number.

`backend/loadtest/README.md` explains how to read the results.

---

## 8. Documented assumptions (brief §24)

These are the assumptions the system is built on. They are stated so a judge can
disagree with one specifically rather than with the whole design.

**Domain and safety**

1. **Simulation only.** The platform operates exclusively against the BUP Fuel
   Supply Simulator. It never contacts real fuel infrastructure and holds no
   real credentials. There are no real purchases or dispatches to prevent,
   because none are possible: the only write endpoint is the simulator's own
   allocation API.
2. **The simulator is the source of truth for the world.** The backend observes
   and advises; it does not model its own competing network state. The
   frontend's in-browser world engine is a development stand-in for the
   simulator, not a second implementation of the domain.
3. **Recommendations are never executed automatically.** `allocate.py` returns
   recommendations and stops. Submission is a separate, explicit,
   operator-initiated call requiring a confirm field and an idempotency key
   (`CONTRACT.md` §9). This is brief §24's "preserve human review for
   consequential simulated decisions" made structural rather than advisory.
4. **All displayed figures are simulated and are labelled as such.** Any
   response carrying a prediction, recommendation or explanation includes
   `"simulated": true`.

**Data and integration**

5. **Field names and enumerations were verified against the live simulator**,
   not taken from the integration guide's prose. The guide documents 12 paths;
   its own `/openapi.json` exposes 27, and the extra ones (metrics, region and
   entity lookups) are what the intelligence layer actually needs
   (`CONTRACT.md` §5.1–5.2).
6. **The simulator may change under us.** An unrecognised enum value or an
   unfamiliar field is logged and tolerated rather than treated as fatal — the
   brief warns that organizers may introduce surprise events during judging.
7. **REST is authoritative; SSE is a hint.** Nothing is computed from an event
   payload alone. This is what makes a dropped or reordered event make a number
   late rather than wrong.
8. **Allocations are idempotent.** A replay returns 201 rather than the
   documented 200; the simulator de-duplicates correctly, so any 2xx is treated
   as success.

**Operational**

9. **Clock.** The simulator advances a tick per `SIMULATION_SPEED` wall-clock
   seconds (compose default: 8 ticks/s, 15 simulated minutes per tick), and
   starts **running**, overriding the guide's `paused` default so a fresh
   `docker compose up` is immediately useful. Forecasts and horizons are
   expressed in **ticks**, not wall-clock time; a horizon is therefore a
   simulated duration, not a real one.
10. **Single-node, single-writer.** The deployment is one backend container with
    SQLite on a named volume. There is no replication, no HA, and no
    multi-writer concurrency. A swap to PostgreSQL is a `DATABASE_URL` change,
    but it has not been tested.
11. **In-process resilience.** The circuit breaker and last-good cache protect
    one process's view of one dependency. They are not a distributed failover
    mechanism.
12. **Confidence values are model-derived, not calibrated probabilities.** A
    confidence of 0.7 means "this method, with this much history, at this
    horizon" — not a measured 70% frequency. `LOW_CONFIDENCE_THRESHOLD = 0.5` is
    a policy default chosen to be conservative, not a tuned optimum.
13. **Load-test figures are desktop measurements, not production benchmarks**,
    and will be reported with their workload (see §7).

**Scope**

14. **No reinforcement learning.** Brief §8 makes RL optional. The decision
    layer uses constrained optimisation with a transparent heuristic fallback,
    because those are inspectable: an operator can see which constraints bound a
    recommendation, and that matters more here than a marginal improvement in a
    simulated objective.
15. **The LLM explains; it never decides.** The model is given only structured
    facts the deterministic engines already produced, and is told explicitly
    that this is a simulation.

---

## 9. Divergences, gaps and limitations

Recorded honestly, because a judge will find them anyway and a documented
limitation is worth more than an undocumented one.

1. **The console does not currently route its reads through the backend
   gateway.** `CONTRACT.md` §1 states the backend is the single gateway and that
   "the frontend talks only to the backend". As built, `frontend/nginx/
   default.conf.template` proxies `/v1/*` directly to the simulator, and no
   backend URL appears anywhere in `frontend/src`. The consequence is
   architectural rather than cosmetic: the console renders the simulator's own
   data and therefore does not show the backend's forecasts, risk signals,
   recommendations or degradation state. The backend API is complete and
   reachable on `:9000`, but the deployed operator experience does not yet
   depend on it. This should be reconciled before judging, in one of two
   directions — either the console is repointed at `/api/v1/*`, or §1's
   single-gateway claim is corrected to describe the topology as built.
2. **`policy.py` is not yet imported by the API layer.** `CONTRACT.md` §11 says
   the policy is "imported by the API layer so the behaviour is declared in one
   place". The policy module and its tests exist now; the call sites in
   `app/api/` do not yet consult it. Until they do, the table in §4.1 is a
   declared policy that is enforced by the gateway and engines' own code paths,
   not by an explicit lookup at each call site. This is a wiring gap, not a
   missing design.
3. **Two metric names in `policy.py` are not declared in `CONTRACT.md` §10.**
   The policy names `fallback_activated_total` and `simulator_*` events for the
   paths §10 does not cover (it declares `llm_fallback_total`,
   `forecast_fallback_total` and `circuit_breaker_state`). Either §10 or the
   observability layer needs to adopt the generic counter, or the fallback
   activations outside the LLM and forecast paths will be visible in the logs
   but not in the metrics.
4. **The `forecast` component in §4.1 is an addition.** `CONTRACT.md` §9 pins
   the `/api/v1/status` components as `simulator`, `database`, `llm` and
   `decision_engine`; the policy also declares a `forecast` low-confidence path,
   because brief §11 treats low prediction confidence as its own case.
5. **The frontend CI job is new and unproven.** The backend job covers install →
   lint → test → package → verify. The frontend job runs the scripts the
   frontend documents (`npm ci`, `lint`, `typecheck`, `test`, `build`); if any of
   them is red on `main`, CI will be red, which is the intended behaviour of a
   gate but worth knowing before a demo.
6. **No Kubernetes, Terraform or GitOps.** Brief §13 lists these as optional
   "additional technical depth" and warns against introducing infrastructure
   complexity that does not improve the solution. A three-container compose
   stack is the deployment; the CI proves the image builds and is configured
   safely.

---

## 10. Deliverables map (brief §19)

| # | Deliverable | Where |
| --- | --- | --- |
| 1 | Working application | `docker compose up --build` → console on `:8080`, API on `:9000` |
| 2 | Source repository | `backend/`, `frontend/`, `docker-compose.yml`, `README.md` |
| 3 | Simulator integration | `backend/app/sim/` — typed client, envelope normalisation, retry, breaker, last-good cache |
| 4 | Intelligence component | `backend/app/intelligence/` — forecasting, anomaly detection, constrained allocation optimisation |
| 5 | Operator interface | `frontend/` — the React console |
| 6 | Architecture diagram | §1 of this document |
| 7 | Reproducible deployment | §2, and `docker-compose.yml` |
| 8 | Observability evidence | §5 — metrics endpoint, structured logs, `/api/v1/status` |
| 9 | Resilience demonstration | §4 — the declared policy table and the fault-injection walkthrough |
| 10 | Load-test evidence | §7 — **placeholder, to be measured** |
| 11 | Final demo | brief §22's story maps onto §4.2 (steps 11–14) |
