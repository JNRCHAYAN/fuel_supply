"""Stockout and shortage intelligence (brief section 7, CONTRACT.md 7.4).

What this adds over :mod:`app.intelligence.forecast`
----------------------------------------------------
``DemandForecaster.stockout_risk`` answers "how *likely* is exhaustion, as a
probability?". This module answers a different, more operational question:
*given what is on hand, what is already on its way, and what will be consumed,
where does inventory actually land at the end of the horizon, and on which tick
does it cross zero?* One is a probability; the other is a projection an operator
can audit. ``RiskLevel`` (a band) is deliberately not merged with
``StockoutRisk`` (a probability) -- conflating them would make the console
unable to show both.

The arithmetic
--------------
The projection is a tick-by-tick walk, not a closed-form subtraction::

    inventory_0 = current_inventory
    for t in 1..horizon:
        inflow_t    = sum of incoming allocations whose arrival_tick == t
        demand_t    = forecast.points[t-1].liters        (>= 0)
        inventory_t = inventory_{t-1} + inflow_t - demand_t

Summing the recurrence telescopes to
``projected_inventory == current_inventory + incoming_supply - expected_demand``,
which is the formula an operator would write down and can check by hand. The
walk exists only because that formula cannot say *when* the crossing happens,
and "when" is the number an operator acts on.

Demand is taken from :class:`app.intelligence.forecast.DemandForecaster`. This
module never re-derives it: two estimators of the same quantity is two answers
to one question, and the console would have no way to say which was displayed.

Degradation
-----------
Everything here is defensive by construction. Non-finite or unparseable inputs
are coerced (inventory toward the cautious reading, demand and supply toward
zero), a missing forecast horizon yields zero demand beyond the points actually
supplied, and the risk level then falls out of the arithmetic rather than being
special-cased. Nothing raises, nothing returns NaN, and identical arguments
always produce an equal result.

Purity
------
No I/O, no clock, no randomness, no global. ``assess`` is a pure function of its
arguments.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "RiskLevel",
    "LEVEL_ORDER",
    "RiskThresholds",
    "IncomingSupply",
    "StockoutAssessment",
    "StockoutAnalyzer",
]


_EPS = 1e-9


# --------------------------------------------------------------------------- #
# Risk levels
# --------------------------------------------------------------------------- #


class RiskLevel(str, Enum):
    """Ordered severity band. ``str``-mixin so it serialises as its own name.

    ``LOW`` is the absence of bad news; ``CRITICAL`` means the station is dry now
    or will be within ``critical_ticks``. The ordering is meaningful: ``assess``
    only ever raises a level, never lowers it, and ``LEVEL_ORDER`` is what the
    summary counts are built from.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


#: Ascending severity. Used for summary counts and by callers that need to sort
#: most-severe-first without re-deriving the order from the enum's declaration.
LEVEL_ORDER: tuple[RiskLevel, ...] = (
    RiskLevel.LOW,
    RiskLevel.MEDIUM,
    RiskLevel.HIGH,
    RiskLevel.CRITICAL,
)


# --------------------------------------------------------------------------- #
# Configuration and inputs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RiskThresholds:
    """The configurable bands. Defaults mirror ``app.config.Settings``.

    ``safety_stock_fraction`` is a fraction of the *horizon's* expected demand,
    not of tank capacity: a slow-moving station and a fast-moving one should not
    be held to the same absolute buffer. Expressing it relative to demand is what
    makes one number work across every (station, fuel) pair.
    """

    critical_ticks: float = 1.0
    high_ticks: float = 3.0
    safety_stock_fraction: float = 0.25
    horizon_ticks: int = 8

    def __post_init__(self) -> None:
        # A negative horizon is meaningless and a fractional tick value below
        # zero would invert the bands, so both are clamped rather than honoured
        # into a degenerate comparison. The horizon goes through ``_tick`` for
        # the same reason every other tick does: ``int()`` raises
        # ``OverflowError`` on an infinite float, and a constructor that can
        # raise would break the never-raises contract at import of a caller
        # rather than inside ``assess``.
        object.__setattr__(self, "horizon_ticks", max(0, _tick(self.horizon_ticks)))
        object.__setattr__(self, "critical_ticks", max(0.0, _finite(self.critical_ticks, 0.0)))
        object.__setattr__(self, "high_ticks", max(0.0, _finite(self.high_ticks, 0.0)))
        object.__setattr__(
            self,
            "safety_stock_fraction",
            max(0.0, _finite(self.safety_stock_fraction, 0.0)),
        )


@dataclass(frozen=True, slots=True)
class IncomingSupply:
    """One consignment already on its way *to the station*.

    Only in-transit allocations qualify. Depot-directed ``SupplyArrival`` rows
    are not modelled here on purpose: a delivery to a depot is not inbound to a
    station until somebody dispatches it, and pretending otherwise would let the
    console report a station as covered when nothing is actually moving.
    """

    quantity: float
    arrival_tick: int
    allocation_id: str = ""
    source_depot_id: str = ""


@dataclass(frozen=True, slots=True)
class StockoutAssessment:
    """One (station, fuel) projection. Every term of the arithmetic is present.

    The fields are the explanation: ``projected_inventory`` is exactly
    ``current_inventory + incoming_supply - expected_demand``, so an operator can
    verify the conclusion without trusting the code. ``basis`` restates it in one
    sentence together with the threshold that decided the level.
    """

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
    incoming_sources: tuple[IncomingSupply, ...] = ()
    simulated: bool = True


# --------------------------------------------------------------------------- #
# Numeric helpers
# --------------------------------------------------------------------------- #


def _finite(value: Any, default: float = 0.0) -> float:
    """Coerce to a finite float, else ``default``. Never raises, never NaN."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _inventory(value: Any) -> float:
    """Normalise on-hand stock. Mirrors ``DemandForecaster._coerce_inventory``.

    ``+inf`` is preserved as unbounded stock rather than coerced to 0: turning
    "unlimited" into "empty" is the worst available direction to be wrong in.
    Everything else non-finite degrades to 0.0, the cautious reading.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isinf(number) and number > 0.0:
        return math.inf
    if not math.isfinite(number):
        return 0.0
    return number


def _tick(value: Any) -> int:
    """A tick index; anything that is not a finite number reads as tick 0.

    ``int()`` raises ``OverflowError`` -- not ``ValueError`` -- for an infinite
    float, so catching only the latter would let a stray ``inf`` escape and
    break the never-raises contract this module is held to. An ``int`` is
    returned untouched so an exact tick never round-trips through a float.
    """
    if isinstance(value, int):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not math.isfinite(number):
        return 0
    return int(number)


def _liters(value: Any) -> float:
    """A demand or supply quantity. Negative is meaningless, so it floors at 0."""
    return max(0.0, _finite(value, 0.0))


def _fmt(value: float) -> str:
    if not math.isfinite(value):
        return "unbounded"
    return f"{value:,.0f}"


def _fmt_ticks(value: float) -> str:
    if abs(value - round(value)) < 0.05:
        return f"{int(round(value))}"
    return f"{value:.1f}"


# --------------------------------------------------------------------------- #
# The analyser
# --------------------------------------------------------------------------- #


class StockoutAnalyzer:
    """Deterministic inventory projection and risk banding.

    Pure: identical arguments always produce an equal result. No I/O, no clock,
    no randomness, no state carried between calls.
    """

    def __init__(self, *, thresholds: RiskThresholds | None = None) -> None:
        self._thresholds = thresholds if thresholds is not None else RiskThresholds()

    @property
    def thresholds(self) -> RiskThresholds:
        return self._thresholds

    # -- public API -------------------------------------------------------- #

    def assess(
        self,
        *,
        station_id: str,
        fuel_type: str,
        current_inventory: float,
        forecast: Any,
        incoming: Sequence[IncomingSupply] = (),
        current_tick: int = 0,
    ) -> StockoutAssessment:
        """Project one (station, fuel) series over the configured horizon.

        Never raises and never returns NaN, including for an empty or malformed
        ``forecast``, negative or non-numeric inventory, and arrivals outside the
        horizon.
        """
        thresholds = self._thresholds
        horizon = thresholds.horizon_ticks
        tick_now = _tick(current_tick)

        inventory_now = _inventory(current_inventory)
        demand_by_tick, demand_method, demand_confidence, supplied_points = (
            self._demand_by_tick(forecast, horizon)
        )
        inflow_by_tick, sources = self._inflow_by_tick(incoming, horizon)

        walk = self._walk(
            inventory_now=inventory_now,
            demand_by_tick=demand_by_tick,
            inflow_by_tick=inflow_by_tick,
            horizon=horizon,
        )

        expected_demand = sum(demand_by_tick.get(t, 0.0) for t in range(1, horizon + 1))
        incoming_supply = sum(inflow_by_tick.get(t, 0.0) for t in range(1, horizon + 1))
        projected = inventory_now + incoming_supply - expected_demand

        shortage = max(0.0, -projected)
        surplus = max(0.0, projected)

        ticks_until, stockout_tick = self._crossing(walk, tick_now)

        risk = self._level(
            inventory_now=inventory_now,
            ticks_until=ticks_until,
            shortage=shortage,
            projected=projected,
            expected_demand=expected_demand,
        )

        basis = self._basis(
            station_id=station_id,
            fuel_type=fuel_type,
            inventory_now=inventory_now,
            expected_demand=expected_demand,
            incoming_supply=incoming_supply,
            projected=projected,
            ticks_until=ticks_until,
            stockout_tick=stockout_tick,
            risk=risk,
            demand_method=demand_method,
            supplied_points=supplied_points,
            horizon=horizon,
            safety_stock=thresholds.safety_stock_fraction * expected_demand,
        )

        return StockoutAssessment(
            station_id=str(station_id or ""),
            fuel_type=str(fuel_type or ""),
            current_inventory=inventory_now,
            expected_demand=expected_demand,
            incoming_supply=incoming_supply,
            projected_inventory=projected,
            shortage_amount=shortage,
            surplus_amount=surplus,
            stockout_tick=stockout_tick,
            ticks_until_stockout=ticks_until,
            risk=risk,
            basis=basis,
            horizon_ticks=horizon,
            demand_method=demand_method,
            demand_confidence=demand_confidence,
            incoming_sources=sources,
        )

    def assess_many(
        self,
        *,
        rows: Iterable[Mapping[str, Any]],
        current_tick: int = 0,
    ) -> list[StockoutAssessment]:
        """Assess a batch of ``(station, fuel)`` rows.

        Each row is a mapping with ``station_id``, ``fuel_type``,
        ``current_inventory``, ``forecast`` and optionally ``incoming``. This is
        the shape the API layer builds, and existing so the route does not have
        to unpack keywords at the call site.

        Output is sorted most severe first, then by station and fuel, so the
        console's most urgent row is the first one it renders and the order does
        not depend on the iteration order of whatever produced the rows.
        """
        order = {level: index for index, level in enumerate(LEVEL_ORDER)}
        results = [
            self.assess(
                station_id=str(row.get("station_id") or ""),
                fuel_type=str(row.get("fuel_type") or ""),
                current_inventory=row.get("current_inventory", 0.0),
                forecast=row.get("forecast"),
                incoming=tuple(row.get("incoming") or ()),
                current_tick=current_tick,
            )
            for row in rows or ()
        ]
        results.sort(
            key=lambda a: (-order.get(a.risk, 0), a.station_id, a.fuel_type)
        )
        return results

    # -- input normalisation ----------------------------------------------- #

    @staticmethod
    def _demand_by_tick(
        forecast: Any, horizon: int
    ) -> tuple[dict[int, float], str, float, int]:
        """``{tick_offset: liters}`` from a forecast, plus how it was produced.

        A forecast shorter than the horizon is not padded with a guess: the ticks
        it does not cover carry zero demand, which understates rather than
        invents consumption, and ``supplied_points`` lets ``basis`` say so.
        """
        points = tuple(getattr(forecast, "points", ()) or ())
        method = str(getattr(forecast, "method", "") or "unknown")
        confidence = _finite(getattr(forecast, "confidence", 0.0), 0.0)
        confidence = max(0.0, min(1.0, confidence))

        by_tick: dict[int, float] = {}
        for index, point in enumerate(points[:horizon]):
            by_tick[index + 1] = _liters(getattr(point, "liters", 0.0))
        return by_tick, method, confidence, min(len(points), horizon)

    @staticmethod
    def _inflow_by_tick(
        incoming: Sequence[IncomingSupply], horizon: int
    ) -> tuple[dict[int, float], tuple[IncomingSupply, ...]]:
        """``{tick_offset: liters}`` for consignments landing inside the horizon.

        An arrival beyond the horizon is *dropped*, not folded into the end
        state: it cannot prevent a stockout that happens before it lands, and
        counting it would report a station as covered while it is dry.
        """
        by_tick: dict[int, float] = {}
        kept: list[IncomingSupply] = []
        for item in incoming or ():
            quantity = _liters(getattr(item, "quantity", 0.0))
            arrival = _tick(getattr(item, "arrival_tick", 0))
            if 1 <= arrival <= horizon and quantity > 0.0:
                by_tick[arrival] = by_tick.get(arrival, 0.0) + quantity
                kept.append(item)
        return by_tick, tuple(kept)

    @staticmethod
    def _walk(
        *,
        inventory_now: float,
        demand_by_tick: Mapping[int, float],
        inflow_by_tick: Mapping[int, float],
        horizon: int,
    ) -> list[tuple[int, float]]:
        """``[(tick_offset, inventory_after)]`` for each step of the horizon.

        Offset 0 is the starting state, so ``walk[0][1] == inventory_now`` and the
        list is the whole projection path -- which is what makes the arithmetic
        auditable rather than a single opaque endpoint.
        """
        walk: list[tuple[int, float]] = [(0, inventory_now)]
        level = inventory_now
        for tick in range(1, horizon + 1):
            level = level + inflow_by_tick.get(tick, 0.0) - demand_by_tick.get(tick, 0.0)
            walk.append((tick, level))
        return walk

    # -- crossing and banding ---------------------------------------------- #

    @staticmethod
    def _crossing(
        walk: Sequence[tuple[int, float]], current_tick: int
    ) -> tuple[float | None, int | None]:
        """Ticks until exhaustion and the absolute tick it lands on.

        Already-empty inventory is a fact, not a projection, so it short-circuits
        to ``0.0``. Otherwise the fraction of the crossing tick is interpolated
        from the two surrounding levels, so the answer is not quantised to whole
        ticks -- a station with 30 L left against a 100 L tick is not "one tick
        away" in the same sense as one with 99 L left.
        """
        if not walk:
            return None, None
        start = walk[0][1]
        if math.isfinite(start) and start <= 0.0:
            return 0.0, current_tick

        for index in range(1, len(walk)):
            _, level = walk[index]
            if math.isfinite(level) and level <= 0.0:
                previous = walk[index - 1][1]
                if not math.isfinite(previous) or not math.isfinite(level):
                    # An unbounded starting balance cannot cross; only a finite
                    # pair can be interpolated between.
                    return float(index), current_tick + index
                drawn = previous - level
                fraction = previous / drawn if drawn > _EPS else 1.0
                fraction = max(0.0, min(1.0, fraction))
                ticks = (index - 1) + fraction
                return ticks, current_tick + int(math.ceil(ticks))
        return None, None

    def _level(
        self,
        *,
        inventory_now: float,
        ticks_until: float | None,
        shortage: float,
        projected: float,
        expected_demand: float,
    ) -> RiskLevel:
        """Band the projection. Ordered checks; the first match wins.

        Because the checks run most severe first, a projected stockout can never
        be reported as LOW -- the bands are nested rather than independent, which
        is the property an operator relies on when they triage top-down.
        """
        thresholds = self._thresholds

        if math.isfinite(inventory_now) and inventory_now <= 0.0:
            return RiskLevel.CRITICAL
        if ticks_until is not None:
            if ticks_until <= thresholds.critical_ticks:
                return RiskLevel.CRITICAL
            if ticks_until <= thresholds.high_ticks:
                return RiskLevel.HIGH
            # A crossing beyond the high band is still a crossing: it stays at
            # least MEDIUM even when the end-of-horizon balance looks healthy
            # because a later arrival refilled it.
            return RiskLevel.MEDIUM

        safety_stock = thresholds.safety_stock_fraction * expected_demand
        if shortage > 0.0 or projected < safety_stock:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    # -- explanation -------------------------------------------------------- #

    @staticmethod
    def _basis(
        *,
        station_id: str,
        fuel_type: str,
        inventory_now: float,
        expected_demand: float,
        incoming_supply: float,
        projected: float,
        ticks_until: float | None,
        stockout_tick: int | None,
        risk: RiskLevel,
        demand_method: str,
        supplied_points: int,
        horizon: int,
        safety_stock: float,
    ) -> str:
        """One operator-facing sentence carrying every number behind the band."""
        subject = (
            f"{fuel_type} at {station_id}"
            if (fuel_type and station_id)
            else (fuel_type or station_id or "This station")
        )

        if ticks_until is None:
            timing = (
                "no depletion is projected inside the horizon"
            )
        elif stockout_tick is None:
            timing = f"projected dry in about {_fmt_ticks(ticks_until)} ticks"
        else:
            timing = (
                f"projected dry in about {_fmt_ticks(ticks_until)} ticks "
                f"(tick {stockout_tick})"
            )

        clauses = [
            f"{subject}: {_fmt(inventory_now)} L on hand + {_fmt(incoming_supply)} L "
            f"incoming - {_fmt(expected_demand)} L expected demand over {horizon} ticks "
            f"= {_fmt(projected)} L projected",
            f"stockout {timing}",
            f"risk {risk.value} against safety stock {_fmt(safety_stock)} L, "
            f"demand model={demand_method}",
        ]
        if supplied_points < horizon:
            clauses.append(
                f"the forecast supplied {supplied_points} of {horizon} ticks, so the "
                f"remaining ticks carry no projected demand"
            )
        return "; ".join(clauses) + "."
