"""Local fakes and app-building helpers shared by the API tests.

This module contains no tests. It exists so `test_api_*.py` can exercise the
routes of `app/api/` with `app.dependency_overrides` while A2 (`app/sim`),
A3 (`app/store`), A4-A6 (`app/intelligence`), A7 (`app/llm`) and A9
(`app/observability`) are still being written by other workstreams.

Nothing here stubs a peer *module*: the fakes stand in for the four objects the
contract's dependency providers hand a route (`Settings`, `SimulatorClient`,
`Repository`, `Metrics`) plus the three engines and the explanation service, and
they are installed through FastAPI's own override mechanism.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)  # so sibling test modules can import this one

from fastapi import FastAPI  # noqa: E402

from app.api import schemas  # noqa: E402
from app.api.health import (  # noqa: E402
    get_allocator_or_none,
    get_deepseek_or_none,
    get_metrics_or_none,
    get_repository_or_none,
    get_settings_or_none,
    get_simulator_client_or_none,
)
from app.api.router import api_router  # noqa: E402

SECRET_SENTINEL = "sk-DO-NOT-LEAK-0123456789abcdef"

#: The simulator's own event vocabulary, read from its OpenAPI schema
#: (`components.schemas.EventCreate.properties.type.enum`). CONTRACT.md does not
#: pin it, so it is recorded here rather than invented in a fixture: a fabricated
#: type would let a filter test pass without touching the real contract.
EVENT_TYPES: tuple[str, ...] = (
    "demand_spike",
    "shipment_delay",
    "route_disruption",
    "station_outage",
    "depot_constraint",
    "supply_shortfall",
)

TICK = 42
STATIONS = (
    SimpleNamespace(
        id="ST-1",
        name="Station One",
        region_id="R-1",
        status="OPEN",
        demand_profile="urban_high",
        demand_multiplier=1.0,
        capacity={"DIESEL": 10000.0, "PETROL": 8000.0, "OCTANE": 5000.0},
        inventory={"DIESEL": 900.0, "PETROL": 4000.0, "OCTANE": 2500.0},
    ),
    SimpleNamespace(
        id="ST-2",
        name="Station Two",
        region_id="R-1",
        status="CONSTRAINED",
        demand_profile="highway",
        demand_multiplier=1.3,
        capacity={"DIESEL": 12000.0, "PETROL": 9000.0, "OCTANE": 6000.0},
        inventory={"DIESEL": 300.0, "PETROL": 1200.0, "OCTANE": 900.0},
    ),
)
DEPOTS = (
    SimpleNamespace(
        id="DP-1",
        name="Depot One",
        region_id="R-1",
        status="OPEN",
        dispatch_capacity_per_tick=5000.0,
        capacity={"DIESEL": 50000.0, "PETROL": 50000.0, "OCTANE": 30000.0},
        inventory={"DIESEL": 40000.0, "PETROL": 30000.0, "OCTANE": 20000.0},
    ),
)
ROUTES = (
    SimpleNamespace(
        id="RT-1",
        source_depot_id="DP-1",
        destination_station_id="ST-1",
        transit_ticks=2,
        max_shipment=4000.0,
        status="AVAILABLE",
    ),
    SimpleNamespace(
        id="RT-2",
        source_depot_id="DP-1",
        destination_station_id="ST-2",
        transit_ticks=3,
        max_shipment=3000.0,
        status="DISRUPTED",
    ),
)
REGIONS = (SimpleNamespace(id="R-1", name="North", demand_factor=1.1),)


# ---------------------------------------------------------------------------
# Settings / metrics / repository
# ---------------------------------------------------------------------------


def fake_settings(**overrides: Any) -> SimpleNamespace:
    """Mirrors A1's Settings. Carries a secret to prove it never leaks."""
    values: dict[str, Any] = {
        "llm_available": False,
        "llm_enabled": True,
        "deepseek_model": "deepseek-chat",
        "deepseek_base_url": "https://api.deepseek.com",
        "deepseek_api_key": SECRET_SENTINEL,
        "simulator_base_url": "http://simulator-api:8000",
        "api_port": 9000,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeMetrics:
    """Structural copy of A9's Metrics (CONTRACT.md section 10)."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, int, float]] = []
        self.forecasts: list[tuple[float, bool]] = []
        self.signals: list[tuple[str, str]] = []
        self.recommendations: list[float] = []
        self.llm_calls: list[tuple[str, bool, float, str | None]] = []
        self.breakers: list[tuple[str, str]] = []
        self.api_errors: list[str] = []

    def observe_request(self, method: str, path: str, status: int, duration: float) -> None:
        self.requests.append((method, path, status, duration))

    def record_forecast(self, confidence: float, fallback: bool) -> None:
        self.forecasts.append((confidence, fallback))

    def record_signal(self, kind: str, severity: str) -> None:
        self.signals.append((kind, severity))

    def record_recommendation(self, confidence: float) -> None:
        self.recommendations.append(confidence)

    def record_llm_call(
        self, purpose: str, ok: bool, latency: float, fallback_reason: str | None = None
    ) -> None:
        self.llm_calls.append((purpose, ok, latency, fallback_reason))

    def set_breaker(self, component: str, state: str) -> None:
        self.breakers.append((component, state))

    def record_api_error(self, code: str) -> None:
        self.api_errors.append(code)


def fake_demand_points(station_id: str, fuel_type: str, count: int = 20) -> list[Any]:
    return [
        SimpleNamespace(
            station_id=station_id,
            fuel_type=fuel_type,
            tick=TICK - count + i,
            liters=100.0 + (i * 3) + (50.0 if i == count - 1 else 0.0),
        )
        for i in range(count)
    ]


class FakeRepository:
    """Structural copy of A3's Repository (CONTRACT.md section 6)."""

    def __init__(self, *, empty_history: bool = False) -> None:
        self.empty_history = empty_history
        self.decisions: list[dict[str, Any]] = []
        self.outcomes: list[dict[str, Any]] = []
        self.alerts: list[Any] = []
        self.llm_calls: list[dict[str, Any]] = []
        self._next_id = 1
        self.fail_on: set[str] = set()

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail_on:
            raise RuntimeError(f"repository {name} failed")

    async def init(self) -> None:
        self._maybe_fail("init")

    async def record_snapshot(self, snap: Any) -> None:
        self._maybe_fail("record_snapshot")

    async def latest_snapshot(self) -> Any | None:
        self._maybe_fail("latest_snapshot")
        return None

    async def record_demand_point(
        self, station_id: str, fuel_type: str, tick: int, liters: float
    ) -> None:
        self._maybe_fail("record_demand_point")

    async def demand_series(
        self, station_id: str, fuel_type: str, limit: int = 500
    ) -> list[Any]:
        self._maybe_fail("demand_series")
        if self.empty_history:
            return []
        return fake_demand_points(station_id, fuel_type, min(limit, 20))

    async def record_decision(self, rec: Any) -> int:
        self._maybe_fail("record_decision")
        decision_id = self._next_id
        self._next_id += 1
        payload = _as_dict(rec)
        payload["id"] = decision_id
        self.decisions.append(payload)
        return decision_id

    async def record_decision_outcome(
        self,
        decision_id: int,
        *,
        submitted: bool,
        allocation_id: int | None,
        note: str | None,
    ) -> None:
        self._maybe_fail("record_decision_outcome")
        self.outcomes.append(
            {
                "decision_id": decision_id,
                "submitted": submitted,
                "allocation_id": allocation_id,
                "note": note,
            }
        )

    async def list_decisions(self, limit: int = 50, offset: int = 0) -> list[Any]:
        self._maybe_fail("list_decisions")
        return [SimpleNamespace(**row) for row in self.decisions[offset : offset + limit]]

    async def get_decision(self, decision_id: int) -> Any | None:
        self._maybe_fail("get_decision")
        for row in self.decisions:
            if row.get("id") == decision_id:
                merged = dict(row)
                for outcome in self.outcomes:
                    if outcome["decision_id"] == decision_id:
                        merged.update(
                            {
                                "submitted": outcome["submitted"],
                                "allocation_id": outcome["allocation_id"],
                                "note": outcome["note"],
                            }
                        )
                return SimpleNamespace(**merged)
        return None

    async def record_alert(self, alert: Any) -> int:
        self._maybe_fail("record_alert")
        self.alerts.append(alert)
        return len(self.alerts)

    async def list_alerts(self, limit: int = 50, active_only: bool = False) -> list[Any]:
        self._maybe_fail("list_alerts")
        return self.alerts[:limit]

    async def record_llm_call(
        self, *, purpose: str, model: str, ok: bool, latency_ms: int, fallback_used: bool
    ) -> None:
        self._maybe_fail("record_llm_call")
        self.llm_calls.append(
            {
                "purpose": purpose,
                "model": model,
                "ok": ok,
                "latency_ms": latency_ms,
                "fallback_used": fallback_used,
            }
        )

    async def llm_stats(self) -> Any:
        self._maybe_fail("llm_stats")
        return SimpleNamespace(calls=len(self.llm_calls))


def _as_dict(value: Any) -> dict[str, Any]:
    from app.api.schemas import to_jsonable

    payload = to_jsonable(value)
    return payload if isinstance(payload, dict) else {"value": payload}


# ---------------------------------------------------------------------------
# Simulator client
# ---------------------------------------------------------------------------


class FakeSimulatorClient:
    """Structural copy of A2's SimulatorClient (CONTRACT.md section 5.4)."""

    #: The event types this fixture returns, so a test can assert they are all
    #: real members of the simulator's `EventCreate.type` enum (see EVENT_TYPES).
    EVENT_TYPES_FIXTURE: tuple[str, str] = ("route_disruption", "demand_spike")

    def __init__(self, *, breaker_state: str = "closed", health_status: str = "ok") -> None:
        self._breaker_state = breaker_state
        self.health_status = health_status
        self.allocations: list[Any] = []
        self.cancelled: list[int] = []
        self.injected_faults: list[Any] = []
        self.faults: list[Any] = []
        self.actions: list[str] = []
        self.fail_on: set[str] = set()
        self.next_allocation_id = 700

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail_on:
            raise RuntimeError(f"simulator {name} exploded")

    @property
    def breaker_state(self) -> str:
        return self._breaker_state

    def last_good(self, key: str) -> tuple[Any, float] | None:
        return None

    async def get_health(self) -> Any:
        self._maybe_fail("get_health")
        return {
            "status": self.health_status,
            "database": "ok",
            "simulation": {"status": "RUNNING", "tick": TICK},
        }

    async def get_instance(self) -> Any:
        self._maybe_fail("get_instance")
        return SimpleNamespace(
            id=1,
            scenario_id="scenario-a",
            scenario_version="1.0",
            seed=7,
            sim_time="2026-09-29T10:00:00Z",
            tick=TICK,
            tick_minutes=5,
            status="RUNNING",
        )

    async def get_regions(self) -> list[Any]:
        self._maybe_fail("get_regions")
        return list(REGIONS)

    async def get_depots(self) -> list[Any]:
        self._maybe_fail("get_depots")
        return list(DEPOTS)

    async def get_stations(self) -> list[Any]:
        self._maybe_fail("get_stations")
        return list(STATIONS)

    async def get_routes(self) -> list[Any]:
        self._maybe_fail("get_routes")
        return list(ROUTES)

    async def get_supply_arrivals(self) -> list[Any]:
        self._maybe_fail("get_supply_arrivals")
        return [
            SimpleNamespace(
                id="SA-1",
                depot_id="DP-1",
                fuel_type="DIESEL",
                quantity=10000.0,
                planned_tick=TICK + 4,
                actual_tick=None,
                status="SCHEDULED",
            )
        ]

    async def get_events(self) -> list[Any]:
        self._maybe_fail("get_events")
        # CONTRACT.md does not pin the event vocabulary, so these are taken from
        # the simulator's own OpenAPI schema (components.schemas.EventCreate.
        # properties.type.enum, read from the live instance). A fabricated type
        # here would let a filter test pass while never exercising the real
        # contract; test_api_events asserts this fixture stays inside the enum.
        return [
            {
                "id": 1,
                "type": self.EVENT_TYPES_FIXTURE[0],
                "start_tick": TICK,
                "end_tick": TICK + 12,
                "status": "ACTIVE",
                "parameters": {"region_id": "R-1"},
            },
            {
                "id": 2,
                "type": self.EVENT_TYPES_FIXTURE[1],
                "start_tick": TICK - 20,
                "end_tick": TICK - 8,
                "status": "RESOLVED",
                "parameters": {},
            },
        ]

    async def get_allocations(self) -> list[Any]:
        self._maybe_fail("get_allocations")
        return []

    async def get_demand_history(
        self, station_id: str | None = None, limit: int = 500
    ) -> list[Any]:
        self._maybe_fail("get_demand_history")
        # Deliberately empty: the API then falls back to the snapshot store,
        # which keeps this fake independent of A2's DemandPoint class.
        return []

    async def get_metrics(self) -> Any:
        self._maybe_fail("get_metrics")
        return {
            "served_demand_liters": 12345.0,
            "unmet_demand_liters": 678.0,
            "service_level": 0.95,
            "allocation_liters": 4000.0,
            "allocation_failures": 1,
        }

    async def create_allocation(self, req: Any) -> Any:
        self._maybe_fail("create_allocation")
        # A2's real client does `req.to_payload()` (app/sim/client.py), so the
        # fake does too: if the API ever handed it a plain dict the live path
        # would raise AttributeError, and this test suite must catch that.
        if not hasattr(req, "to_payload"):
            raise TypeError(
                f"create_allocation expects A2's AllocationRequest, got {type(req).__name__}"
            )
        payload = req.to_payload()
        self.allocations.append(payload)
        allocation_id = self.next_allocation_id
        self.next_allocation_id += 1
        return SimpleNamespace(
            id=allocation_id,
            idempotency_key=payload.get("idempotency_key"),
            source_depot_id=payload.get("source_depot_id"),
            destination_station_id=payload.get("destination_station_id"),
            route_id=payload.get("route_id"),
            fuel_type=payload.get("fuel_type"),
            quantity=payload.get("quantity"),
            created_tick=TICK,
            departure_tick=None,
            expected_arrival_tick=None,
            actual_arrival_tick=None,
            status="PENDING",
            failure_reason=None,
        )

    async def cancel_allocation(self, allocation_id: int) -> Any:
        self._maybe_fail("cancel_allocation")
        self.cancelled.append(allocation_id)
        return SimpleNamespace(id=allocation_id, status="CANCELLED")

    async def admin_run(self) -> Any:
        self._maybe_fail("admin_run")
        self.actions.append("run")
        return await self.get_instance()

    async def admin_pause(self) -> Any:
        self._maybe_fail("admin_pause")
        self.actions.append("pause")
        return SimpleNamespace(**{**vars(await self.get_instance()), "status": "PAUSED"})

    async def admin_step(self) -> dict[str, Any]:
        self._maybe_fail("admin_step")
        self.actions.append("step")
        return {"tick": TICK + 1}

    async def admin_reset(self) -> dict[str, Any]:
        self._maybe_fail("admin_reset")
        self.actions.append("reset")
        return {"reset": True}

    async def admin_inject_event(self, req: Any) -> Any:
        self._maybe_fail("admin_inject_event")
        return SimpleNamespace(id=99, type=getattr(req, "type", None), status="SCHEDULED")

    async def admin_inject_fault(self, req: Any) -> Any:
        self._maybe_fail("admin_inject_fault")
        # A2's real client does `req.to_payload()`; see create_allocation above.
        if not hasattr(req, "to_payload"):
            raise TypeError(
                f"admin_inject_fault expects A2's FaultRequest, got {type(req).__name__}"
            )
        payload = req.to_payload()
        self.injected_faults.append(payload)
        fault = SimpleNamespace(
            id=len(self.injected_faults),
            type=payload.get("type"),
            active=True,
        )
        self.faults.append(fault)
        return fault

    async def admin_clear_faults(self) -> dict[str, Any]:
        self._maybe_fail("admin_clear_faults")
        self.faults.clear()
        return {"cleared": True}

    async def admin_get_faults(self) -> list[Any]:
        self._maybe_fail("admin_get_faults")
        return list(self.faults)


# ---------------------------------------------------------------------------
# Engines and the explanation service
# ---------------------------------------------------------------------------


class FakeForecaster:
    """Structural copy of A4's DemandForecaster (CONTRACT.md section 7.1)."""

    def __init__(self, *, min_points: int = 12) -> None:
        self.min_points = min_points
        self.calls: list[tuple[str, int]] = []

    def forecast(self, history: Any, *, horizon_ticks: int = 8) -> Any:
        points = list(history or [])
        self.calls.append(("forecast", len(points)))
        base = points[-1].liters if points else 100.0
        return SimpleNamespace(
            station_id=points[0].station_id if points else "unknown",
            fuel_type=points[0].fuel_type if points else "DIESEL",
            points=tuple(
                SimpleNamespace(
                    tick=TICK + 1 + i,
                    liters=base + i,
                    lower=base + i - 5.0,
                    upper=base + i + 5.0,
                )
                for i in range(horizon_ticks)
            ),
            confidence=0.7,
            method="ewma" if points else "fallback",
            fallback_used=not points,
        )

    def stockout_risk(
        self, *, inventory_liters: float, forecast: Any, transit_ticks: int | None = None
    ) -> Any:
        self.calls.append(("stockout_risk", 0))
        probability = 0.8 if inventory_liters < 1000 else 0.1
        return SimpleNamespace(
            probability=probability,
            ticks_to_stockout=2.5 if probability > 0.5 else None,
            confidence=0.6,
            basis=f"inventory {inventory_liters} vs forecast demand",
        )


class FakeDetector:
    """Structural copy of A5's AnomalyDetector (CONTRACT.md section 7.2)."""

    def __init__(self) -> None:
        self.calls = 0

    def detect(self, *, snapshot: Any, history: Any) -> list[Any]:
        self.calls += 1
        if not snapshot or not history:
            return []
        return [
            SimpleNamespace(
                kind="demand_anomaly",
                severity="critical",
                entity_type="station",
                entity_id="ST-2",
                detected_at_tick=TICK,
                summary="ST-2 DIESEL demand is 6.1 MAD above its median",
                evidence={"mad": 12.0, "z": 6.1},
                confidence=0.91,
            ),
            SimpleNamespace(
                kind="supply_shortfall",
                severity="warning",
                entity_type="depot",
                entity_id="DP-1",
                detected_at_tick=TICK,
                summary="DP-1 OCTANE cover is below two ticks",
                evidence={"cover_ticks": 1.4},
                confidence=0.62,
            ),
        ]


class FakeAllocator:
    """Structural copy of A6's AllocationEngine (CONTRACT.md section 7.3)."""

    def __init__(self, *, policy: str = "optimizer") -> None:
        self.policy = policy
        self.calls: list[dict[str, Any]] = []

    def recommend(
        self,
        *,
        snapshot: Any,
        signals: Any,
        forecasts: Any,
        budget_liters: float | None = None,
    ) -> list[Any]:
        self.calls.append(
            {
                "signals": len(list(signals or [])),
                "forecasts": len(dict(forecasts or {})),
                "budget_liters": budget_liters,
            }
        )
        return [
            SimpleNamespace(
                id="rec-1",
                station_id="ST-2",
                depot_id="DP-1",
                route_id="RT-1",
                fuel_type="DIESEL",
                quantity_liters=2500.0,
                rationale="ST-2 DIESEL is 1.4 ticks from stockout after a demand spike",
                constraints=("route max_shipment", "depot dispatch capacity"),
                expected_impact={"risk_before": 0.81, "risk_after": 0.12},
                alternatives=(
                    SimpleNamespace(
                        depot_id="DP-1",
                        route_id="RT-3",
                        quantity_liters=1800.0,
                        reason="lower transit capacity",
                    ),
                ),
                confidence=0.83,
                simulated=True,
            )
        ]


class FakeExplanationService:
    """Structural copy of A7's ExplanationService (CONTRACT.md section 8.2)."""

    def __init__(self, *, source: str = "fallback", boom: bool = False) -> None:
        self.source = source
        self.boom = boom
        self.calls: list[str] = []

    def _result(self, text: str) -> Any:
        if self.boom:
            raise RuntimeError("llm exploded")
        return SimpleNamespace(
            text=text,
            source=self.source,
            model="deepseek-chat" if self.source == "llm" else None,
            degraded=self.source == "fallback",
            simulated=True,
        )

    async def explain_recommendation(self, rec: Any, signals: Any) -> Any:
        self.calls.append("explain_recommendation")
        return self._result(f"Ship {getattr(rec, 'quantity_liters', 0)} L because risk is high.")

    async def summarize_network(self, snapshot: Any, signals: Any) -> Any:
        self.calls.append("summarize_network")
        return self._result("The network is degraded in R-1; two signals are active.")

    async def investigate(self, question: str, *, context: dict[str, Any]) -> Any:
        self.calls.append("investigate")
        return self._result(f"No LLM key configured; deterministic answer to: {question}")


# ---------------------------------------------------------------------------
# App construction
# ---------------------------------------------------------------------------


def build_app(**overrides: Any) -> FastAPI:
    """A FastAPI app carrying only A8's router, with peer deps overridden.

    Only the dependency providers declared in `app/api/schemas.py` and
    `app/api/health.py` are replaced. Nothing is monkeypatched.
    """
    app = FastAPI(title="API test app")
    app.include_router(api_router)

    settings = overrides.get("settings", fake_settings())
    metrics = overrides.get("metrics", FakeMetrics())
    client = overrides.get("client", FakeSimulatorClient())
    repository = overrides.get("repository", FakeRepository())
    forecaster = overrides.get("forecaster", FakeForecaster())
    detector = overrides.get("detector", FakeDetector())
    allocator = overrides.get("allocator", FakeAllocator())
    explainer = overrides.get("explainer", FakeExplanationService())

    mapping: dict[Any, Any] = {
        schemas.get_settings: settings,
        schemas.get_simulator_client: client,
        schemas.get_repository: repository,
        schemas.get_metrics: metrics,
        schemas.get_forecaster: forecaster,
        schemas.get_detector: detector,
        schemas.get_allocator: allocator,
        schemas.get_explanation_service: explainer,
        get_settings_or_none: settings,
        get_simulator_client_or_none: client,
        get_repository_or_none: repository,
        get_metrics_or_none: metrics,
        get_allocator_or_none: allocator,
        get_deepseek_or_none: overrides.get("llm_client"),
    }
    for provider, value in overrides.get("extra_overrides", {}).items():
        mapping[provider] = value
    for provider, value in mapping.items():
        if value is None and provider is get_deepseek_or_none:
            app.dependency_overrides[provider] = lambda: None
            continue
        app.dependency_overrides[provider] = (lambda v=value: (lambda: v))()
    return app


def build_bare_app() -> FastAPI:
    """The router on an app with *no* overrides — used to prove /health needs none."""
    app = FastAPI(title="API bare test app")
    app.include_router(api_router)
    return app


def build_full_app() -> FastAPI:
    """A1's real application factory, for tests about the *assembled* app.

    Only the route table is inspected: `create_app` does not enter its lifespan
    until the app is used as a context manager, so nothing here connects to the
    simulator or the database.
    """
    from app.main import create_app

    return create_app()


def client(app: FastAPI) -> Any:
    from fastapi.testclient import TestClient

    return TestClient(app, raise_server_exceptions=False)


def error_code(response: Any) -> str | None:
    body = response.json()
    detail = body.get("detail")
    if isinstance(detail, dict):
        return detail.get("code")
    return None
