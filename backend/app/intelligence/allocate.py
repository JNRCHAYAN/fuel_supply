"""Allocation decision engine — CONTRACT section 7.3 (workstream A6).

Brief section 7 (Decision Intelligence), section 9 (inspectable recommendations)
and section 11 (fallback policy when the model is unavailable).

This module is a **pure function of its inputs**: no I/O, no clock, no
randomness, no simulator call, no database. Identical inputs always produce
identical outputs — the same recommendations in the same order with the same
ids. That property is tested, because the API layer, the explanation layer and
the decision audit history all depend on it.

Two paths, exactly as the contract requires
-------------------------------------------
1. **Primary — constrained optimisation** (``scipy.optimize.linprog``).
2. **Fallback — transparent priority heuristic** (severity x probability x
   volume), used when the optimiser is unavailable (scipy missing), infeasible,
   or raises. ``AllocationEngine.policy`` reports which of the two actually
   produced the last result, and ``AllocationEngine.last_fallback_reason``
   says why the fallback was taken. This is brief section 11's "ML model
   unavailable -> fallback allocation policy" requirement; it is reachable in
   production and is exercised by a test that forces it.

NEVER EXECUTES ANYTHING (CONTRACT section 0.7, brief section 24)
---------------------------------------------------------------
This engine *recommends*. It never calls the simulator, never submits an
allocation, never mutates its inputs. Submission is a separate, explicit,
operator-initiated call (``POST /api/v1/recommendations/{id}/submit``, owned by
the API workstream), which requires an explicit human confirmation field. The
brief requires human review to be preserved for consequential decisions; that
is why the boundary lives here, structurally, rather than by convention.

======================================================================
OPTIMISATION FORMULATION
======================================================================

Decision variables
    x_i >= 0 for every candidate i = (station s, fuel f, depot d, route r)
    where r is an AVAILABLE route from d to s.
    One variable per (station, fuel, depot, route) — so an optimal solution may
    split one station's shortfall across two depots, which is realistic.

Objective (maximise expected unmet-demand reduction)
    max  sum_i  w_i * x_i
    w_i = severity_i * probability_i * time_i
    The engine solves the equivalent minimisation ``min sum_i -w_i * x_i``
    because ``linprog`` minimises.
      * severity   — 0..1, from the worst ``RiskSignal`` severity concerning the
                     station (info 0.10, warning 0.35, serious 0.70, critical 1.0)
      * probability— 0..1, signal confidence blended with the station's
                     shortfall ratio (see ``_probability``)
      * time_i     — transit discount 1/(1 + transit_ticks), floored at 0.25.
                     A litre that arrives later reduces less of the unmet
                     demand, so a slower route is worth strictly less per litre.
    ``w_i`` is therefore "expected unmet demand removed per litre delivered by
    this route", and the objective is maximising total expected reduction.

Constraints
    (1) demand cap / no over-shipping:
        for each (s, f):   sum_{d,r} x_{s,f,d,r}  <=  need_{s,f}
        where need = forecast demand over the horizon minus station inventory
        (min 0). Shipping more than the shortfall is pointless and would burn
        depot inventory and budget, so it is hard-capped.
    (2) depot inventory (per fuel):
        for each (d, f):   sum_{s,r} x_{s,f,d,r}  <=  inventory_{d,f}
    (3) route capacity:
        for each i:        x_i  <=  max_shipment_{r(i)}
        and only routes with status AVAILABLE become variables at all — a
        DISRUPTED route is never a variable, so it can never be used.
    (4) depot dispatch capacity (per tick):
        for each d:        sum_{s,f,r} x_{s,f,d,r}  <=  dispatch_capacity_per_tick_d
    (5) budget (optional, only when ``budget_liters`` is supplied):
        sum_i x_i  <=  budget_liters
    (6) x_i >= 0.

    Rows (1)-(5) carry no equality constraints, so ``x = 0`` is always feasible:
    an infeasible solve can only come from degenerate input (NaN/negative caps),
    which is sanitised on the way in and additionally defended by the fallback.

The heuristic path applies the *same* caps greedily in priority order (highest
``severity * probability * volume`` first), tracking remaining need, remaining
depot inventory, remaining dispatch capacity and remaining budget. Both paths
therefore satisfy the same invariants by construction — an optimiser that
violated one of them would be worse than the heuristic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, Sequence

try:  # pragma: no cover - trivial import guard, exercised by a test
    from scipy.optimize import linprog
except ImportError:  # pragma: no cover - scipy is a pinned dependency
    linprog = None  # type: ignore[assignment]

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime coupling
    from app.intelligence.detect import RiskSignal
    from app.intelligence.forecast import ForecastResult
    from app.sim.models import Snapshot

__all__ = [
    "Alternative",
    "Recommendation",
    "AllocationEngine",
    "POLICY_OPTIMIZER",
    "POLICY_HEURISTIC",
]


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

POLICY_OPTIMIZER = "optimizer"
POLICY_HEURISTIC = "priority_heuristic"
_HISTORICAL_POLICIES = (POLICY_OPTIMIZER, POLICY_HEURISTIC, "heuristic")

#: Litres below this are numerical noise, not a shipment.
_MIN_SHIPMENT_LITERS = 1e-3
#: Relative tolerance for "this constraint row is tight at the optimum".
_TIGHT_REL_TOL = 1e-6

_SEVERITY_WEIGHTS: dict[str, float] = {
    "info": 0.10,
    "warning": 0.35,
    "serious": 0.70,
    "critical": 1.00,
}
_UNKNOWN_SEVERITY_WEIGHT = 0.30  # CONTRACT 5.2: tolerate unknown enum values

_FUEL_TYPES = ("DIESEL", "PETROL", "OCTANE")

#: Confidence assigned to a station with no forecast available for it.
_NO_FORECAST_CONFIDENCE = 0.40
#: Floor on the transit discount, so a very long route is still worth shipping.
_TRANSIT_DISCOUNT_FLOOR = 0.25
#: Default horizon used only for the transit discount when no forecast exists.
_DEFAULT_HORIZON_TICKS = 8


# --------------------------------------------------------------------------
# Public types (CONTRACT 7.3)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Alternative:
    """One option that was considered for a station/fuel and not taken.

    Not pinned by CONTRACT 7.3 (which only names ``tuple[Alternative, ...]``);
    the fields below are this workstream's definition and are reported as a
    contract gap.
    """

    depot_id: str
    route_id: str
    quantity_liters: float          # what it would have carried
    reason_not_chosen: str          # the constraint or the ranking that lost
    score: float                    # its priority / value, for comparison


@dataclass(frozen=True)
class Recommendation:
    """An inspectable, non-executed allocation proposal (CONTRACT 7.3)."""

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
    simulated: bool = True              # brief section 24 — always True


# --------------------------------------------------------------------------
# Internal candidate
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    """A feasible (station, fuel, depot, route) option for the solver."""

    station_id: str
    fuel_type: str
    depot_id: str
    route_id: str
    transit_ticks: int
    need_liters: float          # station shortfall cap for this (station, fuel)
    max_shipment: float
    depot_inventory: float      # the depot's inventory of *this fuel*
    depot_dispatch: float
    severity: float             # 0..1
    probability: float          # 0..1
    weight: float               # value per litre: severity * probability * time
    priority: float             # severity * probability * volume (heuristic)
    risk_before: float          # 0..1, for expected_impact
    forecast_confidence: float
    signal_confidence: float
    signal_summaries: tuple[str, ...]

    @property
    def group_key(self) -> tuple[str, str]:
        return (self.station_id, self.fuel_type)

    @property
    def depot_fuel_key(self) -> tuple[str, str]:
        return (self.depot_id, self.fuel_type)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _as_str(value: Any) -> str:
    """Enum-or-str to str, without importing the peer's enum."""
    return str(getattr(value, "value", value))


def _severity_weight(severity: Any) -> float:
    return _SEVERITY_WEIGHTS.get(_as_str(severity).lower(), _UNKNOWN_SEVERITY_WEIGHT)


def _finite(value: Any, default: float = 0.0) -> float:
    """Coerce to a finite float; anything unusable becomes ``default``."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(out) or math.isinf(out):
        return default
    return out


def _positive(value: Any) -> float:
    """A capacity/limit: finite and never negative."""
    return max(0.0, _finite(value, 0.0))


def _lookup(mapping: Any, fuel_type: str) -> float:
    """Case-tolerant lookup in a ``{fuel_type: float}`` dict."""
    if not isinstance(mapping, Mapping):
        return 0.0
    if fuel_type in mapping:
        return _positive(mapping[fuel_type])
    upper = fuel_type.upper()
    if upper in mapping:
        return _positive(mapping[upper])
    for key, value in mapping.items():
        if isinstance(key, str) and key.upper() == upper:
            return _positive(value)
    return 0.0


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _tight(actual: float, cap: float) -> bool:
    """True when ``actual`` sits on ``cap`` at the optimum."""
    return actual >= cap - _TIGHT_REL_TOL * max(1.0, abs(cap))


def _floor3(value: float) -> float:
    """Floor to millilitre precision — never rounds *up* past a cap."""
    return math.floor(value * 1000.0) / 1000.0


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------


class AllocationEngine:
    """Constrained-optimisation allocation engine with a priority fallback.

    ``policy`` is the requested policy (``"optimizer"`` by default). After each
    :meth:`recommend` call it holds the policy that **actually produced the
    result** — ``"optimizer"`` or ``"priority_heuristic"`` — as CONTRACT 7.3
    requires. The *results* stay a pure function of the inputs; only this
    reporting attribute is stateful, and it always describes the most recent
    call. ``last_fallback_reason`` explains why a fallback was taken (``None``
    when the optimiser ran).
    """

    def __init__(self, *, policy: str = POLICY_OPTIMIZER) -> None:
        if policy not in _HISTORICAL_POLICIES:
            raise ValueError(
                f"unknown policy {policy!r}; expected one of "
                f"{POLICY_OPTIMIZER!r}, {POLICY_HEURISTIC!r}"
            )
        self._requested_policy = (
            POLICY_HEURISTIC if policy == "heuristic" else policy
        )
        #: Which policy produced the most recent result (CONTRACT 7.3).
        self.policy: str = self._requested_policy
        #: Why the fallback ran; ``None`` when the optimiser produced the result.
        self.last_fallback_reason: str | None = None

    # -- public API ---------------------------------------------------------

    def recommend(
        self,
        *,
        snapshot: Snapshot,
        signals: Sequence[RiskSignal],
        forecasts: Mapping[tuple[str, str], ForecastResult],
        budget_liters: float | None = None,
    ) -> list[Recommendation]:
        """Return ranked allocation recommendations. Never executes anything.

        Empty signals, an empty network, or a network with no shortfall all
        return ``[]`` rather than raising — an already-healthy network is not an
        error condition.
        """
        signals = tuple(signals or ())
        forecasts = forecasts or {}
        self.last_fallback_reason = None

        stations = {str(getattr(s, "id", "")): s for s in (snapshot.stations or ())}
        depots = {str(getattr(d, "id", "")): d for d in (snapshot.depots or ())}
        tick = int(_finite(getattr(snapshot, "tick", 0), 0))

        concern = self._concerned_stations(snapshot, signals)
        if not concern:
            # No signal implicates any station: nothing to recommend. Healthy
            # network -> empty list, by design, not by accident.
            self.policy = self._requested_policy
            return []

        candidates = self._build_candidates(
            snapshot=snapshot,
            depots=depots,
            stations=stations,
            concern=concern,
            forecasts=forecasts,
        )
        if not candidates:
            self.policy = self._requested_policy
            return []

        budget = None if budget_liters is None else _positive(budget_liters)

        allocation: dict[int, float] | None = None
        if self._requested_policy == POLICY_OPTIMIZER:
            allocation, reason = self._solve_optimiser(candidates, budget)
            if allocation is None:
                self.last_fallback_reason = reason
                allocation = self._solve_heuristic(candidates, budget)
                self.policy = POLICY_HEURISTIC
            else:
                self.policy = POLICY_OPTIMIZER
        else:
            self.last_fallback_reason = "policy=priority_heuristic requested"
            allocation = self._solve_heuristic(candidates, budget)
            self.policy = POLICY_HEURISTIC

        return self._build_recommendations(
            candidates=candidates,
            allocation=allocation,
            budget=budget,
            tick=tick,
            snapshot=snapshot,
        )

    # -- signal -> station mapping -----------------------------------------

    def _concerned_stations(
        self,
        snapshot: Snapshot,
        signals: Sequence[RiskSignal],
    ) -> dict[str, list[tuple[RiskSignal, tuple[str, ...]]]]:
        """Map each signal to the stations it implicates.

        station -> [(signal, fuel_types)] where fuel_types is empty when the
        signal is not fuel-specific. A signal that implicates no station (an
        unknown entity id, for instance) is tolerated and ignored, never fatal.
        """
        routes = tuple(snapshot.routes or ())
        depots = {str(getattr(d, "id", "")): d for d in (snapshot.depots or ())}
        stations = tuple(snapshot.stations or ())

        by_station: dict[str, list[tuple[RiskSignal, tuple[str, ...]]]] = {}

        def add(station_id: str, signal: RiskSignal, fuels: tuple[str, ...]) -> None:
            by_station.setdefault(station_id, []).append((signal, fuels))

        for signal in sorted(
            signals,
            key=lambda s: (
                -_severity_weight(getattr(s, "severity", "")),
                -_clamp01(_finite(getattr(s, "confidence", 0.0))),
                str(getattr(s, "entity_type", "")),
                str(getattr(s, "entity_id", "")),
                str(getattr(s, "kind", "")),
            ),
        ):
            entity_type = str(getattr(signal, "entity_type", "")).lower()
            entity_id = str(getattr(signal, "entity_id", ""))
            fuels = self._signal_fuels(signal)

            if entity_type == "station":
                if any(str(getattr(s, "id", "")) == entity_id for s in stations):
                    add(entity_id, signal, fuels)
            elif entity_type == "route":
                for route in routes:
                    if str(getattr(route, "id", "")) == entity_id:
                        add(str(getattr(route, "destination_station_id", "")), signal, fuels)
            elif entity_type == "depot":
                for route in routes:
                    if str(getattr(route, "source_depot_id", "")) == entity_id:
                        add(str(getattr(route, "destination_station_id", "")), signal, fuels)
                depot = depots.get(entity_id)
                if depot is not None:
                    region_id = str(getattr(depot, "region_id", ""))
                    for station in stations:
                        if str(getattr(station, "region_id", "")) == region_id:
                            add(str(getattr(station, "id", "")), signal, fuels)
            elif entity_type == "region":
                for station in stations:
                    if str(getattr(station, "region_id", "")) == entity_id:
                        add(str(getattr(station, "id", "")), signal, fuels)
            # Unknown entity_type: tolerated, ignored (CONTRACT 5.2).

        return {k: v for k, v in by_station.items() if k and v}

    @staticmethod
    def _signal_fuels(signal: RiskSignal) -> tuple[str, ...]:
        """Fuel types a signal is specific to, if its evidence says so."""
        evidence = getattr(signal, "evidence", None)
        if not isinstance(evidence, Mapping):
            return ()
        raw = evidence.get("fuel_type") or evidence.get("fuel_types")
        if isinstance(raw, str):
            values = (raw,)
        elif isinstance(raw, (list, tuple, set)):
            values = tuple(str(v) for v in raw)
        else:
            return ()
        return tuple(v.upper() for v in values if str(v).upper() in _FUEL_TYPES)

    # -- candidate construction --------------------------------------------

    def _build_candidates(
        self,
        *,
        snapshot: Snapshot,
        depots: Mapping[str, Any],
        stations: Mapping[str, Any],
        concern: Mapping[str, Sequence[tuple[RiskSignal, tuple[str, ...]]]],
        forecasts: Mapping[tuple[str, str], ForecastResult],
    ) -> list[_Candidate]:
        routes = tuple(snapshot.routes or ())
        routes_by_destination: dict[str, list[Any]] = {}
        for route in routes:
            # Route availability is checked once, here. A DISRUPTED route never
            # becomes a decision variable, so it can never carry a shipment —
            # this is the single point where CONTRACT 7.3's route rule lives.
            if _as_str(getattr(route, "status", "")).upper() != "AVAILABLE":
                continue
            destination = str(getattr(route, "destination_station_id", ""))
            routes_by_destination.setdefault(destination, []).append(route)

        candidates: list[_Candidate] = []

        for station_id in sorted(concern):
            station = stations.get(station_id)
            if station is None:
                continue
            entries = concern[station_id]
            station_signals = [sig for sig, _ in entries]
            severity = max(
                (_severity_weight(getattr(s, "severity", "")) for s in station_signals),
                default=0.0,
            )
            signal_confidence = max(
                (_clamp01(_finite(getattr(s, "confidence", 0.0))) for s in station_signals),
                default=0.0,
            )
            summaries = tuple(
                sorted({str(getattr(s, "summary", "")).strip() for s in station_signals if getattr(s, "summary", "")})
            )
            fuel_restriction: set[str] = set()
            for _, fuels in entries:
                fuel_restriction.update(fuels)

            station_routes = sorted(
                routes_by_destination.get(station_id, []),
                key=lambda r: (
                    int(_finite(getattr(r, "transit_ticks", 0), 0)),
                    str(getattr(r, "id", "")),
                ),
            )
            if not station_routes:
                # No available route: the station is skipped rather than handed
                # an impossible recommendation.
                continue

            for fuel_type in _FUEL_TYPES:
                if fuel_restriction and fuel_type not in fuel_restriction:
                    continue
                forecast = forecasts.get((station_id, fuel_type))
                need, horizon = self._need_liters(station, fuel_type, forecast)
                if need <= _MIN_SHIPMENT_LITERS:
                    continue  # no shortfall -> already healthy for this fuel

                probability = self._probability(
                    inventory=_lookup(getattr(station, "inventory", None), fuel_type),
                    need=need,
                    forecast=forecast,
                    signal_confidence=signal_confidence,
                )
                risk_before = _clamp01(severity * probability)
                forecast_confidence = (
                    _clamp01(_finite(getattr(forecast, "confidence", 0.0)))
                    if forecast is not None
                    else _NO_FORECAST_CONFIDENCE
                )

                for route in station_routes:
                    depot_id = str(getattr(route, "source_depot_id", ""))
                    depot = depots.get(depot_id)
                    if depot is None:
                        continue
                    inventory = _lookup(getattr(depot, "inventory", None), fuel_type)
                    if inventory <= 0.0:
                        continue  # nothing to ship from here
                    transit = max(0, int(_finite(getattr(route, "transit_ticks", 0), 0)))
                    time_factor = max(
                        _TRANSIT_DISCOUNT_FLOOR, 1.0 / (1.0 + transit)
                    )
                    weight = severity * probability * time_factor
                    candidates.append(
                        _Candidate(
                            station_id=station_id,
                            fuel_type=fuel_type,
                            depot_id=depot_id,
                            route_id=str(getattr(route, "id", "")),
                            transit_ticks=transit,
                            need_liters=need,
                            max_shipment=_positive(getattr(route, "max_shipment", 0.0)),
                            depot_inventory=inventory,
                            depot_dispatch=_positive(
                                getattr(depot, "dispatch_capacity_per_tick", 0.0)
                            ),
                            severity=severity,
                            probability=probability,
                            weight=weight,
                            priority=severity * probability * need,
                            risk_before=risk_before,
                            forecast_confidence=forecast_confidence,
                            signal_confidence=signal_confidence,
                            signal_summaries=summaries,
                        )
                    )

        candidates.sort(
            key=lambda c: (
                c.station_id,
                c.fuel_type,
                -c.weight,
                c.transit_ticks,
                c.depot_id,
                c.route_id,
            )
        )
        return candidates

    @staticmethod
    def _need_liters(
        station: Any,
        fuel_type: str,
        forecast: ForecastResult | None,
    ) -> tuple[float, int]:
        """(litres to ship, horizon ticks) for one station/fuel.

        With a forecast: the shortfall between forecast demand over the horizon
        and the station's own inventory. Without one: refill to tank capacity,
        which is a transparent rule rather than a guess — and it never invents
        demand out of nothing (a full station yields zero).
        """
        inventory = _lookup(getattr(station, "inventory", None), fuel_type)
        if forecast is not None:
            points = tuple(getattr(forecast, "points", ()) or ())
            demand = sum(max(0.0, _finite(getattr(p, "liters", 0.0))) for p in points)
            horizon = max(1, len(points))
            return max(0.0, demand - inventory), horizon
        capacity = _lookup(getattr(station, "capacity", None), fuel_type)
        return max(0.0, capacity - inventory), _DEFAULT_HORIZON_TICKS

    @staticmethod
    def _probability(
        *,
        inventory: float,
        need: float,
        forecast: ForecastResult | None,
        signal_confidence: float,
    ) -> float:
        """0..1 probability that this shortfall actually bites.

        Blends the detection confidence with the shortfall ratio, and pulls the
        result down when the forecast that produced ``need`` is itself weak.
        """
        demand = inventory + need
        shortfall_ratio = need / demand if demand > 0 else 0.0
        blended = 0.5 * _clamp01(signal_confidence) + 0.5 * _clamp01(shortfall_ratio)
        if forecast is not None:
            fc = _clamp01(_finite(getattr(forecast, "confidence", 0.0)))
            blended *= 0.7 + 0.3 * fc
        return _clamp01(blended)

    # -- primary path: constrained optimisation -----------------------------

    def _solve_optimiser(
        self,
        candidates: Sequence[_Candidate],
        budget: float | None,
    ) -> tuple[dict[int, float] | None, str | None]:
        """Solve the LP. Returns (allocation, fallback_reason).

        ``allocation`` is ``None`` when the caller must fall back; the reason
        string is then non-empty. Never raises.
        """
        if linprog is None:
            return None, "optimizer unavailable: scipy.optimize.linprog not importable"

        c = [-cand.weight for cand in candidates]
        a_ub: list[list[float]] = []
        b_ub: list[float] = []
        n = len(candidates)

        # Rows are built as sums over the variables belonging to each key, so the
        # cap for a row is just any member's value (they agree by construction).
        def add_rows(
            key_of: Any,
            cap_of: Any,
        ) -> None:
            rows: dict[Any, list[float]] = {}
            caps: dict[Any, float] = {}
            for i, cand in enumerate(candidates):
                key = key_of(cand)
                rows.setdefault(key, [0.0] * n)[i] = 1.0
                caps.setdefault(key, cap_of(cand))
            for key in sorted(rows):
                a_ub.append(rows[key])
                b_ub.append(caps[key])

        # (1) demand cap per (station, fuel) — never ship more than the shortfall
        add_rows(lambda c_: c_.group_key, lambda c_: c_.need_liters)
        # (2) depot inventory per (depot, fuel)
        add_rows(lambda c_: c_.depot_fuel_key, lambda c_: c_.depot_inventory)
        # (4) depot dispatch capacity per tick
        add_rows(lambda c_: c_.depot_id, lambda c_: c_.depot_dispatch)

        # (5) global budget, when the operator set one
        if budget is not None:
            a_ub.append([1.0] * n)
            b_ub.append(budget)

        # (3) route max_shipment, as per-variable bounds alongside x >= 0
        bounds = [
            (0.0, min(cand.max_shipment, cand.need_liters, cand.depot_inventory))
            for cand in candidates
        ]

        try:
            result = linprog(
                c,
                A_ub=a_ub,
                b_ub=b_ub,
                bounds=bounds,
                method="highs",
            )
        except Exception as exc:  # noqa: BLE001 - any solver failure -> fallback
            return None, f"optimizer raised {type(exc).__name__}: {exc}"

        status = int(_finite(getattr(result, "status", -1), -1))
        if status != 0 or getattr(result, "x", None) is None:
            return None, (
                f"optimizer did not return an optimal solution (status={status}); "
                "falling back to the priority heuristic"
            )

        allocation: dict[int, float] = {}
        for i, value in enumerate(result.x):
            liters = _finite(value, 0.0)
            if liters >= _MIN_SHIPMENT_LITERS:
                allocation[i] = liters
        return allocation, None

    # -- fallback path: transparent priority heuristic ----------------------

    def _solve_heuristic(
        self,
        candidates: Sequence[_Candidate],
        budget: float | None,
    ) -> dict[int, float]:
        """Greedy allocation on severity x probability x volume.

        Transparent by construction: the ranking key is exactly the brief's
        formula, and every cap is applied in the same order the optimiser's
        constraint rows are written.
        """
        order = sorted(
            range(len(candidates)),
            key=lambda i: (
                -candidates[i].priority,
                -candidates[i].weight,
                candidates[i].station_id,
                candidates[i].fuel_type,
                candidates[i].transit_ticks,
                candidates[i].depot_id,
                candidates[i].route_id,
            ),
        )

        remaining_need: dict[tuple[str, str], float] = {}
        for cand in candidates:
            remaining_need[cand.group_key] = cand.need_liters
        remaining_depot_fuel: dict[tuple[str, str], float] = {}
        remaining_dispatch: dict[str, float] = {}
        for cand in candidates:
            remaining_depot_fuel[cand.depot_fuel_key] = min(
                remaining_depot_fuel.get(cand.depot_fuel_key, cand.depot_inventory),
                cand.depot_inventory,
            )
            remaining_dispatch[cand.depot_id] = min(
                remaining_dispatch.get(cand.depot_id, cand.depot_dispatch),
                cand.depot_dispatch,
            )

        remaining_budget = budget
        allocation: dict[int, float] = {}

        for i in order:
            cand = candidates[i]
            caps = [
                cand.max_shipment,
                remaining_need.get(cand.group_key, 0.0),
                remaining_depot_fuel.get(cand.depot_fuel_key, 0.0),
                remaining_dispatch.get(cand.depot_id, 0.0),
            ]
            if remaining_budget is not None:
                caps.append(remaining_budget)
            liters = min(caps)
            if liters < _MIN_SHIPMENT_LITERS:
                continue
            allocation[i] = liters
            remaining_need[cand.group_key] = (
                remaining_need.get(cand.group_key, 0.0) - liters
            )
            remaining_depot_fuel[cand.depot_fuel_key] = (
                remaining_depot_fuel.get(cand.depot_fuel_key, 0.0) - liters
            )
            remaining_dispatch[cand.depot_id] = (
                remaining_dispatch.get(cand.depot_id, 0.0) - liters
            )
            if remaining_budget is not None:
                remaining_budget -= liters

        return allocation

    # -- post-processing: inspectable recommendations -----------------------

    def _build_recommendations(
        self,
        *,
        candidates: Sequence[_Candidate],
        allocation: Mapping[int, float],
        budget: float | None,
        tick: int,
        snapshot: Snapshot,
    ) -> list[Recommendation]:
        if not allocation:
            return []

        # Aggregate residual state for both paths, so the "why not" text and the
        # binding constraints are computed identically for optimiser and
        # heuristic results.
        group_used: dict[tuple[str, str], float] = {}
        depot_fuel_used: dict[tuple[str, str], float] = {}
        depot_used: dict[str, float] = {}
        for i, liters in allocation.items():
            cand = candidates[i]
            group_used[cand.group_key] = group_used.get(cand.group_key, 0.0) + liters
            depot_fuel_used[cand.depot_fuel_key] = (
                depot_fuel_used.get(cand.depot_fuel_key, 0.0) + liters
            )
            depot_used[cand.depot_id] = depot_used.get(cand.depot_id, 0.0) + liters
        total_used = sum(allocation.values())

        ranked: list[tuple[tuple[float, float, str], Recommendation]] = []
        for i in sorted(allocation):
            cand = candidates[i]
            quantity = _floor3(allocation[i])
            if quantity < _MIN_SHIPMENT_LITERS:
                continue

            group_total = group_used.get(cand.group_key, 0.0)
            depot_fuel_total = depot_fuel_used.get(cand.depot_fuel_key, 0.0)
            depot_total = depot_used.get(cand.depot_id, 0.0)
            # Coverage is measured against the *station/fuel* shortfall, so every
            # recommendation answering the same shortfall reports the same
            # residual risk and the same residual shortfall. Two split legs that
            # disagreed about "risk after" would be worse than useless to an
            # operator deciding whether the plan is sufficient.
            coverage = (
                _clamp01(group_total / cand.need_liters) if cand.need_liters > 0 else 0.0
            )

            alternatives = self._alternatives(
                chosen_index=i,
                candidates=candidates,
                allocation=allocation,
                group_used=group_used,
                depot_fuel_used=depot_fuel_used,
                depot_used=depot_used,
                total_used=total_used,
                budget=budget,
                snapshot=snapshot,
            )
            constraints = self._binding_constraints(
                cand=cand,
                quantity=quantity,
                group_total=group_total,
                depot_fuel_total=depot_fuel_total,
                depot_total=depot_total,
                total_used=total_used,
                budget=budget,
            )
            confidence = self._confidence(cand)
            risk_after = _clamp01(cand.risk_before * (1.0 - coverage * confidence))
            # This leg's own marginal contribution, for "what does this shipment
            # buy me" — kept separate so the group figures stay consistent.
            leg_coverage = (
                _clamp01(quantity / cand.need_liters) if cand.need_liters > 0 else 0.0
            )
            leg_risk_after = _clamp01(
                cand.risk_before * (1.0 - leg_coverage * confidence)
            )

            ranked.append(
                (
                    # Rank by the risk it addresses, then by how much it actually
                    # buys (litres x value per litre), then by id for determinism.
                    (
                        -round(cand.risk_before, 6),
                        -round(cand.weight * quantity, 6),
                        self._recommendation_id(tick, cand),
                    ),
                    Recommendation(
                        id=self._recommendation_id(tick, cand),
                        station_id=cand.station_id,
                        depot_id=cand.depot_id,
                        route_id=cand.route_id,
                        fuel_type=cand.fuel_type,
                        quantity_liters=quantity,
                        rationale=self._rationale(cand, quantity, coverage),
                        constraints=constraints,
                        expected_impact={
                            "risk_before": round(cand.risk_before, 4),
                            "risk_after": round(risk_after, 4),
                            "shortfall_before_liters": round(cand.need_liters, 3),
                            "shortfall_after_liters": round(
                                max(0.0, cand.need_liters - group_total), 3
                            ),
                            "coverage_ratio": round(coverage, 4),
                            "risk_reduction_from_this_recommendation": round(
                                cand.risk_before - leg_risk_after, 4
                            ),
                        },
                        alternatives=alternatives,
                        confidence=round(confidence, 4),
                        simulated=True,
                    ),
                )
            )

        # Highest risk first, then the largest expected reduction, then id — a
        # total order, so the output is deterministic.
        ranked.sort(key=lambda item: item[0])
        return [rec for _, rec in ranked]

    @staticmethod
    def _recommendation_id(tick: int, cand: _Candidate) -> str:
        """Stable within a tick: depends only on tick and the decision identity.

        Deliberately excludes the quantity, so re-running the engine on the same
        tick yields the same ids even if a number shifts slightly.
        """
        return (
            f"rec-{tick}-{cand.station_id}-{cand.fuel_type}-"
            f"{cand.depot_id}-{cand.route_id}"
        )

    def _alternatives(
        self,
        *,
        chosen_index: int,
        candidates: Sequence[_Candidate],
        allocation: Mapping[int, float],
        group_used: Mapping[tuple[str, str], float],
        depot_fuel_used: Mapping[tuple[str, str], float],
        depot_used: Mapping[str, float],
        total_used: float,
        budget: float | None,
        snapshot: Snapshot,
    ) -> tuple[Alternative, ...]:
        """What else was considered for this station/fuel, and why not."""
        chosen = candidates[chosen_index]
        out: list[Alternative] = []

        for j, cand in enumerate(candidates):
            if j == chosen_index or cand.group_key != chosen.group_key:
                continue
            allocated = allocation.get(j, 0.0)
            if allocated >= _MIN_SHIPMENT_LITERS:
                # Also carrying part of the same shortfall — not an alternative
                # so much as a split shipment; still worth showing the operator.
                out.append(
                    Alternative(
                        depot_id=cand.depot_id,
                        route_id=cand.route_id,
                        quantity_liters=_floor3(allocated),
                        reason_not_chosen=(
                            "also allocated by the solver to cover the remainder "
                            "of the same shortfall"
                        ),
                        score=round(cand.priority, 4),
                    )
                )
                continue

            residual = min(
                cand.max_shipment,
                max(0.0, cand.need_liters - group_used.get(cand.group_key, 0.0)),
                max(0.0, cand.depot_inventory - depot_fuel_used.get(cand.depot_fuel_key, 0.0)),
                max(0.0, cand.depot_dispatch - depot_used.get(cand.depot_id, 0.0)),
                cand.need_liters,
                *([max(0.0, budget - total_used)] if budget is not None else []),
            )
            out.append(
                Alternative(
                    depot_id=cand.depot_id,
                    route_id=cand.route_id,
                    quantity_liters=_floor3(residual),
                    reason_not_chosen=self._rejection_reason(
                        cand=cand,
                        chosen=chosen,
                        residual=residual,
                        group_used=group_used.get(cand.group_key, 0.0),
                        depot_fuel_used=depot_fuel_used.get(cand.depot_fuel_key, 0.0),
                        depot_used=depot_used.get(cand.depot_id, 0.0),
                        total_used=total_used,
                        budget=budget,
                    ),
                    score=round(cand.priority, 4),
                )
            )

        # Disrupted routes are shown as considered-and-rejected: they are the
        # most common reason a station has fewer options than it looks like.
        for route in sorted(
            (r for r in (snapshot.routes or ())),
            key=lambda r: str(getattr(r, "id", "")),
        ):
            if str(getattr(route, "destination_station_id", "")) != chosen.station_id:
                continue
            if _as_str(getattr(route, "status", "")).upper() == "AVAILABLE":
                continue
            out.append(
                Alternative(
                    depot_id=str(getattr(route, "source_depot_id", "")),
                    route_id=str(getattr(route, "id", "")),
                    quantity_liters=0.0,
                    reason_not_chosen=(
                        f"route status is {_as_str(getattr(route, 'status', '')).upper()}, "
                        "so it cannot carry a shipment (CONTRACT 7.3)"
                    ),
                    score=0.0,
                )
            )

        out.sort(key=lambda a: (-a.score, a.depot_id, a.route_id))
        return tuple(out)

    @staticmethod
    def _rejection_reason(
        *,
        cand: _Candidate,
        chosen: _Candidate,
        residual: float,
        group_used: float,
        depot_fuel_used: float,
        depot_used: float,
        total_used: float,
        budget: float | None,
    ) -> str:
        if cand.max_shipment < _MIN_SHIPMENT_LITERS:
            return "route max_shipment is 0 L — the route cannot carry fuel"
        if group_used >= cand.need_liters - _MIN_SHIPMENT_LITERS:
            return "the station's shortfall was already covered by a higher-ranked option"
        if depot_fuel_used >= cand.depot_inventory - _MIN_SHIPMENT_LITERS:
            return (
                f"the depot's {cand.fuel_type} inventory was fully committed to "
                "higher-priority stations"
            )
        if depot_used >= cand.depot_dispatch - _MIN_SHIPMENT_LITERS:
            return (
                "the depot's dispatch_capacity_per_tick was fully committed this tick"
            )
        if budget is not None and total_used >= budget - _MIN_SHIPMENT_LITERS:
            return "the budget cap was reached before this option was considered"
        if residual < _MIN_SHIPMENT_LITERS:
            return "no residual capacity remained under the binding constraints"
        return (
            f"lower expected unmet-demand reduction per litre "
            f"({cand.weight:.3f} vs {chosen.weight:.3f})"
            + (
                f" and longer transit ({cand.transit_ticks} vs {chosen.transit_ticks} ticks)"
                if cand.transit_ticks != chosen.transit_ticks
                else ""
            )
        )

    @staticmethod
    def _binding_constraints(
        *,
        cand: _Candidate,
        quantity: float,
        group_total: float,
        depot_fuel_total: float,
        depot_total: float,
        total_used: float,
        budget: float | None,
    ) -> tuple[str, ...]:
        """The limits that actually bound *this* choice — computed from the
        solved quantities, not a generic list."""
        notes: list[str] = []
        if _tight(quantity, cand.max_shipment) and cand.max_shipment > 0:
            notes.append(
                f"route max_shipment={cand.max_shipment:.1f} L caps this shipment"
            )
        if _tight(group_total, cand.need_liters):
            notes.append(
                f"station shortfall of {cand.need_liters:.1f} L is fully covered "
                "— there is no further demand to serve"
            )
        if _tight(depot_fuel_total, cand.depot_inventory):
            notes.append(
                f"source depot {cand.fuel_type} inventory ({cand.depot_inventory:.1f} L) "
                "is fully committed"
            )
        if _tight(depot_total, cand.depot_dispatch):
            notes.append(
                f"depot dispatch_capacity_per_tick ({cand.depot_dispatch:.1f} L/tick) "
                "is fully committed"
            )
        if budget is not None and _tight(total_used, budget):
            notes.append(f"total budget_liters={budget:.1f} L is fully committed")
        if not notes:
            notes.append(
                "no external limit bound this quantity — it was accepted in full"
            )
        return tuple(notes)

    def _confidence(self, cand: _Candidate) -> float:
        """Deterministic 0..1 confidence, weakened by weak evidence and by the
        fallback path, and never reported as certainty."""
        value = (
            0.30
            + 0.35 * cand.forecast_confidence
            + 0.20 * cand.signal_confidence
            + 0.10 * cand.severity
        )
        if self.policy == POLICY_OPTIMIZER:
            value += 0.05
        else:
            value -= 0.05
        return _clamp01(value)

    @staticmethod
    def _rationale(cand: _Candidate, quantity: float, coverage: float) -> str:
        return (
            f"Ship {quantity:.1f} L of {cand.fuel_type} from depot {cand.depot_id} "
            f"to station {cand.station_id} via route {cand.route_id} "
            f"(transit {cand.transit_ticks} tick(s)); covers {coverage * 100:.1f}% of "
            f"the forecast shortfall of {cand.need_liters:.1f} L. "
            f"Driven by risk severity {cand.severity:.2f} and probability "
            f"{cand.probability:.2f}"
            + (
                ": " + "; ".join(cand.signal_summaries)
                if cand.signal_summaries
                else " (no detection signal implicated this station)"
            )
            + ". Recommendation only — not executed."
        )
