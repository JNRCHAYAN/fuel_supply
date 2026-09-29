# Stockout & Shortage Intelligence — Design

Date: 2026-09-29
Status: approved for implementation

## Purpose

Extend the existing intelligence layer with a per-(station, fuel) projection of
future inventory, so an operator can see not only *that* a station will run dry
but *when*, and by how much. This is additive: no existing engine, route or
response is changed.

## Scope decisions

| Decision | Choice |
|---|---|
| Incoming supply | In-transit allocations (`destination_station_id` = station, status `PENDING`/`IN_TRANSIT`), arriving within the horizon. Depot-directed `supply_arrivals` are **not** attributed to stations — that would be a modelled assumption, not an observed fact. |
| Exposure | New route `GET /api/v1/stockout`. `/api/v1/forecast` is untouched. |
| Risk rule | Time-to-stockout bands plus a safety-stock floor, all thresholds configurable via `Settings`. |
| Console | A panel on the existing dashboard. No router is mounted today and none is added. |

## Arithmetic

The stated formula is the horizon endpoint; a tick-by-tick walk produces the
timing. The two reconcile exactly, which is what makes the result explainable.

```
inventory_0 = current_inventory
for t in 1..horizon:
    inflow_t    = Σ incoming allocations whose arrival_tick == t
    demand_t    = forecast.points[t-1].liters          (≥ 0; 0 past the supplied horizon)
    inventory_t = inventory_{t-1} + inflow_t - demand_t
    if inventory_t ≤ 0 and no crossing yet: record t
```

`expected_demand = Σ demand_t` and `incoming_supply = Σ inflow_t` over the
horizon, so `projected_inventory = current + incoming − expected` telescopes to
exactly `inventory_horizon`.

- `shortage_amount = max(0, −projected_inventory)`
- `surplus_amount  = max(0,  projected_inventory)`
- `ticks_until_stockout` — interpolated *within* the crossing tick:
  `(t−1) + inventory_{t−1} / (inventory_{t−1} − inventory_t)`, clamped to `[0, 1]`
  within the tick. `0.0` when already dry, `None` when no crossing.
- `stockout_tick = current_tick + ceil(ticks_until_stockout)`, `None` when no crossing.

Demand comes from the existing `DemandForecaster`; this module never re-derives
it. Duplicating demand estimation would give two answers to one question.

## Risk rule

Ordered, first match wins, so any projected stockout is at least MEDIUM:

| Level | Condition |
|---|---|
| CRITICAL | `current_inventory ≤ 0`, **or** `ticks_until_stockout ≤ critical_ticks` |
| HIGH | `ticks_until_stockout ≤ high_ticks` |
| MEDIUM | `shortage_amount > 0` **or** `projected_inventory < safety_stock` |
| LOW | otherwise |

`safety_stock = safety_stock_fraction × expected_demand`.

Configuration (`app/config.py`, plus both `.env.example` files):

```
STOCKOUT_HORIZON_TICKS=8
STOCKOUT_CRITICAL_TICKS=1.0
STOCKOUT_HIGH_TICKS=3.0
STOCKOUT_SAFETY_STOCK_FRACTION=0.25
```

## Backend components

### `app/intelligence/stockout.py` — pure engine

Same purity contract as `forecast.py` / `detect.py` / `allocate.py`: no I/O, no
clock, no randomness, deterministic, never raises, never NaN.

Frozen interface:

```python
class RiskLevel(str, Enum):
    LOW = "LOW"; MEDIUM = "MEDIUM"; HIGH = "HIGH"; CRITICAL = "CRITICAL"

LEVEL_ORDER: tuple[RiskLevel, ...] = (LOW, MEDIUM, HIGH, CRITICAL)

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
    basis: str
    horizon_ticks: int
    demand_method: str
    demand_confidence: float
    incoming_sources: tuple[IncomingSupply, ...]

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

`assess_many` takes mappings of
`{station_id, fuel_type, current_inventory, forecast, incoming}` — the shape the
API layer builds — and is what the route calls.

### `app/api/schemas.py` — response models and provider

New models `StockoutAssessmentOut`, `StockoutSummaryOut`, `StockoutThresholdsOut`,
`StockoutResponse`; new provider `get_stockout_analyzer(request)` mirroring
`get_forecaster` (deps → app.state → cached construction from `Settings`).

### `app/api/intelligence.py` — `GET /api/v1/stockout`

Composed exactly as `/forecast` already is: `load_snapshot` → `build_history` →
`compute_forecasts`, plus one `client.get_allocations()` indexed by
`(station, fuel)` into `IncomingSupply`. Query params `station_id`, `fuel_type`,
`risk`, `horizon_ticks`. Unknown `station_id` → typed 404, matching `/forecast`.

### Response

```json
{ "horizon_ticks": 8, "generated_at_tick": 9060, "count": 12,
  "thresholds": {"critical_ticks":1.0,"high_ticks":3.0,
                 "safety_stock_fraction":0.25,"horizon_ticks":8},
  "summary": {"LOW":0,"MEDIUM":0,"HIGH":0,"CRITICAL":12},
  "assessments": [
    { "station_id":"station-mirpur","fuel_type":"DIESEL",
      "current_inventory":0,"expected_demand":11900,"incoming_supply":0,
      "projected_inventory":-11900,"shortage_amount":11900,"surplus_amount":0,
      "stockout_tick":9061,"ticks_until_stockout":0.0,"risk":"CRITICAL",
      "basis":"…","horizon_ticks":8,"demand_method":"holt_winters",
      "demand_confidence":0.42,"incoming_sources":[],"simulated":true } ],
  "simulated": true }
```

## Frontend components

- `src/lib/types/stockout.ts` — response types mirroring the schema.
- `src/features/stockout/useStockout.ts` — fetches `/api/v1/stockout` on the same
  5s cadence and stale/error handling as `useDashboard.ts`.
- `src/features/stockout/StockoutPanel.tsx` — four risk-count tiles, a risk-level
  filter, and a station × fuel table (current / expected demand / incoming /
  projected / shortage / ticks-to-stockout / risk badge), reusing `Panel`,
  `DataTable`, `StatTile`, `StatusPill`.
- `src/app/App.tsx` — a `#stockout` section plus sidebar entry.

## Tests

`backend/tests/test_stockout.py` (pure engine), the six required cases plus
edges:

1. sufficient inventory → LOW, surplus > 0, no `stockout_tick`
2. low inventory → MEDIUM (below safety stock, no stockout within horizon)
3. shortage → negative projection, `shortage_amount > 0`, ≥ MEDIUM
4. immediate stockout → 0 inventory → CRITICAL, `ticks_until_stockout == 0.0`,
   `stockout_tick == current_tick`
5. incoming supply → inflow displaces the crossing; asserts
   `projected == current + incoming − expected`, and that an arrival *after* the
   horizon does not rescue the station
6. multiple future ticks → crossing lands on the correct tick, fractional
   interpolation asserted

Plus threshold configurability, determinism, garbage-in-never-raises, and level
monotonicity.

`backend/tests/test_api_stockout.py` (route), following `test_api_intelligence.py`:
envelope shape, filters, summary counts, 404, degraded-simulator path.

Frontend: Vitest for the hook and panel.

## Known limitation

Against the live simulator every station currently sits at 0 inventory with no
allocations, so all 12 (station, fuel) rows read CRITICAL. That is the honest
reading of the simulated network, not a defect. Variety appears once
recommendations are submitted.

## Non-goals

- No depot-level assessment (stations only, as specified).
- No changes to `DemandForecaster`, `/api/v1/forecast`, or `StockoutRisk`.
  `StockoutRisk` carries *probability*; `RiskLevel` carries a *band*. They are
  deliberately distinct and are not merged.
- No auto-remediation. This engine recommends nothing; it reports.
