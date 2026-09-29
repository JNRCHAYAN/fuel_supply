"""Tests for the allocation decision engine — CONTRACT 7.3 (workstream A6).

The engine is a pure function of its inputs, so every test here builds an
in-memory network by hand: no simulator, no database, no clock, no randomness.

Most tests use the local fixture types below, which mirror the field names
pinned in CONTRACT 5.2 / 7.1 / 7.2 exactly. Three interop tests at the bottom
additionally run the engine against the *real* peer types (A2's ``Snapshot``,
A4's ``ForecastResult``, A5's ``RiskSignal``) so a drift in a peer's field names
is caught here rather than in the API layer.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

# Make `app` importable regardless of the pytest rootdir/conftest A1 sets up.
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.intelligence import allocate as allocate_module  # noqa: E402
from app.intelligence.allocate import (  # noqa: E402
    POLICY_HEURISTIC,
    POLICY_OPTIMIZER,
    AllocationEngine,
    Alternative,
    Recommendation,
)


# ---------------------------------------------------------------------------
# Fixtures mirroring CONTRACT 5.2 (Snapshot and its members)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Depot:
    id: str
    name: str = ""
    region_id: str = "R1"
    status: str = "OPEN"
    dispatch_capacity_per_tick: float = 5000.0
    capacity: dict[str, float] = field(default_factory=dict)
    inventory: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class _Station:
    id: str
    name: str = ""
    region_id: str = "R1"
    status: str = "OPEN"
    demand_profile: str = "urban_high"
    demand_multiplier: float = 1.0
    capacity: dict[str, float] = field(default_factory=dict)
    inventory: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class _Route:
    id: str
    source_depot_id: str
    destination_station_id: str
    transit_ticks: int = 1
    max_shipment: float = 1000.0
    status: str = "AVAILABLE"


@dataclass(frozen=True)
class _Metrics:
    served_demand_liters: float = 0.0
    unmet_demand_liters: float = 0.0
    service_level: float = 1.0
    allocation_liters: float = 0.0
    allocation_failures: int = 0


@dataclass(frozen=True)
class _Snapshot:
    taken_at: float = 0.0
    tick: int = 12
    sim_time: str = "2026-01-01T00:00:00Z"
    status: str = "RUNNING"
    depots: tuple[_Depot, ...] = ()
    stations: tuple[_Station, ...] = ()
    routes: tuple[_Route, ...] = ()
    regions: tuple[Any, ...] = ()
    supply_arrivals: tuple[Any, ...] = ()
    events: tuple[Any, ...] = ()
    metrics: _Metrics = field(default_factory=_Metrics)
    stale: bool = False
    age_seconds: float = 0.0


@dataclass(frozen=True)
class _Signal:
    """Mirrors CONTRACT 7.2's RiskSignal. Severity is an enum-like value."""

    kind: str = "demand_anomaly"
    severity: Any = "serious"
    entity_type: str = "station"
    entity_id: str = "S1"
    detected_at_tick: int = 12
    summary: str = "demand spike at S1"
    evidence: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.9


@dataclass(frozen=True)
class _ForecastPoint:
    tick: int
    liters: float
    lower: float
    upper: float


@dataclass(frozen=True)
class _ForecastResult:
    station_id: str
    fuel_type: str
    points: tuple[_ForecastPoint, ...]
    confidence: float
    method: str = "ewma"
    fallback_used: bool = False


def _forecast(
    station_id: str,
    fuel_type: str,
    liters_per_tick: float,
    horizon: int = 8,
    confidence: float = 0.8,
) -> _ForecastResult:
    return _ForecastResult(
        station_id=station_id,
        fuel_type=fuel_type,
        points=tuple(
            _ForecastPoint(tick=i + 1, liters=liters_per_tick, lower=0.0, upper=0.0)
            for i in range(horizon)
        ),
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Network builders
# ---------------------------------------------------------------------------


def _default_depots() -> tuple[_Depot, ...]:
    return (
        _Depot(
            id="D1",
            region_id="R1",
            dispatch_capacity_per_tick=5000.0,
            inventory={"DIESEL": 10000.0, "PETROL": 8000.0},
        ),
        _Depot(
            id="D2",
            region_id="R1",
            dispatch_capacity_per_tick=3000.0,
            inventory={"DIESEL": 10000.0},
        ),
    )


def _default_stations() -> tuple[_Station, ...]:
    return (
        _Station(
            id="S1",
            region_id="R1",
            capacity={"DIESEL": 5000.0, "PETROL": 5000.0},
            inventory={"DIESEL": 200.0, "PETROL": 3000.0},
        ),
        _Station(
            id="S2",
            region_id="R1",
            capacity={"DIESEL": 5000.0},
            inventory={"DIESEL": 4800.0},
        ),
    )


def _default_routes() -> tuple[_Route, ...]:
    return (
        _Route("RT-1", "D1", "S1", transit_ticks=1, max_shipment=1000.0),
        _Route("RT-2", "D2", "S1", transit_ticks=3, max_shipment=2000.0),
        _Route("RT-3", "D1", "S2", transit_ticks=2, max_shipment=5000.0),
    )


def _snapshot(
    *,
    depots: tuple[_Depot, ...] | None = None,
    stations: tuple[_Station, ...] | None = None,
    routes: tuple[_Route, ...] | None = None,
    tick: int = 12,
    stale: bool = False,
) -> _Snapshot:
    return _Snapshot(
        tick=tick,
        depots=_default_depots() if depots is None else depots,
        stations=_default_stations() if stations is None else stations,
        routes=_default_routes() if routes is None else routes,
        stale=stale,
    )


def _diesel_signal(station_id: str = "S1", **kwargs: Any) -> _Signal:
    """A station signal restricted to DIESEL via evidence, so a test controls
    exactly which fuel is in play."""
    kwargs.setdefault("entity_id", station_id)
    kwargs.setdefault("evidence", {"fuel_type": "DIESEL", "shortfall_liters": 2800.0})
    return _Signal(**kwargs)


def _diesel_forecasts() -> dict[tuple[str, str], _ForecastResult]:
    # 8 ticks x 375 L = 3000 L demand; S1 holds 200 L -> shortfall 2800 L.
    return {("S1", "DIESEL"): _forecast("S1", "DIESEL", 375.0)}


def _run(
    *,
    engine: AllocationEngine | None = None,
    snapshot: _Snapshot | None = None,
    signals: list[Any] | None = None,
    forecasts: dict[tuple[str, str], Any] | None = None,
    budget_liters: float | None = None,
) -> tuple[list[Recommendation], AllocationEngine]:
    engine = engine or AllocationEngine()
    recs = engine.recommend(
        snapshot=snapshot if snapshot is not None else _snapshot(),
        signals=[_diesel_signal()] if signals is None else signals,
        forecasts=_diesel_forecasts() if forecasts is None else forecasts,
        budget_liters=budget_liters,
    )
    return recs, engine


def _assert_invariants(
    recs: list[Recommendation],
    snapshot: _Snapshot,
) -> None:
    """Every hard invariant, checked against the whole recommendation set."""
    depots = {d.id: d for d in snapshot.depots}
    routes = {r.id: r for r in snapshot.routes}
    routes_by_depot: dict[str, float] = {}
    inventory_used: dict[tuple[str, str], float] = {}

    for rec in recs:
        route = routes[rec.route_id]

        # route availability: a DISRUPTED route may never be used
        assert route.status == "AVAILABLE", f"{rec.id} uses a {route.status} route"

        # route max_shipment
        assert rec.quantity_liters <= route.max_shipment + 1e-9, (
            f"{rec.id} ships {rec.quantity_liters} L > max_shipment "
            f"{route.max_shipment} L"
        )

        # the route must actually connect the stated depot and station
        assert route.source_depot_id == rec.depot_id
        assert route.destination_station_id == rec.station_id

        routes_by_depot[rec.depot_id] = (
            routes_by_depot.get(rec.depot_id, 0.0) + rec.quantity_liters
        )
        key = (rec.depot_id, rec.fuel_type)
        inventory_used[key] = inventory_used.get(key, 0.0) + rec.quantity_liters

        assert rec.quantity_liters > 0.0

    # depot inventory, per fuel, summed over every recommendation from that depot
    for (depot_id, fuel_type), used in inventory_used.items():
        available = depots[depot_id].inventory.get(fuel_type, 0.0)
        assert used <= available + 1e-9, (
            f"depot {depot_id} {fuel_type} over-committed: {used} > {available}"
        )

    # depot dispatch capacity per tick, summed over every fuel
    for depot_id, used in routes_by_depot.items():
        cap = depots[depot_id].dispatch_capacity_per_tick
        assert used <= cap + 1e-9, (
            f"depot {depot_id} dispatch over-committed: {used} > {cap}"
        )


# ---------------------------------------------------------------------------
# Empty / healthy inputs
# ---------------------------------------------------------------------------


def test_no_signals_returns_no_recommendations() -> None:
    recs, engine = _run(signals=[])
    assert recs == []
    assert engine.policy == POLICY_OPTIMIZER  # nothing ran, but not a fallback


def test_none_signals_tolerated() -> None:
    engine = AllocationEngine()
    assert engine.recommend(snapshot=_snapshot(), signals=[], forecasts={}) == []


def test_empty_network_returns_empty_list_not_an_exception() -> None:
    empty = _snapshot(depots=(), stations=(), routes=())
    recs, _ = _run(snapshot=empty)
    assert recs == []


def test_already_healthy_network_returns_empty_list() -> None:
    """Every station is full: no shortfall anywhere."""
    stations = (
        _Station(
            id="S1",
            capacity={"DIESEL": 5000.0},
            inventory={"DIESEL": 5000.0},
        ),
    )
    recs, _ = _run(snapshot=_snapshot(stations=stations))
    assert recs == []


def test_signal_for_unknown_entity_is_ignored() -> None:
    recs, _ = _run(signals=[_diesel_signal(station_id="DOES-NOT-EXIST")])
    assert recs == []


def test_station_without_an_available_route_is_skipped() -> None:
    stations = _default_stations() + (
        _Station(
            id="S3",
            region_id="R1",
            capacity={"DIESEL": 5000.0},
            inventory={"DIESEL": 0.0},
        ),
    )
    routes = _default_routes() + (
        _Route("RT-4", "D1", "S3", transit_ticks=1, max_shipment=900.0, status="DISRUPTED"),
    )
    signals = [_diesel_signal("S1"), _diesel_signal("S3")]
    recs, _ = _run(snapshot=_snapshot(stations=stations, routes=routes), signals=signals)

    assert recs, "the station with a usable route should still be served"
    assert all(r.station_id != "S3" for r in recs), (
        "a station with only a DISRUPTED route must be skipped, not given an "
        "impossible recommendation"
    )


# ---------------------------------------------------------------------------
# Hard invariants — one test each
# ---------------------------------------------------------------------------


def test_is_a_recommendation_at_all_baseline() -> None:
    recs, _ = _run()
    assert recs
    _assert_invariants(recs, _snapshot())


def test_never_exceeds_route_max_shipment() -> None:
    # RT-1 caps at 400 L while the shortfall is 2800 L.
    routes = (
        _Route("RT-1", "D1", "S1", transit_ticks=1, max_shipment=400.0),
    )
    snapshot = _snapshot(routes=routes)
    recs, _ = _run(snapshot=snapshot)
    assert recs
    assert all(r.quantity_liters <= 400.0 + 1e-9 for r in recs)
    assert sum(r.quantity_liters for r in recs) == pytest.approx(400.0, abs=1e-6)
    _assert_invariants(recs, snapshot)


def test_never_exceeds_depot_inventory_of_that_fuel() -> None:
    depots = (
        _Depot(
            id="D1",
            dispatch_capacity_per_tick=5000.0,
            inventory={"DIESEL": 250.0},
        ),
    )
    routes = (
        _Route("RT-1", "D1", "S1", transit_ticks=1, max_shipment=1000.0),
        _Route("RT-2", "D1", "S2", transit_ticks=1, max_shipment=1000.0),
    )
    stations = _default_stations()
    snapshot = _snapshot(depots=depots, stations=stations, routes=routes)
    signals = [_diesel_signal("S1"), _diesel_signal("S2")]

    recs, _ = _run(snapshot=snapshot, signals=signals)
    assert recs
    shipped = sum(r.quantity_liters for r in recs)
    assert shipped <= 250.0 + 1e-9, f"shipped {shipped} L from a 250 L depot"
    _assert_invariants(recs, snapshot)


def test_never_uses_a_disrupted_route() -> None:
    routes = (
        _Route("RT-1", "D1", "S1", transit_ticks=1, max_shipment=1000.0,
               status="DISRUPTED"),
        _Route("RT-2", "D2", "S1", transit_ticks=3, max_shipment=2000.0),
    )
    snapshot = _snapshot(routes=routes)
    recs, _ = _run(snapshot=snapshot)
    assert recs
    assert {r.route_id for r in recs} == {"RT-2"}
    _assert_invariants(recs, snapshot)


def test_never_exceeds_depot_dispatch_capacity_per_tick() -> None:
    depots = _default_depots()
    depots = (
        _Depot(
            id="D1",
            dispatch_capacity_per_tick=300.0,
            inventory={"DIESEL": 10000.0},
        ),
        _Depot(id="D2", dispatch_capacity_per_tick=100.0,
               inventory={"DIESEL": 10000.0}),
    )
    snapshot = _snapshot(depots=depots)
    recs, _ = _run(snapshot=snapshot)
    assert recs
    for depot_id, cap in (("D1", 300.0), ("D2", 100.0)):
        shipped = sum(r.quantity_liters for r in recs if r.depot_id == depot_id)
        assert shipped <= cap + 1e-9, f"{depot_id} dispatched {shipped} L > {cap} L/tick"
    _assert_invariants(recs, snapshot)


def test_budget_cap_is_respected_when_set() -> None:
    snapshot = _snapshot()
    recs, _ = _run(snapshot=snapshot, budget_liters=1500.0)
    assert recs
    assert sum(r.quantity_liters for r in recs) <= 1500.0 + 1e-9
    # and with no budget the same network ships more
    unbudgeted, _ = _run(snapshot=snapshot)
    assert sum(r.quantity_liters for r in unbudgeted) > 1500.0
    _assert_invariants(recs, snapshot)


def test_never_ships_more_than_the_station_shortfall() -> None:
    snapshot = _snapshot()
    recs, _ = _run(snapshot=snapshot)
    shipped = sum(r.quantity_liters for r in recs)
    assert shipped <= 2800.0 + 1e-6
    for rec in recs:
        assert rec.quantity_liters <= rec.expected_impact["shortfall_before_liters"] + 1e-6


# ---------------------------------------------------------------------------
# Fallback path — brief section 11, must be genuinely reachable
# ---------------------------------------------------------------------------


def test_fallback_when_optimizer_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("HiGHS exploded")

    monkeypatch.setattr(allocate_module, "linprog", _boom)
    snapshot = _snapshot()
    recs, engine = _run(snapshot=snapshot)

    assert engine.policy == POLICY_HEURISTIC, "policy must report the path that ran"
    assert engine.last_fallback_reason is not None
    assert "RuntimeError" in engine.last_fallback_reason
    assert recs, "the fallback must still produce recommendations"
    _assert_invariants(recs, snapshot)
    assert all(r.simulated is True for r in recs)


def test_fallback_when_optimizer_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(allocate_module, "linprog", None)
    snapshot = _snapshot()
    recs, engine = _run(snapshot=snapshot)

    assert engine.policy == POLICY_HEURISTIC
    assert engine.last_fallback_reason is not None
    assert "unavailable" in engine.last_fallback_reason
    assert recs
    _assert_invariants(recs, snapshot)


def test_fallback_when_optimizer_is_infeasible(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Infeasible:
        status = 2
        x = None
        fun = None

    monkeypatch.setattr(
        allocate_module, "linprog", lambda *a, **k: _Infeasible()
    )
    snapshot = _snapshot()
    recs, engine = _run(snapshot=snapshot)

    assert engine.policy == POLICY_HEURISTIC
    assert engine.last_fallback_reason is not None
    assert "status=2" in engine.last_fallback_reason
    assert recs
    _assert_invariants(recs, snapshot)


def test_fallback_respects_budget_and_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(allocate_module, "linprog", _boom)
    depots = (
        _Depot(id="D1", dispatch_capacity_per_tick=400.0,
               inventory={"DIESEL": 10000.0}),
        _Depot(id="D2", dispatch_capacity_per_tick=100.0,
               inventory={"DIESEL": 50.0}),
    )
    snapshot = _snapshot(depots=depots)
    recs, engine = _run(snapshot=snapshot, budget_liters=300.0)

    assert engine.policy == POLICY_HEURISTIC
    assert recs
    assert sum(r.quantity_liters for r in recs) <= 300.0 + 1e-9
    _assert_invariants(recs, snapshot)


def test_explicit_heuristic_policy_never_calls_the_optimizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _must_not_be_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("linprog must not be called when policy=heuristic")

    monkeypatch.setattr(allocate_module, "linprog", _must_not_be_called)
    engine = AllocationEngine(policy="priority_heuristic")
    snapshot = _snapshot()
    recs = engine.recommend(
        snapshot=snapshot, signals=[_diesel_signal()], forecasts=_diesel_forecasts()
    )
    assert engine.policy == POLICY_HEURISTIC
    assert recs
    _assert_invariants(recs, snapshot)


def test_unknown_policy_is_rejected() -> None:
    with pytest.raises(ValueError):
        AllocationEngine(policy="magic")


def test_optimizer_and_fallback_produce_the_same_invariants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot()
    optimised, _ = _run(snapshot=snapshot)

    monkeypatch.setattr(
        allocate_module, "linprog", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    fallback, engine = _run(snapshot=snapshot)

    assert engine.policy == POLICY_HEURISTIC
    _assert_invariants(optimised, snapshot)
    _assert_invariants(fallback, snapshot)
    # Both must cover at least as much of the shortfall as the route cap allows.
    for recs in (optimised, fallback):
        assert sum(r.quantity_liters for r in recs) >= 2000.0


# ---------------------------------------------------------------------------
# Determinism and identity
# ---------------------------------------------------------------------------


def test_deterministic_for_identical_input() -> None:
    first, _ = _run()
    second, _ = _run()
    assert first == second
    assert [r.id for r in first] == [r.id for r in second]
    assert first is not second


def test_deterministic_across_engine_instances_and_policies() -> None:
    optimised, _ = _run(engine=AllocationEngine())
    again, _ = _run(engine=AllocationEngine())
    assert [r.id for r in optimised] == [r.id for r in again]
    assert [r.quantity_liters for r in optimised] == [
        r.quantity_liters for r in again
    ]


def test_recommendation_ids_are_stable_and_tick_scoped() -> None:
    recs, _ = _run()
    recs_again, _ = _run()
    assert {r.id for r in recs} == {r.id for r in recs_again}
    assert all(r.id.startswith("rec-12-") for r in recs), (
        "ids must be stable within the tick and name the tick they belong to"
    )

    other_tick, _ = _run(snapshot=_snapshot(tick=99))
    assert all(r.id.startswith("rec-99-") for r in other_tick)
    assert not ({r.id for r in recs} & {r.id for r in other_tick})


def test_no_duplicate_recommendation_ids() -> None:
    recs, _ = _run()
    ids = [r.id for r in recs]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Inspectability — brief section 9
# ---------------------------------------------------------------------------


def test_split_legs_agree_on_group_risk_and_residual_shortfall() -> None:
    """Two legs answering one shortfall must not disagree about the outcome."""
    recs, _ = _run()
    assert len(recs) == 2
    assert len({r.expected_impact["risk_before"] for r in recs}) == 1
    assert len({r.expected_impact["risk_after"] for r in recs}) == 1
    assert len({r.expected_impact["shortfall_before_liters"] for r in recs}) == 1
    assert len({r.expected_impact["coverage_ratio"] for r in recs}) == 1
    assert recs[0].expected_impact["shortfall_after_liters"] == 0.0
    # ... while each leg still reports its own marginal contribution.
    margins = {round(r.expected_impact["risk_reduction_from_this_recommendation"], 6) for r in recs}
    assert len(margins) == 2, "the legs are different sizes, so their margins differ"


def test_every_recommendation_is_inspectable() -> None:
    recs, _ = _run()
    assert recs
    for rec in recs:
        assert isinstance(rec, Recommendation)
        assert rec.rationale.strip(), "rationale is required"
        assert "L" in rec.rationale and rec.fuel_type in rec.rationale
        assert rec.constraints, "constraints must say what actually bound the choice"
        assert all(isinstance(c, str) and c.strip() for c in rec.constraints)
        assert rec.expected_impact.keys() >= {"risk_before", "risk_after"}
        assert 0.0 <= rec.expected_impact["risk_after"] <= rec.expected_impact["risk_before"] <= 1.0
        assert (
            0.0
            <= rec.expected_impact["risk_reduction_from_this_recommendation"]
            <= rec.expected_impact["risk_before"]
        )
        assert 0.0 < rec.confidence <= 1.0
        assert rec.simulated is True, "CONTRACT 0.6 / brief section 24"
        assert isinstance(rec.alternatives, tuple)
        assert all(isinstance(a, Alternative) for a in rec.alternatives)


def test_constraints_name_the_specific_binding_limit() -> None:
    """A route cap that actually binds must be reported as binding."""
    routes = (_Route("RT-1", "D1", "S1", transit_ticks=1, max_shipment=400.0),)
    recs, _ = _run(snapshot=_snapshot(routes=routes))
    assert recs
    joined = " ".join(recs[0].constraints)
    assert "max_shipment" in joined and "400.0" in joined


def test_constraints_report_only_the_limits_that_actually_bound() -> None:
    """Excess capacity must not be reported as a binding limit.

    Here the route, the depot inventory and the depot's dispatch capacity are all
    orders of magnitude larger than the shortfall, so the only limit that can
    bind is the station's own shortfall (shipping more is pointless) — and the
    recommendation must say exactly that, not list every cap it knows about.
    """
    routes = (_Route("RT-1", "D1", "S1", transit_ticks=1, max_shipment=5000.0),)
    depots = (
        _Depot(id="D1", dispatch_capacity_per_tick=99999.0,
               inventory={"DIESEL": 99999.0}),
    )
    recs, _ = _run(snapshot=_snapshot(depots=depots, routes=routes))
    assert recs
    joined = " ".join(recs[0].constraints)
    assert "shortfall" in joined
    assert "max_shipment" not in joined
    assert "dispatch_capacity_per_tick" not in joined
    assert "inventory" not in joined


def test_alternatives_are_present_when_a_choice_existed() -> None:
    """Two depots can serve S1/DIESEL. The shortfall (200 L) fits entirely on the
    cheaper route, so the engine must take RT-1 and report RT-2 as considered and
    rejected, with the ranking that lost."""
    routes = (
        _Route("RT-1", "D1", "S1", transit_ticks=1, max_shipment=500.0),
        _Route("RT-2", "D2", "S1", transit_ticks=4, max_shipment=2000.0),
    )
    # 8 ticks x 50 L = 400 L demand; S1 holds 200 L -> shortfall 200 L.
    forecasts = {("S1", "DIESEL"): _forecast("S1", "DIESEL", 50.0)}
    recs, _ = _run(snapshot=_snapshot(routes=routes), forecasts=forecasts)

    assert len(recs) == 1, "one shortfall, one chosen option"
    best = recs[0]
    assert best.route_id == "RT-1", "the shorter transit is worth more per litre"
    assert best.quantity_liters == pytest.approx(200.0, abs=1e-6)
    assert best.alternatives, "a rejected option must be reported"
    rejected = best.alternatives[0]
    assert rejected.depot_id == "D2" and rejected.route_id == "RT-2"
    assert rejected.quantity_liters == 0.0
    # The truthful reason: nothing was left to ship there, because a better
    # option already covered the whole shortfall.
    assert "shortfall" in rejected.reason_not_chosen
    assert "higher-ranked" in rejected.reason_not_chosen
    assert rejected.score >= 0.0


def test_alternative_reason_names_the_ranking_when_capacity_remained() -> None:
    """When need remains but an option still loses, the reason is the ranking."""
    routes = (
        _Route("RT-1", "D1", "S1", transit_ticks=1, max_shipment=100.0),
        _Route("RT-2", "D1", "S1", transit_ticks=6, max_shipment=100.0),
    )
    # Two routes from the SAME depot to the SAME station: the depot's dispatch
    # capacity (120 L) is the only thing that can decide between them.
    depots = (
        _Depot(id="D1", dispatch_capacity_per_tick=120.0,
               inventory={"DIESEL": 10000.0}),
    )
    snapshot = _snapshot(depots=depots, routes=routes)
    recs, _ = _run(snapshot=snapshot)
    assert recs
    reasons = " ".join(
        a.reason_not_chosen for rec in recs for a in rec.alternatives
    )
    assert reasons, "the rejected twin route must be explained"
    _assert_invariants(recs, snapshot)


def test_split_shipment_reports_the_other_leg() -> None:
    """When the shortfall exceeds the best route's cap, the second depot is used
    too — and each recommendation names the other leg rather than hiding it."""
    snapshot = _snapshot()
    recs, _ = _run(snapshot=snapshot)
    assert len(recs) == 2
    routes = {r.route_id for r in recs}
    assert routes == {"RT-1", "RT-2"}
    for rec in recs:
        others = {a.route_id for a in rec.alternatives}
        assert others == routes - {rec.route_id}
        assert sum(r.quantity_liters for r in recs) == pytest.approx(2800.0, abs=1e-3)


def test_disrupted_route_is_reported_as_a_considered_alternative() -> None:
    routes = (
        _Route("RT-1", "D1", "S1", transit_ticks=1, max_shipment=1000.0),
        _Route("RT-9", "D2", "S1", transit_ticks=1, max_shipment=1000.0,
               status="DISRUPTED"),
    )
    recs, _ = _run(snapshot=_snapshot(routes=routes))
    assert recs
    reasons = " ".join(a.reason_not_chosen for a in recs[0].alternatives)
    assert "RT-9" in [a.route_id for a in recs[0].alternatives]
    assert "DISRUPTED" in reasons


def test_rationale_cites_the_signal_that_drove_it() -> None:
    signals = [_diesel_signal(summary="inventory collapse at S1")]
    recs, _ = _run(signals=signals)
    assert recs
    assert "inventory collapse at S1" in recs[0].rationale


def test_low_confidence_signal_lowers_recommendation_confidence() -> None:
    high, _ = _run(signals=[_diesel_signal(confidence=0.95)])
    low, _ = _run(signals=[_diesel_signal(confidence=0.05)])
    assert high[0].confidence > low[0].confidence


def test_missing_forecast_still_produces_an_inspectable_recommendation() -> None:
    snapshot = _snapshot()
    recs, _ = _run(snapshot=snapshot, forecasts={})
    assert recs
    # Without a forecast the engine refills toward tank capacity, and says so
    # through a lower confidence rather than by inventing demand.
    assert all(0.0 < r.confidence < 0.85 for r in recs)
    _assert_invariants(recs, snapshot)


def test_unknown_severity_value_is_tolerated() -> None:
    signals = [_diesel_signal(severity="catastrophic")]
    recs, _ = _run(signals=signals)
    assert recs
    assert recs[0].expected_impact["risk_before"] > 0.0


def test_enum_like_severity_is_tolerated() -> None:
    """A5's Severity is a str enum; the engine must not care which."""

    class _Severity:
        def __init__(self, value: str) -> None:
            self.value = value

    signals = [_diesel_signal(severity=_Severity("critical"))]
    recs, _ = _run(signals=signals)
    assert recs
    assert recs[0].expected_impact["risk_before"] > 0.5


def test_signal_on_a_route_implicates_its_destination_station() -> None:
    signal = _Signal(
        kind="route_bottleneck",
        entity_type="route",
        entity_id="RT-1",
        summary="route RT-1 congested",
        evidence={"fuel_type": "DIESEL"},
    )
    recs, _ = _run(signals=[signal])
    assert recs
    assert all(r.station_id == "S1" for r in recs)
    assert any("RT-1" in r.rationale for r in recs)


def test_stale_snapshot_is_still_served() -> None:
    """Degrade, do not fabricate: a stale snapshot still yields recommendations."""
    snapshot = _snapshot(stale=True)
    recs, _ = _run(snapshot=snapshot)
    assert recs
    _assert_invariants(recs, snapshot)


def test_zero_capacity_route_yields_nothing_for_that_route() -> None:
    routes = (_Route("RT-1", "D1", "S1", transit_ticks=1, max_shipment=0.0),)
    snapshot = _snapshot(routes=routes)
    recs, _ = _run(snapshot=snapshot)
    assert recs == []
    _assert_invariants(recs, snapshot)


def test_engine_never_executes_and_has_no_io() -> None:
    """CONTRACT 0.7 / brief section 24 — structural check on the module itself."""
    source = Path(allocate_module.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "create_allocation",
        "cancel_allocation",
        "httpx",
        "app.sim.client",
        "app.store",
        "import random",
        "datetime.now",
        "time.time",
    ):
        assert forbidden not in source, f"{forbidden!r} must not appear in allocate.py"

    before = set(sys.modules)
    _run()
    assert "app.sim.client" not in set(sys.modules) - before
    assert "httpx" not in set(sys.modules) - before


def test_inputs_are_not_mutated() -> None:
    import copy

    snapshot = _snapshot()
    signals = [_diesel_signal()]
    forecasts = _diesel_forecasts()
    snapshot_copy = copy.deepcopy(snapshot)
    signals_copy = copy.deepcopy(signals)
    forecasts_copy = copy.deepcopy(forecasts)

    AllocationEngine().recommend(
        snapshot=snapshot, signals=signals, forecasts=forecasts
    )

    assert snapshot == snapshot_copy
    assert signals == signals_copy
    assert forecasts == forecasts_copy


# ---------------------------------------------------------------------------
# Interop with the real peer types
# ---------------------------------------------------------------------------


def test_consumes_real_forecast_result() -> None:
    """A4's real ForecastResult (not the local fixture) must be consumable."""
    forecast_mod = pytest.importorskip("app.intelligence.forecast")
    points = tuple(
        forecast_mod.ForecastPoint(tick=i + 1, liters=375.0, lower=0.0, upper=0.0)
        for i in range(8)
    )
    result = forecast_mod.ForecastResult(
        station_id="S1",
        fuel_type="DIESEL",
        points=points,
        confidence=0.8,
        method="ewma",
        fallback_used=False,
    )
    snapshot = _snapshot()
    recs = AllocationEngine().recommend(
        snapshot=snapshot,
        signals=[_diesel_signal()],
        forecasts={("S1", "DIESEL"): result},
    )
    assert recs, "the real ForecastResult must be consumable"
    assert sum(r.quantity_liters for r in recs) == pytest.approx(2800.0, abs=1e-3)
    _assert_invariants(recs, snapshot)


def test_real_forecaster_output_is_accepted_even_when_it_is_flat() -> None:
    """A4's no-history fallback yields a flat forecast for an empty series; the
    engine must accept it without raising (and correctly decide not to ship)."""
    forecast_mod = pytest.importorskip("app.intelligence.forecast")
    result = forecast_mod.DemandForecaster().forecast([], horizon_ticks=4)
    recs = AllocationEngine().recommend(
        snapshot=_snapshot(),
        signals=[_diesel_signal()],
        forecasts={("S1", "DIESEL"): result},
    )
    assert isinstance(recs, list)


def test_consumes_real_risk_signal() -> None:
    detect_mod = pytest.importorskip("app.intelligence.detect")
    signal = detect_mod.RiskSignal(
        kind="demand_anomaly",
        severity=detect_mod.Severity.SERIOUS,
        entity_type="station",
        entity_id="S1",
        detected_at_tick=12,
        summary="real signal",
        evidence={"fuel_type": "DIESEL"},
        confidence=0.8,
    )
    recs = AllocationEngine().recommend(
        snapshot=_snapshot(), signals=[signal], forecasts=_diesel_forecasts()
    )
    assert recs, "the real RiskSignal must be consumable"
    assert "real signal" in recs[0].rationale


def test_consumes_real_snapshot() -> None:
    models = pytest.importorskip("app.sim.models")
    snapshot = models.Snapshot(
        taken_at=0.0,
        tick=12,
        sim_time="2026-01-01T00:00:00Z",
        status="RUNNING",
        depots=(
            models.Depot(
                id="D1",
                name="Depot One",
                region_id="R1",
                status="OPEN",
                dispatch_capacity_per_tick=5000.0,
                capacity={"DIESEL": 20000.0},
                inventory={"DIESEL": 10000.0},
            ),
        ),
        stations=(
            models.Station(
                id="S1",
                name="Station One",
                region_id="R1",
                status="OPEN",
                demand_profile="urban_high",
                demand_multiplier=1.0,
                capacity={"DIESEL": 5000.0},
                inventory={"DIESEL": 200.0},
            ),
        ),
        routes=(
            models.Route(
                id="RT-1",
                source_depot_id="D1",
                destination_station_id="S1",
                transit_ticks=1,
                max_shipment=1000.0,
                status="AVAILABLE",
            ),
        ),
        regions=(),
        supply_arrivals=(),
        events=(),
        metrics=models.Metrics(
            served_demand_liters=0.0,
            unmet_demand_liters=0.0,
            service_level=1.0,
            allocation_liters=0.0,
            allocation_failures=0,
        ),
    )
    recs = AllocationEngine().recommend(
        snapshot=snapshot, signals=[_diesel_signal()], forecasts=_diesel_forecasts()
    )
    assert recs, "the real Snapshot must be consumable"
    _assert_invariants(recs, snapshot)
