# Backend Contract — Fuel Supply Intelligence & Resilience Platform

**This file is the authority for every backend module.** Ten workstreams are
being built in parallel against it. If your module needs something from another
module, it must be in this file. If it is not here, do not invent it — implement
against the signature below and report the gap.

Source documents: `Fuel_Supply_Intelligence_Resilience_Platform.md` (the brief)
and `BUP_Fuel_Supply_Simulator_Integration_Guide_Final.md` (the simulator).

---

## 0. Non-negotiable constraints

These come from brief §24 (Constraints and Guardrails) and §18 (Security and
Engineering Hygiene). They are not style preferences.

1. **Operate only against the simulation environment.** Never contact real fuel
   infrastructure. No real purchases or dispatches, ever.
2. **Never hard-code a secret.** The DeepSeek key comes from the environment only.
   It must never appear in source, in a log line, in a metric label, in an API
   response, or in a committed file.
3. **Validate external input.** Every request body and query param goes through
   Pydantic. The simulator's responses are also external input — validate before
   trusting.
4. **Handle failed requests appropriately.** No bare `except: pass`. Every failure
   path yields a typed error, a log line, and a metric.
5. **Document required configuration.** Every environment variable goes in
   `.env.example` with a comment.
6. **Distinguish simulated results from real-world conditions.** Every API
   response that carries a recommendation or prediction includes
   `"simulated": true`. Do not let a judge mistake this for a real tool.
7. **Preserve human review for consequential decisions.** The backend
   **recommends**; it never auto-executes an allocation against the simulator.
   Submission is a separate, explicit, operator-initiated call.

---

## 1. Architecture

```
                    ┌──────────────────────────────────────────┐
                    │  BUP Fuel Supply Simulator (published)    │
                    │  REST /v1/*  +  SSE /v1/stream            │
                    └────────────────┬─────────────────────────┘
                                     │  httpx (async), no CORS needed
                    ┌────────────────▼─────────────────────────┐
                    │  Simulator Gateway  (app/sim/)            │
                    │  typed client · retry · circuit breaker   │
                    │  last-good cache · envelope normalisation │
                    └────────────────┬─────────────────────────┘
                                     │
        ┌───────────────┬────────────┴───────────┬──────────────────┐
        ▼               ▼                        ▼                  ▼
  ┌───────────┐  ┌─────────────┐        ┌──────────────┐   ┌──────────────┐
  │ Snapshot  │  │ Forecasting │        │  Detection   │   │  Decision    │
  │  Store    │  │ (app/       │        │  (app/       │   │  Engine      │
  │ (app/     │  │  intel/     │        │   intel/     │   │  (app/intel/ │
  │  store/)  │  │  forecast)  │        │   detect)    │   │   allocate)  │
  └─────┬─────┘  └──────┬──────┘        └──────┬───────┘   └──────┬───────┘
        │               │                      │                  │
        │               └──────────┬───────────┘                  │
        │                          ▼                              │
        │                 ┌─────────────────┐                     │
        │                 │  Explanation    │◄────────────────────┘
        │                 │  (DeepSeek LLM) │
        │                 │  app/llm/       │
        │                 └────────┬────────┘
        │                          │  falls back to deterministic
        │                          │  templates when unavailable
        └──────────────┬───────────┴──────────────┐
                       ▼                          ▼
              ┌─────────────────┐        ┌──────────────────┐
              │  REST API       │        │  Observability   │
              │  app/api/       │        │  app/observ/     │
              │  port 9000      │        │  logs·metrics    │
              └─────────────────┘        └──────────────────┘
```

The backend is the **single gateway**: the frontend talks only to the backend,
and the backend talks to the simulator. This is what makes the whole platform
one origin and removes any CORS question (the simulator sends no CORS headers
and 405s the preflight — verified against the live instance).

---

## 2. Stack — pinned

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12 | Matches the simulator; best numeric/ML ecosystem |
| Web | FastAPI + uvicorn | Async, Pydantic-native, OpenAPI for free |
| HTTP | httpx (async) | Same client for REST and SSE |
| Validation | Pydantic v2 + pydantic-settings | Brief §18 requires validated input |
| Storage | SQLAlchemy 2.0 + SQLite (`aiosqlite`) | Zero infra, works in compose; swappable to Postgres by URL |
| Numerics | numpy, scipy | `scipy.optimize.linprog` for the allocation LP |
| Metrics | prometheus-client | Brief §14 observability |
| Logs | structlog (JSON) | Brief §14 logs |
| Retry | tenacity | Brief §11 retries |
| Tests | pytest, pytest-asyncio, httpx.ASGITransport | |
| Load | locust | Brief §17 |

Do not add a dependency outside this table without reporting it.

---

## 3. File tree and ownership

Each workstream owns its files exclusively. **Do not edit a file you do not own.**

```
backend/
  CONTRACT.md                    [given]
  requirements.txt               A1
  .env.example                   A1
  Dockerfile                     A10
  pyproject.toml / pytest.ini    A1
  app/
    __init__.py                  A1
    config.py                    A1   Settings (pydantic-settings)
    deps.py                      A1   FastAPI DI providers
    main.py                      A1   app factory, lifespan, router mount
    sim/
      __init__.py                A2
      models.py                  A2   typed simulator domain models + Snapshot + DemandPoint
      errors.py                  A2   SimulatorError + envelope normalisation
      client.py                  A2   SimulatorClient
      circuit.py                 A2   CircuitBreaker
    store/
      __init__.py                A3
      models.py                  A3   SQLAlchemy tables
      db.py                      A3   engine/session lifecycle
      repository.py              A3   Repository
    intelligence/
      __init__.py                A4
      forecast.py                A4   DemandForecaster
      detect.py                  A5   AnomalyDetector
      allocate.py                A6   AllocationEngine
    llm/
      __init__.py                A7
      deepseek.py                A7   DeepSeekClient
      prompts.py                 A7   prompt templates
      explain.py                 A7   ExplanationService
    api/
      __init__.py                A8
      schemas.py                 A8   request/response models
      router.py                  A8   api_router
      health.py                  A8   /api/v1/health, /api/v1/status
      network.py                 A8   /api/v1/network/*
      intelligence.py            A8   /api/v1/forecast, /risk, /recommendations
      decisions.py               A8   /api/v1/decisions/*
      events.py                  A8   /api/v1/events
      admin.py                   A8   /api/v1/admin/* (simulator control)
    observability/
      __init__.py                A9
      logging.py                 A9   structured logging setup
      metrics.py                 A9   Prometheus registry + counters
      middleware.py              A9   request metrics/log middleware
      setup.py                   A9   install_observability(app)
    resilience/
      __init__.py                A10
      policy.py                  A10 FallbackPolicy, degraded-mode helpers
  tests/                         each owner adds their own test_*.py
  loadtest/locustfile.py         A10
.github/workflows/ci.yml         A10
docs/architecture.md             A10
```

---

## 4. Configuration — `app/config.py` (A1)

Every variable is documented in `.env.example`. Settings are read from the
environment with pydantic-settings; `backend/.env` is git-ignored.

| Variable | Default | Meaning |
|---|---|---|
| `SIMULATOR_BASE_URL` | `http://simulator-api:8000` | Simulator root |
| `SIMULATOR_TIMEOUT_SECONDS` | `10` | Per-request timeout |
| `SIMULATOR_MAX_RETRIES` | `3` | Tenacity retry attempts |
| `SIMULATOR_CACHE_TTL_SECONDS` | `1` | Reuse successful reads and coalesce concurrent requests |
| `CIRCUIT_FAILURE_THRESHOLD` | `5` | Failures before the breaker opens |
| `CIRCUIT_RESET_SECONDS` | `30` | Open→half-open delay |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/fuel.db` | Storage |
| `DEEPSEEK_API_KEY` | *(empty)* | **Secret. Never logged, never returned.** |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI-compatible endpoint |
| `DEEPSEEK_MODEL` | `deepseek-chat` | Model id |
| `DEEPSEEK_TIMEOUT_SECONDS` | `30` | LLM timeout |
| `LLM_ENABLED` | `true` | Set false to force the deterministic fallback path |
| `API_PORT` | `9000` | |
| `LOG_LEVEL` | `INFO` | |
| `CORS_ALLOW_ORIGINS` | `http://localhost:8080,http://localhost:5173` | Backend may allow the console |

```python
# app/config.py
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    simulator_base_url: str = "http://simulator-api:8000"
    # ... one field per row above
    @property
    def llm_available(self) -> bool: ...

@lru_cache
def get_settings() -> Settings: ...
```

**`DEEPSEEK_API_KEY` has no default that is a real key.** When empty,
`llm_available` is False and every LLM-dependent path takes its deterministic
fallback — the brief's §11 requirement ("ML model unavailable → fallback") maps
directly onto this, and it is a demonstrable resilience behaviour, not an error.

---

## 5. Simulator gateway — `app/sim/` (A2)

### 5.1 Verified contract

The live simulator was audited via its own `/openapi.json`. **These 27 paths are
the real surface** (the guide documents only 12):

```
GET  /                                     GET  /v1/allocations
GET  /admin                                POST /v1/allocations
GET  /admin/audit                          POST /v1/allocations/{allocation_id}/cancel
GET  /admin/events                         GET  /v1/demand-history
POST /admin/events                         GET  /v1/depots
GET  /admin/faults                         GET  /v1/depots/{entity_id}
POST /admin/faults                         GET  /v1/events
POST /admin/faults/clear                   GET  /v1/health
POST /admin/pause                          GET  /v1/instance
POST /admin/reset                          GET  /v1/metrics
POST /admin/run                            GET  /v1/regions
GET  /admin/stats                          GET  /v1/routes
POST /admin/step                           GET  /v1/stations
POST /admin/toggle                         GET  /v1/stations/{entity_id}
                                           GET  /v1/stream   (SSE)
                                           GET  /v1/supply-arrivals
```

### 5.2 Field names are verified, not guessed

#### The two shared composite types

`Snapshot` and `DemandPoint` are consumed by the forecasting, detection, decision
and API layers, so they are pinned here and owned by **A2 in `app/sim/models.py`**.

```python
@dataclass(frozen=True)
class DemandPoint:
    station_id: str
    fuel_type: str          # "DIESEL" | "PETROL" | "OCTANE"
    tick: int
    liters: float

@dataclass(frozen=True)
class Snapshot:
    taken_at: float                       # time.monotonic() when assembled
    tick: int
    sim_time: str
    status: str                           # instance status
    depots: tuple[Depot, ...]
    stations: tuple[Station, ...]
    routes: tuple[Route, ...]
    regions: tuple[Region, ...]
    supply_arrivals: tuple[SupplyArrival, ...]
    events: tuple[DomainEvent, ...]
    metrics: Metrics
    stale: bool = False                   # True when served from last-good cache
    age_seconds: float = 0.0              # age of the underlying data
```

#### Simulator field names

**Event `type` enum — read from the live `GET /openapi.json`
(`components.schemas.EventCreate.properties.type.enum`), which is authoritative.
The integration guide's §7.8 table is garbled by PDF text extraction and names
`route_disruption` where a careless reader might guess `road_closure`:**

```
demand_spike   shipment_delay   route_disruption
station_outage depot_constraint supply_shortfall
```

Exactly six values. `POST /v1/events` rejects anything else, so a fixture using
an invented name (`road_closure`, `supply_disruption`) tests nothing about the
real contract and would fail against the live simulator.

Two more observed shapes:

- `GET /v1/events` returns a **bare JSON array** (`[{"id":1,"type":"demand_spike",
  "start_tick":500,"end_tick":520,"status":"RESOLVED","parameters":{...}}]`),
  not an object envelope.
- Event `status` is UPPERCASE (`ACTIVE`, `RESOLVED`). Filter matching is
  case-insensitive, so `?status=active` and `?status=ACTIVE` both work.

These were observed from live responses. Use them exactly.

```python
Health      { status: str, database: str, simulation: { status: str, tick: int } }
SimInstance { id: int, scenario_id: str, scenario_version: str, seed: int,
              sim_time: str, tick: int, tick_minutes: int, status: str }
Metrics     { served_demand_liters: float, unmet_demand_liters: float,
              service_level: float, allocation_liters: float,
              allocation_failures: int }
Region      { id: str, name: str, demand_factor: float }
Depot       { id, name, region_id, status, dispatch_capacity_per_tick,
              capacity: dict[str, float], inventory: dict[str, float] }
Station     { id, name, region_id, status, demand_profile, demand_multiplier,
              capacity: dict[str, float], inventory: dict[str, float] }
Route       { id, source_depot_id, destination_station_id, transit_ticks,
              max_shipment: float, status }
SupplyArrival { id, depot_id, fuel_type, quantity, planned_tick,
                actual_tick: int | None, status }
DomainEvent { id: int, type, start_tick, end_tick, status,
              parameters: dict[str, Any] }
Allocation  { id: int, idempotency_key, source_depot_id, destination_station_id,
              route_id, fuel_type, quantity, created_tick,
              departure_tick: int|None, expected_arrival_tick: int|None,
              actual_arrival_tick: int|None, status, failure_reason: str|None }
AuditEntry  { id, wall_time, sim_time, tick, action, entity_type, entity_id,
              result, metadata_json }
```

**Enums observed live:**
- `fuel_type`: `DIESEL` | `PETROL` | `OCTANE`
- depot/station `status`: `OPEN` | `CONSTRAINED` | `OUTAGE`
- route `status`: `AVAILABLE` | `DISRUPTED`
- supply `status`: `SCHEDULED` | `DELAYED` | `ARRIVED`
- allocation `status`: `PENDING` | `IN_TRANSIT` | `ARRIVED` | `FAILED` | `CANCELLED`
- event `status`: `SCHEDULED` | `ACTIVE` | `RESOLVED`
- instance `status`: `RUNNING` | `PAUSED`
- `demand_profile`: `urban_high` | `industrial` | `highway` | `regional`

Model these as `str` enums but **do not hard-fail on an unknown value** — a
surprise value from the simulator (brief §10 says organizers may introduce
changes mid-event) must be logged and tolerated, never crash a request.

### 5.3 Error normalisation

The simulator has three distinct error envelopes. All three normalise to one
`SimulatorError`:

```python
{"detail": {"code": "...", "message": "..."}}   # domain
{"error":  {"code": "...", "message": "..."}}   # injected fault
{"detail": [{"loc": [...], "msg": "..."}]}      # FastAPI 422
```

**A known trap, verified against the live API:** a helper testing
`isinstance(v, dict)` alone is *wrong* — a JSON array is not a dict in Python, but
the equivalent check in TypeScript (`typeof x === 'object'`) accepts arrays and
silently routes the 422 envelope down the wrong branch. In Python use
`isinstance(body, dict)` **and** check `dict` before `list`, and test the array
case explicitly.

**Also verified:** `POST /v1/allocations` returns **201 on an idempotency replay**,
not the 200 the guide documents. The simulator de-duplicates correctly (only one
allocation is created); only the status code differs. Accept any 2xx as success.

### 5.4 `SimulatorClient`

```python
class SimulatorClient:
    def __init__(self, settings: Settings, metrics: Metrics) -> None: ...

    async def start(self) -> None: ...      # starts the SSE consumer task
    async def aclose(self) -> None: ...

    # reads — every one raises SimulatorError, never returns None
    async def get_health(self) -> Health
    async def get_instance(self) -> SimInstance
    async def get_regions(self) -> list[Region]
    async def get_depots(self) -> list[Depot]
    async def get_stations(self) -> list[Station]
    async def get_routes(self) -> list[Route]
    async def get_supply_arrivals(self) -> list[SupplyArrival]
    async def get_events(self) -> list[DomainEvent]
    async def get_allocations(self) -> list[Allocation]
    async def get_demand_history(self, station_id: str | None = None,
                                 limit: int = 500) -> list[DemandObservation]
    async def get_metrics(self) -> Metrics

    # writes — operator-initiated only
    async def create_allocation(self, req: AllocationRequest) -> Allocation
    async def cancel_allocation(self, allocation_id: int) -> Allocation

    # admin / scenario control
    async def admin_run(self) -> SimInstance
    async def admin_pause(self) -> SimInstance
    async def admin_step(self) -> dict[str, Any]
    async def admin_reset(self) -> dict[str, Any]
    async def admin_inject_event(self, req: EventRequest) -> DomainEvent
    async def admin_inject_fault(self, req: FaultRequest) -> Fault
    async def admin_clear_faults(self) -> dict[str, Any]
    async def admin_get_faults(self) -> list[Fault]

    # streaming
    def stream(self) -> AsyncIterator[StreamEvent]
    def subscribe(self, cb: Callable[[StreamEvent], Awaitable[None]]) -> Callable[[], None]

    # resilience surface, consumed by /api/v1/status
    @property
    def breaker_state(self) -> Literal["closed", "open", "half_open"]: ...
    def last_good(self, key: str) -> tuple[Any, float] | None: ...  # (value, monotonic_ts)
```

`StreamEvent = { name: str, data: dict, received_at: float }` where `name` is one
of `simulation.tick` | `allocation.status_changed` | `inventory.updated` |
`simulator.notice`.

### 5.5 Resilience requirements (brief §11)

- **Timeout** on every request (`SIMULATOR_TIMEOUT_SECONDS`).
- **Retry with backoff** on 5xx and transport errors only — never on 4xx.
- **Circuit breaker**: after `CIRCUIT_FAILURE_THRESHOLD` consecutive failures,
  open for `CIRCUIT_RESET_SECONDS`, then half-open with a single trial.
- **Last-good cache**: every successful read stores `(value, monotonic_ts)`.
  When the breaker is open, reads return the cached value with
  `stale=True, age_seconds=...` rather than failing. If there is no cached value,
  raise `SimulatorError` — degrade, do not fabricate.
- **SSE reconnect**: on stream error, reconnect with exponential backoff. Guide
  §6.1: a subscriber more than 200 events behind is dropped — reconnect and
  re-fetch, never assume the stream is complete.

**REST is the source of truth. An SSE event is only a hint that something
changed** — every event triggers a re-fetch, and nothing is computed from an event
payload alone. A dropped or reordered event must therefore be able to make a
number late, never wrong.

---

## 6. Storage — `app/store/` (A3)

SQLAlchemy 2.0 declarative, async engine, SQLite by default.

```python
class Repository:
    async def init(self) -> None: ...          # create_all
    async def record_snapshot(self, snap: Snapshot) -> None
    async def latest_snapshot(self) -> Snapshot | None
    async def record_demand_point(self, station_id: str, fuel_type: str,
                                  tick: int, liters: float) -> None
    async def demand_series(self, station_id: str, fuel_type: str,
                            limit: int = 500) -> list[DemandPoint]
    async def record_decision(self, rec: RecommendationRecord) -> int
    async def record_decision_outcome(self, decision_id: int, *,
                                      submitted: bool, allocation_id: int | None,
                                      note: str | None) -> None
    async def list_decisions(self, limit: int = 50,
                             offset: int = 0) -> list[RecommendationRecord]
    async def get_decision(self, decision_id: int) -> RecommendationRecord | None
    async def record_alert(self, alert: AlertRecord) -> int
    async def list_alerts(self, limit: int = 50, active_only: bool = False) -> list[AlertRecord]
    async def record_llm_call(self, *, purpose: str, model: str,
                              ok: bool, latency_ms: int,
                              fallback_used: bool) -> None
    async def llm_stats(self) -> LLMStats
```

Tables: `snapshots`, `demand_points`, `decisions`, `alerts`, `llm_calls`.
`decisions` is the **decision audit history** (brief §20 recommended deliverable).

---

## 7. Intelligence — `app/intelligence/`

Each engine is a **pure function of its inputs**. No engine may call the
simulator, the database, or the network. This is what makes them testable and
what lets the API layer compose them.

### 7.1 Forecasting (A4) — `forecast.py`

```python
@dataclass(frozen=True)
class ForecastPoint:
    tick: int
    liters: float
    lower: float          # 80% interval
    upper: float

@dataclass(frozen=True)
class ForecastResult:
    station_id: str
    fuel_type: str
    points: tuple[ForecastPoint, ...]
    confidence: float                  # 0..1
    method: str                        # "seasonal_naive" | "holt_winters" | "ewma"
    fallback_used: bool

class DemandForecaster:
    def __init__(self, *, min_points: int = 12) -> None: ...
    def forecast(self, history: Sequence[DemandPoint], *,
                 horizon_ticks: int = 8) -> ForecastResult: ...
    def stockout_risk(self, *, inventory_liters: float,
                      forecast: ForecastResult,
                      transit_ticks: int | None = None) -> StockoutRisk: ...

@dataclass(frozen=True)
class StockoutRisk:
    probability: float                 # 0..1
    ticks_to_stockout: float | None
    confidence: float
    basis: str                         # human-readable, shown to the operator
```

Requirements:
- Must work with **few or zero** historical points. With `< min_points`, fall
  back to a level-based estimate from current inventory + demand, set
  `method="fallback"`, `fallback_used=True`, and lower `confidence`. Never raise,
  never return NaN.
- `confidence` must fall as the horizon grows and as history shrinks. A constant
  confidence is a bug.
- Deterministic for identical input — no hidden randomness.

### 7.2 Detection (A5) — `detect.py`

```python
class Severity(str, Enum):
    INFO = "info"; WARNING = "warning"; SERIOUS = "serious"; CRITICAL = "critical"

@dataclass(frozen=True)
class RiskSignal:
    kind: str                  # "demand_anomaly" | "inventory_drop" |
                               # "route_bottleneck" | "regional_disruption" |
                               # "supply_shortfall"
    severity: Severity
    entity_type: str           # "station" | "depot" | "route" | "region"
    entity_id: str
    detected_at_tick: int
    summary: str               # one line, operator-facing
    evidence: dict[str, Any]   # the numbers behind it — brief §9 "which signals"
    confidence: float

class AnomalyDetector:
    def detect(self, *, snapshot: Snapshot,
               history: Mapping[tuple[str, str], Sequence[DemandPoint]]) -> list[RiskSignal]: ...
```

- Use a robust z-score (median + MAD), not mean + stdev — one spike must not
  hide every later one.
- Severity must be **derived from the numbers**, not hard-coded per rule.
- Return signals sorted most severe first, then most confident.
- Empty input → empty list, not an exception.

### 7.3 Decision engine (A6) — `allocate.py`

```python
@dataclass(frozen=True)
class Recommendation:
    id: str                             # stable within a tick
    station_id: str
    depot_id: str
    route_id: str
    fuel_type: str
    quantity_liters: float
    rationale: str
    constraints: tuple[str, ...]        # which limits bound this choice
    expected_impact: dict[str, float]   # e.g. {"risk_before": .., "risk_after": ..}
    alternatives: tuple[Alternative, ...]
    confidence: float
    simulated: bool = True              # brief §24 — always True

class AllocationEngine:
    def __init__(self, *, policy: str = "optimizer") -> None: ...
    def recommend(self, *, snapshot: Snapshot,
                  signals: Sequence[RiskSignal],
                  forecasts: Mapping[tuple[str, str], ForecastResult],
                  budget_liters: float | None = None) -> list[Recommendation]: ...
```

Requirements — these are the brief's §7 Decision Intelligence and §9:
- **Primary path**: constrained optimisation (`scipy.optimize.linprog`) maximising
  expected unmet-demand reduction, subject to depot inventory, route
  `max_shipment`, route availability, and depot dispatch capacity.
- **Fallback path**: a transparent priority heuristic (severity × probability ×
  volume), used when the optimizer is unavailable, infeasible, or errors.
  `policy` reports which ran. This is the §11 "ML model unavailable → fallback
  allocation policy" requirement — it must be reachable and tested.
- Every recommendation carries `rationale`, `constraints`, `expected_impact`,
  `alternatives`, and `confidence` — the brief requires recommendations to be
  **inspectable**, showing why, which signals, which constraints, and what else
  was considered.
- **Never execute.** The engine returns recommendations. Submission is a separate
  operator action (brief §24, "preserve human review").
- No recommendation may exceed `max_shipment`, exceed depot inventory, or use a
  `DISRUPTED` route. Test these explicitly.

### 7.4 Stockout & shortage intelligence (A4b) — `stockout.py`

```python
class RiskLevel(str, Enum):
    LOW = "LOW"; MEDIUM = "MEDIUM"; HIGH = "HIGH"; CRITICAL = "CRITICAL"

LEVEL_ORDER: tuple[RiskLevel, ...] = (LOW, MEDIUM, HIGH, CRITICAL)   # ascending severity

@dataclass(frozen=True)
class RiskThresholds:
    critical_ticks: float = 1.0
    high_ticks: float = 3.0
    safety_stock_fraction: float = 0.25
    horizon_ticks: int = 8

@dataclass(frozen=True)
class IncomingSupply:
    quantity: float
    arrival_tick: int
    allocation_id: str = ""
    source_depot_id: str = ""

@dataclass(frozen=True)
class StockoutAssessment:
    station_id: str
    fuel_type: str
    current_inventory: float
    expected_demand: float
    incoming_supply: float
    projected_inventory: float
    shortage_amount: float
    surplus_amount: float
    stockout_tick: int | None
    ticks_until_stockout: float | None
    risk: RiskLevel
    basis: str                       # one operator-facing sentence
    horizon_ticks: int
    demand_method: str
    demand_confidence: float
    incoming_sources: tuple[IncomingSupply, ...] = ()
    simulated: bool = True

class StockoutAnalyzer:
    def __init__(self, *, thresholds: RiskThresholds | None = None) -> None: ...
    @property
    def thresholds(self) -> RiskThresholds: ...
    def assess(self, *, station_id: str, fuel_type: str,
               current_inventory: float, forecast: Any,
               incoming: Sequence[IncomingSupply] = (),
               current_tick: int = 0) -> StockoutAssessment: ...
    def assess_many(self, *, rows: Iterable[Mapping[str, Any]],
                    current_tick: int = 0) -> list[StockoutAssessment]: ...
```

Requirements:
- Same **purity contract** as 7.1–7.3: no simulator, database or network access, no
  clock, no randomness, deterministic. Never raises, never returns NaN; identical
  arguments always produce an equal result.
- The projection is a **tick-by-tick walk**, not a closed-form subtraction:
  `inventory_t = inventory_{t-1} + inflow_t - demand_t` for `t = 1..horizon`, where
  `inflow_t` is the supply landing on tick `t` and `demand_t = forecast.points[t-1].liters`.
  Summing the recurrence telescopes to
  `projected_inventory == current_inventory + incoming_supply - expected_demand` — the
  formula an operator can check by hand. The walk exists only to produce the
  **timing**: the closed form cannot say *when* the crossing happens, and "when" is
  the number an operator acts on.
- `expected_demand` is the sum of the forecast's points over the horizon and comes
  from `DemandForecaster`; this module **never re-derives it**. Two estimators of one
  quantity would be two answers to one question.
- Risk is banded by an **ordered, first-match-wins** rule. The checks run most severe
  first, so the bands are nested and a projected stockout is never LOW:
  - `CRITICAL` — `current_inventory <= 0`, **or** `ticks_until_stockout <= critical_ticks`
  - `HIGH` — `ticks_until_stockout <= high_ticks`
  - `MEDIUM` — `shortage_amount > 0`, **or** `projected_inventory < safety_stock`
  - `LOW` — otherwise, where `safety_stock = safety_stock_fraction x expected_demand`
- Only **in-transit allocations** count as a station's incoming supply (status
  `PENDING` or `IN_TRANSIT`). Depot-directed `SupplyArrival` rows are **deliberately
  excluded**: a delivery to a depot is not inbound to a station until somebody
  dispatches it, and counting it would report a station as covered while nothing is
  moving.
- An arrival **beyond the horizon is excluded**, never folded into the end state: it
  cannot prevent a stockout that happens before it lands, and counting it would
  report a dry station as covered.
- Configuration is read from `Settings` (`app/config.py`) and passed in as
  `RiskThresholds`: `STOCKOUT_HORIZON_TICKS` (8), `STOCKOUT_CRITICAL_TICKS` (1.0),
  `STOCKOUT_HIGH_TICKS` (3.0), `STOCKOUT_SAFETY_STOCK_FRACTION` (0.25).

---

## 8. LLM / DeepSeek — `app/llm/` (A7)

The brief (§7 Generative AI) asks for incident explanation, supply-chain state
summarisation, operator investigation assistance, and human-readable decision
explanations — and says LLMs should **support the operational system rather than
merely provide a chatbot**. So the LLM is used to explain decisions the
deterministic engines already made, never to make them.

### 8.1 `DeepSeekClient`

DeepSeek exposes an **OpenAI-compatible** API, so use plain `httpx` against
`{DEEPSEEK_BASE_URL}/chat/completions` with a bearer token. Do not depend on the
`openai` SDK.

```python
class DeepSeekClient:
    def __init__(self, settings: Settings, metrics: Metrics) -> None: ...
    @property
    def available(self) -> bool: ...      # False when key empty or LLM_ENABLED false
    async def complete(self, *, system: str, user: str,
                       max_tokens: int = 700, temperature: float = 0.2) -> str: ...
```

Rules:
- **The API key is read from settings and used only in the Authorization header.**
  It must never be logged, never echoed in an error message, never returned by an
  endpoint, never put in a metric label. When reporting an error, redact it.
- Timeout, retry once, then raise `LLMUnavailable`. Never let an LLM failure
  propagate as a 500 to an operator.
- `temperature` low — this is operational explanation, not creative writing.
- Record every call to `Repository.record_llm_call`.

### 8.2 `ExplanationService`

```python
class ExplanationService:
    def __init__(self, client: DeepSeekClient, repo: Repository,
                 metrics: Metrics) -> None: ...

    async def explain_recommendation(self, rec: Recommendation,
                                     signals: Sequence[RiskSignal]) -> Explanation
    async def summarize_network(self, snapshot: Snapshot,
                                signals: Sequence[RiskSignal]) -> Explanation
    async def investigate(self, question: str, *,
                          context: dict[str, Any]) -> Explanation

@dataclass(frozen=True)
class Explanation:
    text: str
    source: Literal["llm", "fallback"]
    model: str | None
    degraded: bool          # True when the deterministic fallback produced this
    simulated: bool = True
```

**The fallback is mandatory and is the interesting part.** When the key is
missing, `LLM_ENABLED=false`, the call times out, or the response fails
validation, the service returns a **deterministic, templated explanation built
from the same structured facts** — with `source="fallback"`, `degraded=True`. The
operator always gets an explanation; the response always says where it came from.
This is brief §11 ("ML model unavailable → fallback") and it must have a test that
runs with no key configured.

Prompt rules:
- Prompts live in `prompts.py`, never inline in logic.
- The model is given **only structured facts** the engines produced. It is told
  explicitly that this is a simulation and must not assert real-world fuel
  conditions (brief §24).
- Never send the API key, credentials, or raw environment to the model.
- Validate the response: non-empty, within a length bound. A response that fails
  validation is treated as an LLM failure and falls back.

---

## 9. API — `app/api/` (A8)

All routes under `/api/v1`. Every response carries `simulated: true` where it
contains a prediction, recommendation, or explanation.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/health` | Liveness. Cheap, no dependencies. |
| GET | `/api/v1/status` | Component health + p95 latency + error rate (brief §15) |
| GET | `/api/v1/network/snapshot` | Instance + depots + stations + routes + regions |
| GET | `/api/v1/network/demand-history` | Proxied, validated |
| GET | `/api/v1/forecast` | Forecast + stockout risk per station/fuel |
| GET | `/api/v1/stockout` | Stockout + shortage projection per station/fuel, with risk bands |
| GET | `/api/v1/risk` | Current `RiskSignal`s (brief §6 "shortage alerts") |
| GET | `/api/v1/recommendations` | Ranked, inspectable recommendations |
| GET | `/api/v1/recommendations/{id}/explanation` | LLM or fallback explanation |
| POST | `/api/v1/recommendations/{id}/submit` | **Operator-initiated** allocation submission |
| GET | `/api/v1/decisions` | Decision audit history |
| GET | `/api/v1/decisions/{id}` | One decision + outcome |
| GET | `/api/v1/events` | Crisis events |
| POST | `/api/v1/events/summary` | Network state summary (LLM/fallback) |
| POST | `/api/v1/investigate` | Operator investigation (LLM/fallback) |
| GET | `/api/v1/admin/faults` | Pass-through, for demos |
| POST | `/api/v1/admin/faults` | Inject a fault (demo of resilience) |
| POST | `/api/v1/admin/simulation/{action}` | run/pause/step/reset |
| GET | `/metrics` | Prometheus, no `/api/v1` prefix |

Requirements:
- **`/api/v1/recommendations/{id}/submit` requires an explicit confirm field**
  (e.g. `{"confirm": true, "idempotency_key": "..."}`). Without it, 400. This is
  the brief's "preserve human review for consequential simulated decisions" made
  structural rather than advisory.
- `/api/v1/status` must report each component separately — `simulator`,
  `database`, `llm`, `decision_engine` — mirroring the brief §15 example, plus
  p95 latency and error rate from the metrics middleware.
- `llm` status must be `"degraded"` (not `"down"`) when the key is absent: that is
  a supported mode, not a failure.
- Unknown recommendation id → 404 with a typed error body, not a 500.
- Every route validates input via `schemas.py`.

---

## 10. Observability — `app/observability/` (A9)

Brief §14 requires four layers. All four must be present.

**Application**: `http_requests_total{method,path,status}`, `http_request_duration_seconds`
(histogram), plus p95 computed from it.
**System**: `process_cpu_seconds_total`, `process_resident_memory_bytes` (use
`prometheus_client`'s ProcessCollector).
**Intelligence** (this is the one teams skip — do not skip it):
`forecast_confidence` (histogram), `forecast_fallback_total`,
`risk_signals_total{kind,severity}`, `recommendations_generated_total`,
`recommendation_confidence` (histogram), `llm_calls_total{purpose,result}`,
`llm_latency_seconds` (histogram), `llm_fallback_total{reason}`,
`circuit_breaker_state{component}`.
**Logs**: structlog JSON, one line per request, one per decision, one per
fallback activation, one per recovery.

```python
def install_observability(app: FastAPI) -> None: ...
class Metrics:  # injected via deps
    def observe_request(self, method, path, status, duration) -> None
    def record_forecast(self, confidence: float, fallback: bool) -> None
    def record_signal(self, kind: str, severity: str) -> None
    def record_recommendation(self, confidence: float) -> None
    def record_llm_call(self, purpose: str, ok: bool, latency: float,
                        fallback_reason: str | None) -> None
    def set_breaker(self, component: str, state: str) -> None
```

**Never put a secret, an API key, a full request body, or a station's raw payload
into a log line or a metric label.** Metric labels must be bounded — never label
by `station_id` (unbounded cardinality); aggregate or bucket instead.

---

## 11. Resilience and delivery — `app/resilience/`, `loadtest/`, CI (A10)

- `policy.py`: `FallbackPolicy` describing each degradation path and its trigger,
  imported by the API layer so the behaviour is declared in one place:
  LLM unavailable → templated explanation; simulator open-circuit → last-good
  cached snapshot marked stale; optimizer infeasible → priority heuristic;
  low confidence → flag for human review; database unavailable → serve from
  memory and mark degraded.
- `backend/Dockerfile`: multi-stage, non-root user, `HEALTHCHECK` against
  `/api/v1/health`.
- `loadtest/locustfile.py`: at least one meaningful path (brief §17) — the
  recommendations endpoint is the right one. Must print p50/p95/p99, throughput,
  and error rate, and the numbers must be recorded in `docs/architecture.md`.
- `.github/workflows/ci.yml`: install → lint → pytest → build image.
- `docs/architecture.md`: the §19 deliverable #6 diagram (simulator → data/backend
  → intelligence → decision → application → monitoring), the assumptions from
  §24, and the load-test results.

---

## 12. Definition of done

A module is done when:
1. Every public signature in this contract exists with the stated name and shape.
2. Its own tests pass, and the whole suite passes.
3. Failure paths are tested, not just happy paths — for the engines that means:
   empty input, insufficient history, invalid simulator response, breaker open,
   LLM key absent.
4. No secret is logged, returned, or committed.
5. It does not import a module it does not own at an interface not in this file.

**Report honestly.** A module that does not work, reported as working, is worse
than one reported broken. State what passed, what failed, and what you left out.
