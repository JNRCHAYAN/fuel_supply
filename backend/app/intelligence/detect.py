"""A5 — anomaly and risk detection (brief §6 alerts, §7 Detection list).

Implements the ``AnomalyDetector`` contract from ``backend/CONTRACT.md`` §7.2.

What this module detects
------------------------
Brief §7 asks for four detection families; they map onto five signal kinds:

``demand_anomaly``
    A station's per-tick demand for one fuel has departed from its own robust
    baseline.
``inventory_drop``
    A depot's or station's inventory for one fuel is abnormally low, measured
    both against its own capacity and against the per-tick demand it has to
    serve (i.e. "ticks of cover").
``route_bottleneck``
    A route is ``DISRUPTED``, or its ``max_shipment`` cannot cover the
    per-tick demand of the station it feeds.
``regional_disruption``
    A region contains multiple affected entities at once (outages,
    constrained depots/stations, disrupted routes, active crisis events).
``supply_shortfall``
    A supply arrival is overdue/delayed against its ``planned_tick``, or its
    quantity leaves the depot short of what its region needs.

Design rules this module holds to
---------------------------------
**Pure function of its inputs.**  No I/O, no network, no database, no clock,
no randomness, no module-level mutable state.  ``detect()`` reads only its two
arguments.  Identical input therefore yields byte-identical output; the import
whitelist is enforced by a test.

**Robust z-score (median + MAD), never mean + stdev.**  With mean + stdev a
single large spike inflates the scale and hides every later anomaly.  See
``_robust_location_scale`` and the module's spike-vs-anomaly test.

**Severity is derived from the numbers, never hard-coded per rule.**  Every
rule computes a dimensionless ``score`` in ``[0, 1]`` from its own measured
magnitude; ``_severity_from_score`` maps that score onto the four bands once,
for every rule.  A 3-sigma demand anomaly and a 9-sigma demand anomaly are
therefore different severities because the numbers differ, not because two
``if`` branches exist.

**Degenerate input never raises and never divides by zero.**  Empty input
returns ``[]``; a single-point series has no spread and yields nothing; an
all-identical series has MAD 0 and falls back through mean-absolute-deviation
to a clamped z rather than an infinity.

Structural consumption of ``Snapshot``
--------------------------------------
The detector reads attributes off the snapshot and the history points.  It
imports ``Snapshot``/``DemandPoint`` under ``TYPE_CHECKING`` only, so the
names are checked by type checkers while the runtime stays decoupled from
``app.sim.models`` (which A2 owns).  That is deliberate: brief §10 warns that
the organisers may introduce model changes mid-event, and a detector that
fails to import because a peer added a field would take the whole risk
endpoint down.  ``test_detect.py`` pins the actual attribute names this module
depends on against A2's real dataclasses.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime coupling
    from app.sim.models import DemandPoint, Snapshot

__all__ = ["Severity", "RiskSignal", "AnomalyDetector"]


# --------------------------------------------------------------------------
# Numeric constants
# --------------------------------------------------------------------------

#: Robust z-scores are clamped here.  A perfectly flat history has a scale of
#: exactly 0 and any deviation is formally infinite; clamping keeps the value
#: finite and JSON-serialisable while still ranking as maximally severe.
_Z_CLAMP = 12.0

#: Everything below this is treated as a zero scale (exact-constant series).
_EPSILON = 1e-9

#: MAD -> sigma for a normal distribution.
_MAD_TO_SIGMA = 1.4826

#: Mean absolute deviation -> sigma for a normal distribution (sqrt(pi/2)).
_MEAN_ABS_DEV_TO_SIGMA = 1.2533141373155003

#: Trailing window used when estimating a "liters per tick" demand rate.
_RATE_WINDOW = 12

#: Fuel types the simulator is known to carry.  Unknown fuel types arriving
#: from the simulator are still handled; this is only used to iterate the
#: union of capacity/inventory keys deterministically.
_KNOWN_FUELS: tuple[str, ...] = ("DIESEL", "PETROL", "OCTANE")

#: Instance statuses that count as "not healthy" (contract §5.2).
_DEGRADED_STATUSES = frozenset({"CONSTRAINED", "OUTAGE"})


# --------------------------------------------------------------------------
# Contract types (CONTRACT.md §7.2)
# --------------------------------------------------------------------------


class Severity(str, Enum):
    """Signal severity.  Ordered ``INFO < WARNING < SERIOUS < CRITICAL``."""

    INFO = "info"
    WARNING = "warning"
    SERIOUS = "serious"
    CRITICAL = "critical"


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.WARNING: 1,
    Severity.SERIOUS: 2,
    Severity.CRITICAL: 3,
}

#: A rule's dimensionless 0..1 score is mapped onto these bands.  One table,
#: applied identically by every rule, which is what makes severity a function
#: of the numbers rather than of the rule that produced them.
_SEVERITY_BANDS: tuple[tuple[float, Severity], ...] = (
    (0.80, Severity.CRITICAL),
    (0.55, Severity.SERIOUS),
    (0.30, Severity.WARNING),
)


@dataclass(frozen=True)
class RiskSignal:
    """One detected risk.  ``evidence`` carries the numbers behind it.

    Brief §9 requires an operator to be able to inspect *which signals*
    influenced a decision, so ``evidence`` holds the actual measured values
    (deviation, baseline, inventory, cover, delay ticks, ...) rather than a
    prose retelling, and is JSON-serialisable for the API layer (A8).
    """

    kind: str
    severity: Severity
    entity_type: str
    entity_id: str
    detected_at_tick: int
    summary: str
    evidence: dict[str, Any]
    confidence: float


# --------------------------------------------------------------------------
# Small numeric helpers — all pure
# --------------------------------------------------------------------------


def _g(obj: Any, name: str, default: Any = None) -> Any:
    """Read an attribute without ever raising on a surprising object."""
    value = getattr(obj, name, default)
    return default if value is None else value


def _finite(value: Any) -> float | None:
    """Coerce to a finite float, or return ``None``."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _r(value: Any, nd: int = 3) -> float | None:
    """Round for evidence payloads; non-finite values become ``None``."""
    number = _finite(value)
    return None if number is None else round(number, nd)


def _mapping(value: Any) -> Mapping[str, Any]:
    """Return ``value`` if it is a mapping, else an empty one."""
    return value if isinstance(value, Mapping) else {}


def _event_filter_match(parameters: Mapping[str, Any], key: str, entity_id: str) -> bool:
    """Does a domain event's filter list cover ``entity_id``?

    Integration guide section 7.8 pins the filter parameters as plural **lists**
    -- ``region_ids``, ``station_ids``, ``route_ids``, ``depot_ids``,
    ``fuel_types`` -- and states the matching rule outright: "if the list is
    empty, the event applies to all entities of that type". An empty list
    therefore means *every* entity, never *no* entity.

    This call site used to read a singular ``region_id`` scalar, which a real
    event never carries, so it could not match anything. The mismatch was
    silent -- ``_mapping`` and ``.get`` are both defensive, so nothing raised --
    and the consequence was not cosmetic: ``active_event_ids`` was always empty
    for every region, ``affected`` was undercounted, and a genuine
    ``regional_disruption`` could be suppressed at its
    ``regional_min_entities`` threshold. The detector reported calm during a
    crisis.

    The singular spelling stays accepted as a fallback because fixtures and
    older payloads carry it, but the documented plural list is authoritative.
    """
    values = parameters.get(key)
    if values is None:
        # Legacy singular spelling: `region_ids` -> `region_id`.
        values = parameters.get(key[:-1])
    if values is None or values == "":
        # Absent (or blank) filter: the guide's "applies to all of that type".
        return True
    if isinstance(values, (str, bytes)):
        # A bare scalar where a list is documented still means that one entity.
        return str(values) == entity_id
    if not isinstance(values, (list, tuple, set, frozenset)):
        # An unexpected shape is not evidence of a match; stay conservative.
        return False
    if not values:
        return True
    return any(str(value) == entity_id for value in values)


def _ramp(value: Any, low: float, high: float) -> float:
    """Monotone 0 -> 1 as ``value`` rises from ``low`` to ``high``."""
    number = _finite(value)
    if number is None:
        return 0.0
    if high <= low:
        return 1.0 if number >= high else 0.0
    if number <= low:
        return 0.0
    if number >= high:
        return 1.0
    return (number - low) / (high - low)


def _ramp_down(value: Any, good: float, bad: float) -> float:
    """Monotone 0 -> 1 as ``value`` falls from ``good`` down to ``bad``."""
    number = _finite(value)
    if number is None:
        return 0.0
    if bad >= good:
        return 1.0 if number <= bad else 0.0
    return _ramp(-number, -good, -bad)


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _severity_from_score(score: float) -> Severity:
    """Map a rule-independent 0..1 magnitude onto a :class:`Severity`."""
    bounded = _clip(_finite(score) or 0.0, 0.0, 1.0)
    for threshold, severity in _SEVERITY_BANDS:
        if bounded >= threshold:
            return severity
    return Severity.INFO


def _median(values: Sequence[float]) -> float | None:
    """``statistics.median`` that returns ``None`` instead of raising."""
    return statistics.median(values) if values else None


def _robust_location_scale(
    values: Sequence[float],
) -> tuple[float, float, str]:
    """Median location and a robust scale, plus the name of the scale used.

    This is the heart of the contract's "median + MAD, not mean + stdev"
    requirement.  ``stdev`` is *not* robust: one 10x spike can add enough
    variance to the baseline that every later, genuine anomaly falls inside
    one sigma.  MAD ignores the magnitude of outliers, so a spike moves the
    median and the MAD barely at all and later anomalies stay visible.

    Returns ``(median, scale, source)`` where ``source`` is one of:

    ``"mad"``
        The normal case, ``1.4826 * median(|x - median|)``.
    ``"mean_abs_dev"``
        MAD was exactly 0 because the majority of the window is identical
        (a flat history).  Mean absolute deviation is a weaker but still
        outlier-resistant scale, and it cannot be zero unless *every* point
        is identical.
    ``"degenerate"``
        Both are zero: a perfectly constant window.  ``scale`` is 0.0 and
        callers clamp the resulting z instead of dividing by zero.
    """
    med = _median(values)
    if med is None:
        return 0.0, 0.0, "degenerate"

    deviations = [abs(value - med) for value in values]
    mad = statistics.median(deviations) if deviations else 0.0
    scale = mad * _MAD_TO_SIGMA
    if scale > _EPSILON:
        return med, scale, "mad"

    mean_abs_dev = sum(deviations) / len(deviations)
    scale = mean_abs_dev * _MEAN_ABS_DEV_TO_SIGMA
    if scale > _EPSILON:
        return med, scale, "mean_abs_dev"

    return med, 0.0, "degenerate"


def _robust_z(value: float, med: float, scale: float) -> float:
    """Robust z-score, clamped and never infinite or NaN."""
    if scale <= _EPSILON:
        # A constant baseline: any deviation is unbounded, so saturate.
        if abs(value - med) <= _EPSILON:
            return 0.0
        return math.copysign(_Z_CLAMP, value - med)
    return _clip((value - med) / scale, -_Z_CLAMP, _Z_CLAMP)


def _sort_key(signal: RiskSignal) -> tuple[Any, ...]:
    """Most severe first, then most confident; then a total order.

    The trailing fields are a deterministic tie-break so that two signals
    which tie on severity *and* confidence always come out in the same order,
    whatever order the caller happened to build its containers in.
    """
    return (
        -_SEVERITY_RANK[signal.severity],
        -signal.confidence,
        signal.kind,
        signal.entity_type,
        signal.entity_id,
        signal.summary,
    )


def _clean_points(points: Any) -> list[Any]:
    """Filter a history sequence to usable points, ordered by tick."""
    if not points:
        return []
    usable: list[Any] = []
    for position, point in enumerate(points):
        liters = _finite(getattr(point, "liters", None))
        if liters is None:
            continue
        tick = _finite(getattr(point, "tick", None))
        usable.append((tick if tick is not None else float(position), liters, point))
    usable.sort(key=lambda item: item[0])
    return [item[2] for item in usable]


def _liters_of(points: Sequence[Any]) -> list[float]:
    return [value for value in (_finite(getattr(p, "liters", None)) for p in points) if value is not None]


def _demand_rate(points: Sequence[Any]) -> float | None:
    """Recent median liters-per-tick for a series."""
    if not points:
        return None
    window = _liters_of(list(points)[-_RATE_WINDOW:])
    return _median(window)


# --------------------------------------------------------------------------
# Detector
# --------------------------------------------------------------------------


@dataclass
class _Index:
    """Deterministic, pre-computed view of the two inputs."""

    snapshot: Any
    tick: int
    series: list[tuple[str, str, list[Any]]]
    rate: dict[tuple[str, str], float]
    region_rate: dict[tuple[str, str], float]
    stations_by_id: dict[str, Any]
    depots_by_id: dict[str, Any]


class AnomalyDetector:
    """Detect anomalous demand, inventory, routes, regions and supply.

    Every threshold is a constructor keyword with a documented default, so a
    caller can tune sensitivity without touching the rules.  The object is
    stateless after construction: ``detect()`` is a pure function of its
    arguments and two detectors built with the same thresholds are
    interchangeable.
    """

    def __init__(
        self,
        *,
        # demand anomalies
        z_warning: float = 3.0,
        z_critical: float = 6.0,
        min_points: int = 6,
        # inventory
        inventory_low_fill: float = 0.25,
        inventory_critical_fill: float = 0.10,
        coverage_warning_ticks: float = 6.0,
        coverage_critical_ticks: float = 2.0,
        # routes
        route_deficit_min: float = 0.20,
        route_deficit_critical: float = 0.90,
        # regions
        regional_min_entities: int = 2,
        regional_full_share: float = 0.90,
        # supply arrivals
        supply_delay_warning_ticks: float = 2.0,
        supply_delay_critical_ticks: float = 8.0,
        supply_shortfall_min_ratio: float = 0.25,
    ) -> None:
        self.z_warning = float(z_warning)
        self.z_critical = float(z_critical)
        self.min_points = max(2, int(min_points))
        self.inventory_low_fill = float(inventory_low_fill)
        self.inventory_critical_fill = float(inventory_critical_fill)
        self.coverage_warning_ticks = float(coverage_warning_ticks)
        self.coverage_critical_ticks = float(coverage_critical_ticks)
        self.route_deficit_min = float(route_deficit_min)
        self.route_deficit_critical = float(route_deficit_critical)
        self.regional_min_entities = max(1, int(regional_min_entities))
        self.regional_full_share = float(regional_full_share)
        self.supply_delay_warning_ticks = float(supply_delay_warning_ticks)
        self.supply_delay_critical_ticks = float(supply_delay_critical_ticks)
        self.supply_shortfall_min_ratio = float(supply_shortfall_min_ratio)

    # -- public API --------------------------------------------------------

    def detect(
        self,
        *,
        snapshot: Snapshot,
        history: Mapping[tuple[str, str], Sequence[DemandPoint]],
    ) -> list[RiskSignal]:
        """Return every current risk signal, most severe first.

        Never raises on empty, single-point, flat or malformed input: the
        worst case is an empty list.
        """
        if snapshot is None:
            return []

        index = self._build_index(snapshot, history)

        signals: list[RiskSignal] = []
        signals.extend(self._demand_anomalies(index))
        signals.extend(self._inventory_drops(index))
        signals.extend(self._route_bottlenecks(index))
        signals.extend(self._regional_disruptions(index))
        signals.extend(self._supply_shortfalls(index))
        signals.sort(key=_sort_key)
        return signals

    # -- index -------------------------------------------------------------

    def _build_index(self, snapshot: Any, history: Any) -> _Index:
        tick = int(_finite(_g(snapshot, "tick", 0)) or 0)

        notes: list[tuple[str, str, list[Any]]] = []
        if isinstance(history, Mapping):
            for key, points in history.items():
                station_id: Any
                fuel_type: Any
                try:
                    station_id, fuel_type = key  # contract: (station_id, fuel_type)
                except (TypeError, ValueError):
                    station_id, fuel_type = key, ""
                notes.append((str(station_id), str(fuel_type), _clean_points(points)))
        notes.sort(key=lambda item: (item[0], item[1]))

        rate: dict[tuple[str, str], float] = {}
        for station_id, fuel_type, points in notes:
            value = _demand_rate(points)
            if value is not None:
                rate[(station_id, fuel_type)] = value

        stations_by_id: dict[str, Any] = {}
        for station in _g(snapshot, "stations", ()) or ():
            stations_by_id[str(_g(station, "id", ""))] = station
        depots_by_id: dict[str, Any] = {}
        for depot in _g(snapshot, "depots", ()) or ():
            depots_by_id[str(_g(depot, "id", ""))] = depot

        region_rate: dict[tuple[str, str], float] = {}
        for station_id, fuel_type in sorted(rate):
            station = stations_by_id.get(station_id)
            if station is None:
                continue
            region_id = str(_g(station, "region_id", ""))
            if not region_id:
                continue
            region_rate[(region_id, fuel_type)] = (
                region_rate.get((region_id, fuel_type), 0.0) + rate[(station_id, fuel_type)]
            )

        return _Index(
            snapshot=snapshot,
            tick=tick,
            series=notes,
            rate=rate,
            region_rate=region_rate,
            stations_by_id=stations_by_id,
            depots_by_id=depots_by_id,
        )

    # -- rule 1: anomalous demand -----------------------------------------

    def _demand_anomalies(self, index: _Index) -> list[RiskSignal]:
        """Station/fuel demand far from its own robust baseline.

        The baseline is every observation *before* the latest one.  An early
        spike sits inside that baseline; because the scale is MAD-based it
        barely moves the scale, so a later, moderate anomaly is still flagged.
        """
        signals: list[RiskSignal] = []
        span = max(self.z_critical - self.z_warning, _EPSILON)

        for station_id, fuel_type, points in index.series:
            if len(points) < self.min_points:
                # Not enough history for a baseline; a single-point series has
                # no spread at all and simply produces nothing.
                continue

            latest_values = _liters_of(list(points)[-1:])
            baseline_values = _liters_of(list(points)[:-1])
            if not latest_values or not baseline_values:
                continue
            latest = latest_values[-1]

            med, scale, scale_source = _robust_location_scale(baseline_values)
            if scale <= _EPSILON and abs(latest - med) <= _EPSILON:
                continue  # constant history, nothing changed

            abs_z = abs(_robust_z(latest, med, scale))
            if abs_z < self.z_warning:
                continue

            score = _ramp(abs_z, self.z_warning, self.z_critical)
            severity = _severity_from_score(score)
            direction = "spike" if latest > med else "drop"

            station = index.stations_by_id.get(station_id)
            confidence = _clip(
                0.45 * _ramp(len(baseline_values), self.min_points, self.min_points * 4.0)
                + 0.55 * score,
                0.05,
                0.99,
            )

            signals.append(
                RiskSignal(
                    kind="demand_anomaly",
                    severity=severity,
                    entity_type="station",
                    entity_id=station_id,
                    detected_at_tick=index.tick,
                    summary=(
                        f"Station {station_id} {fuel_type}: demand {latest:.0f} L/tick is "
                        f"{abs_z:.1f} sigma {direction} against a robust baseline of "
                        f"{med:.0f} L/tick"
                    ),
                    evidence={
                        "fuel_type": fuel_type,
                        "latest_liters": _r(latest),
                        "baseline_median_liters": _r(med),
                        "baseline_scale_liters": _r(scale),
                        "scale_source": scale_source,
                        "robust_z": _r(_robust_z(latest, med, scale), 4),
                        "abs_robust_z": _r(abs_z, 4),
                        "direction": direction,
                        "baseline_points": len(baseline_values),
                        "history_points": len(points),
                        "z_warning": self.z_warning,
                        "z_critical": self.z_critical,
                        "score": _r(score, 4),
                        "station_status": _g(station, "status", None) if station else None,
                        "station_demand_multiplier": (
                            _r(_g(station, "demand_multiplier", None), 4) if station else None
                        ),
                        "station_region_id": _g(station, "region_id", None) if station else None,
                        "station_present_in_snapshot": station is not None,
                    },
                    confidence=round(confidence, 4),
                )
            )
        return signals

    # -- rule 2: abnormal inventory ---------------------------------------

    def _inventory_drops(self, index: _Index) -> list[RiskSignal]:
        """Depot/station inventory abnormally low for one fuel.

        Two independent measures, both taken from the numbers:

        * **fill ratio** -- inventory against the entity's own capacity;
        * **cover** -- inventory divided by the per-tick demand that has to
          be served from it (for a station, its own recent demand; for a
          depot, the summed recent demand of the stations in its region).

        The score is the worse of the two, so an entity can be flagged for
        being nearly empty even when its demand history is missing.
        """
        signals: list[RiskSignal] = []

        for entity_type, entities, cover_for in (
            ("station", _g(index.snapshot, "stations", ()) or (), self._station_cover),
            ("depot", _g(index.snapshot, "depots", ()) or (), self._depot_cover),
        ):
            for entity in sorted(entities, key=lambda e: str(_g(e, "id", ""))):
                entity_id = str(_g(entity, "id", ""))
                if not entity_id:
                    continue
                capacity = _mapping(_g(entity, "capacity", {}))
                inventory = _mapping(_g(entity, "inventory", {}))

                for fuel_type in _fuel_keys(capacity, inventory):
                    capacity_liters = _finite(capacity.get(fuel_type))
                    if capacity_liters is None or capacity_liters <= 0:
                        # No capacity to compare against: skip rather than
                        # divide by zero or invent a ratio.
                        continue
                    inventory_liters = _finite(inventory.get(fuel_type)) or 0.0
                    fill_ratio = inventory_liters / capacity_liters

                    cover, cover_basis = cover_for(index, entity, fuel_type)
                    fill_score = _ramp(
                        1.0 - fill_ratio,
                        1.0 - self.inventory_low_fill,
                        1.0 - self.inventory_critical_fill,
                    )
                    cover_score = _ramp_down(
                        cover, self.coverage_warning_ticks, self.coverage_critical_ticks
                    )
                    score = max(fill_score, cover_score)

                    low_fill = fill_ratio <= self.inventory_low_fill
                    low_cover = cover is not None and cover <= self.coverage_warning_ticks
                    if not (low_fill or low_cover):
                        continue

                    confidence = _clip(
                        0.5 * _ramp(1.0 - fill_ratio, 0.0, 1.0 - self.inventory_critical_fill)
                        + 0.5 * (cover_score if cover is not None else fill_score),
                        0.05,
                        0.99,
                    )
                    cover_text = (
                        f", {cover:.1f} ticks of cover at {cover_basis:.0f} L/tick"
                        if cover is not None
                        else ", no demand history to size cover against"
                    )
                    if entity_type == "station":
                        summary = (
                            f"Station {entity_id} {fuel_type}: inventory "
                            f"{inventory_liters:.0f} L is {fill_ratio:.0%} of capacity"
                            f"{cover_text}"
                        )
                    else:
                        region_id = _g(entity, "region_id", None)
                        summary = (
                            f"Depot {entity_id} {fuel_type}: inventory "
                            f"{inventory_liters:.0f} L is {fill_ratio:.0%} of capacity"
                            f"{cover_text}"
                            + (f" for region {region_id}" if cover is not None and region_id else "")
                        )

                    signals.append(
                        RiskSignal(
                            kind="inventory_drop",
                            severity=_severity_from_score(score),
                            entity_type=entity_type,
                            entity_id=entity_id,
                            detected_at_tick=index.tick,
                            summary=summary,
                            evidence={
                                "fuel_type": fuel_type,
                                "inventory_liters": _r(inventory_liters),
                                "capacity_liters": _r(capacity_liters),
                                "fill_ratio": _r(fill_ratio, 4),
                                "cover_ticks": _r(cover, 3) if cover is not None else None,
                                "cover_basis_liters_per_tick": (
                                    _r(cover_basis, 3) if cover is not None else None
                                ),
                                "cover_scope": (
                                    "station"
                                    if entity_type == "station"
                                    else "region_stations"
                                ),
                                "low_fill_threshold": self.inventory_low_fill,
                                "coverage_warning_ticks": self.coverage_warning_ticks,
                                "coverage_critical_ticks": self.coverage_critical_ticks,
                                "fill_score": _r(fill_score, 4),
                                "cover_score": _r(cover_score, 4),
                                "score": _r(score, 4),
                                "entity_status": _g(entity, "status", None),
                                "region_id": _g(entity, "region_id", None),
                            },
                            confidence=round(confidence, 4),
                        )
                    )
        return signals

    def _station_cover(
        self, index: _Index, station: Any, fuel_type: str
    ) -> tuple[float | None, float]:
        """Ticks of cover from the station's own demand history."""
        rate = index.rate.get((str(_g(station, "id", "")), fuel_type))
        if not rate or rate <= 0:
            return None, 0.0
        inventory = _finite(_mapping(_g(station, "inventory", {})).get(fuel_type)) or 0.0
        return inventory / rate, rate

    def _depot_cover(
        self, index: _Index, depot: Any, fuel_type: str
    ) -> tuple[float | None, float]:
        """Ticks of cover for the whole region the depot serves."""
        region_id = str(_g(depot, "region_id", ""))
        if not region_id:
            return None, 0.0
        rate = index.region_rate.get((region_id, fuel_type))
        if not rate or rate <= 0:
            return None, 0.0
        inventory = _finite(_mapping(_g(depot, "inventory", {})).get(fuel_type)) or 0.0
        return inventory / rate, rate

    # -- rule 3: route bottlenecks ----------------------------------------

    def _route_bottlenecks(self, index: _Index) -> list[RiskSignal]:
        """A route that is disrupted, or too small for the demand it feeds."""
        signals: list[RiskSignal] = []
        routes = list(_g(index.snapshot, "routes", ()) or ())

        for route in sorted(routes, key=lambda r: str(_g(r, "id", ""))):
            route_id = str(_g(route, "id", ""))
            dest_id = str(_g(route, "destination_station_id", ""))
            status = str(_g(route, "status", "") or "")
            disrupted = status.upper() == "DISRUPTED"

            alternatives = sum(
                1
                for other in routes
                if other is not route
                and str(_g(other, "destination_station_id", "")) == dest_id
                and str(_g(other, "status", "") or "").upper() == "AVAILABLE"
            )

            rate = index.rate.get((dest_id, "DIESEL"))
            # Prefer the fuel that actually moves on this route: the depot's
            # own fuel keys are the best signal available on a Route record.
            source = index.depots_by_id.get(str(_g(route, "source_depot_id", "")))
            if source is not None:
                for fuel_type in _fuel_keys(
                    _mapping(_g(source, "capacity", {})), _mapping(_g(source, "inventory", {}))
                ):
                    candidate = index.rate.get((dest_id, fuel_type))
                    if candidate:
                        rate = candidate
                        break

            max_shipment = _finite(_g(route, "max_shipment", None))
            deficit: float | None = None
            if rate and rate > 0 and max_shipment is not None:
                deficit = 1.0 - (max_shipment / rate)

            under_capacity = deficit is not None and deficit >= self.route_deficit_min
            if not disrupted and not under_capacity:
                continue

            station = index.stations_by_id.get(dest_id)
            station_cover: float | None = None
            station_rate = rate or 0.0
            if station is not None and station_rate > 0:
                inventory = sum(
                    _finite(v) or 0.0
                    for v in _mapping(_g(station, "inventory", {})).values()
                )
                station_cover = inventory / station_rate

            cover_score = _ramp_down(
                station_cover, self.coverage_warning_ticks, self.coverage_critical_ticks
            )
            capacity_score = (
                _ramp(deficit, self.route_deficit_min, self.route_deficit_critical)
                if deficit is not None
                else 0.0
            )
            if disrupted:
                # Fewer alternatives means the same disruption costs the
                # station more, so the single-source factor drives severity.
                single_source = {0: 1.0, 1: 0.60}.get(alternatives, 0.25)
                score = max(single_source, cover_score, capacity_score)
            else:
                score = capacity_score

            if disrupted:
                # A disrupted route with no alternative is a much more certain
                # finding than one the network can route around.
                confidence = _clip(
                    0.55 * score + 0.45 * _ramp(2.0 - min(alternatives, 2), 0.0, 2.0),
                    0.05,
                    0.99,
                )
            else:
                confidence = _clip(0.35 + 0.6 * score, 0.05, 0.99)

            parts: list[str] = []
            if disrupted:
                parts.append(f"Route {route_id} to station {dest_id} is DISRUPTED")
                parts.append(
                    "no alternative available route"
                    if alternatives == 0
                    else f"{alternatives} alternative available route(s)"
                )
            if under_capacity and deficit is not None:
                parts.append(
                    f"max_shipment {max_shipment:.0f} L cannot cover station demand "
                    f"{rate:.0f} L/tick ({deficit:.0%} short)"
                )

            signals.append(
                RiskSignal(
                    kind="route_bottleneck",
                    severity=_severity_from_score(score),
                    entity_type="route",
                    entity_id=route_id,
                    detected_at_tick=index.tick,
                    summary="; ".join(parts),
                    evidence={
                        "route_status": status or None,
                        "disrupted": disrupted,
                        "source_depot_id": _g(route, "source_depot_id", None),
                        "destination_station_id": dest_id,
                        "max_shipment_liters": _r(max_shipment),
                        "station_demand_liters_per_tick": _r(rate),
                        "capacity_deficit_ratio": _r(deficit, 4) if deficit is not None else None,
                        "alternative_available_routes": alternatives,
                        "transit_ticks": _g(route, "transit_ticks", None),
                        "station_cover_ticks": _r(station_cover, 3) if station_cover is not None else None,
                        "station_status": _g(station, "status", None) if station else None,
                        "capacity_score": _r(capacity_score, 4),
                        "cover_score": _r(cover_score, 4),
                        "score": _r(score, 4),
                    },
                    confidence=round(confidence, 4),
                )
            )
        return signals

    # -- rule 4: regional disruption --------------------------------------

    def _regional_disruptions(self, index: _Index) -> list[RiskSignal]:
        """A region carrying several affected entities at once."""
        signals: list[RiskSignal] = []
        regions = list(_g(index.snapshot, "regions", ()) or ())
        stations = list(_g(index.snapshot, "stations", ()) or ())
        depots = list(_g(index.snapshot, "depots", ()) or ())
        routes = list(_g(index.snapshot, "routes", ()) or ())
        events = list(_g(index.snapshot, "events", ()) or ())

        for region in sorted(regions, key=lambda r: str(_g(r, "id", ""))):
            region_id = str(_g(region, "id", ""))
            if not region_id:
                continue

            region_stations = [s for s in stations if str(_g(s, "region_id", "")) == region_id]
            region_depots = [d for d in depots if str(_g(d, "region_id", "")) == region_id]

            degraded_stations = sorted(
                str(_g(s, "id", ""))
                for s in region_stations
                if str(_g(s, "status", "") or "").upper() in _DEGRADED_STATUSES
            )
            degraded_depots = sorted(
                str(_g(d, "id", ""))
                for d in region_depots
                if str(_g(d, "status", "") or "").upper() in _DEGRADED_STATUSES
            )

            station_ids = {str(_g(s, "id", "")) for s in region_stations}
            depot_ids = {str(_g(d, "id", "")) for d in region_depots}

            disrupted_routes: list[str] = []
            for route in routes:
                if str(_g(route, "status", "") or "").upper() != "DISRUPTED":
                    continue
                if (
                    str(_g(route, "destination_station_id", "")) in station_ids
                    or str(_g(route, "source_depot_id", "")) in depot_ids
                ):
                    disrupted_routes.append(str(_g(route, "id", "")))
            disrupted_routes.sort()

            active_events: list[str] = []
            for event in events:
                if str(_g(event, "status", "") or "").upper() != "ACTIVE":
                    continue
                parameters = _mapping(_g(event, "parameters", {}))
                if _event_filter_match(parameters, "region_ids", region_id):
                    active_events.append(str(_g(event, "id", "")))
            active_events.sort()

            affected = len(degraded_stations) + len(degraded_depots) + len(disrupted_routes) + len(active_events)
            if affected < self.regional_min_entities:
                continue

            total_entities = len(region_stations) + len(region_depots)
            share = affected / total_entities if total_entities else 1.0
            score = max(
                _ramp(affected, self.regional_min_entities, self.regional_min_entities + 4.0),
                _ramp(share, 0.34, self.regional_full_share),
            )
            confidence = _clip(
                0.45 * _ramp(affected, 1.0, 6.0) + 0.55 * _ramp(share, 0.20, 0.85),
                0.05,
                0.99,
            )

            degraded_count = len(degraded_stations) + len(degraded_depots)
            signals.append(
                RiskSignal(
                    kind="regional_disruption",
                    severity=_severity_from_score(score),
                    entity_type="region",
                    entity_id=region_id,
                    detected_at_tick=index.tick,
                    summary=(
                        f"Region {region_id}: {affected} affected entities "
                        f"({degraded_count} degraded entities, {len(disrupted_routes)} disrupted "
                        f"routes, {len(active_events)} active events) across "
                        f"{total_entities} entities"
                    ),
                    evidence={
                        "region_id": region_id,
                        "region_name": _g(region, "name", None),
                        "region_demand_factor": _r(_g(region, "demand_factor", None), 4),
                        "affected_entities": affected,
                        "total_entities": total_entities,
                        "affected_share": _r(share, 4),
                        "degraded_station_ids": degraded_stations,
                        "degraded_depot_ids": degraded_depots,
                        "disrupted_route_ids": disrupted_routes,
                        "active_event_ids": active_events,
                        "min_entities": self.regional_min_entities,
                        "score": _r(score, 4),
                    },
                    confidence=round(confidence, 4),
                )
            )
        return signals

    # -- rule 5: supply shortfall -----------------------------------------

    def _supply_shortfalls(self, index: _Index) -> list[RiskSignal]:
        """An arrival that is late, or that leaves the depot short of plan.

        "Delayed" is measured against ``planned_tick`` -- both for arrivals
        already flagged ``DELAYED`` and for arrivals that are simply overdue
        while the tick has moved past their plan.

        "Short against plan" is measured as the residual gap after the
        quantity lands: what the depot still could not cover for its region
        over the warning horizon.  That needs demand history; without it the
        rule still reports pure delay.
        """
        signals: list[RiskSignal] = []
        arrivals = list(_g(index.snapshot, "supply_arrivals", ()) or ())

        for arrival in sorted(arrivals, key=lambda a: str(_g(a, "id", ""))):
            arrival_id = str(_g(arrival, "id", ""))
            depot_id = str(_g(arrival, "depot_id", ""))
            fuel_type = str(_g(arrival, "fuel_type", "") or "")
            status = str(_g(arrival, "status", "") or "")

            planned_tick = _finite(_g(arrival, "planned_tick", None))
            actual_tick = _finite(_g(arrival, "actual_tick", None))
            quantity = _finite(_g(arrival, "quantity", None)) or 0.0

            arrived = actual_tick is not None or status.upper() == "ARRIVED"
            if arrived:
                delay_ticks = (actual_tick - planned_tick) if (actual_tick is not None and planned_tick is not None) else 0.0
            elif planned_tick is not None:
                delay_ticks = index.tick - planned_tick
            else:
                delay_ticks = 0.0
            delay_ticks = max(0.0, delay_ticks)

            depot = index.depots_by_id.get(depot_id)
            shortfall_liters = 0.0
            required_liters: float | None = None
            projected_liters: float | None = None
            region_id: str | None = None
            rate: float | None = None
            required_basis: str | None = None
            if depot is not None:
                region_id = _g(depot, "region_id", None)
                held = _finite(_mapping(_g(depot, "inventory", {})).get(fuel_type)) or 0.0
                if region_id:
                    rate = index.region_rate.get((str(region_id), fuel_type))
                if rate and rate > 0:
                    required_liters = rate * self.coverage_warning_ticks
                    projected_liters = held + quantity
                    shortfall_liters = max(0.0, required_liters - projected_liters)
                    required_basis = "region_demand"
                elif fuel_type:
                    # No demand history to size regional cover against, so fall
                    # back to the depot's own capacity as the plan the arrival
                    # was meant to restore.  Recorded as its own basis so the
                    # operator can see which yardstick was used.
                    capacity_liters = _finite(
                        _mapping(_g(depot, "capacity", {})).get(fuel_type)
                    )
                    if capacity_liters and capacity_liters > 0:
                        required_liters = capacity_liters
                        projected_liters = held + quantity
                        shortfall_liters = max(0.0, required_liters - projected_liters)
                        required_basis = "depot_capacity"

            shortfall_ratio = (
                shortfall_liters / required_liters
                if required_liters and required_liters > 0
                else 0.0
            )
            # The simulator's own DELAYED flag is a source-of-truth statement
            # that the arrival is late, so it triggers the rule even when the
            # tick arithmetic cannot yet measure any overdue ticks.  The
            # severity still comes from the measured numbers, so such a signal
            # lands at INFO rather than being inflated to match the label.
            declared_delayed = status.upper() == "DELAYED" and not arrived
            late_enough = delay_ticks >= self.supply_delay_warning_ticks
            short_enough = shortfall_ratio >= self.supply_shortfall_min_ratio
            if not (late_enough or declared_delayed or short_enough):
                continue

            delay_score = _ramp(
                delay_ticks, self.supply_delay_warning_ticks, self.supply_delay_critical_ticks
            )
            short_score = _ramp(shortfall_ratio, self.supply_shortfall_min_ratio, 1.0)
            score = max(delay_score, short_score)

            confidence = _clip(
                0.15
                + 0.5 * (delay_score if (late_enough or declared_delayed) else 0.0)
                + 0.5 * (short_score if short_enough else 0.0),
                0.05,
                0.99,
            )

            parts: list[str] = []
            if late_enough or declared_delayed:
                if planned_tick is not None:
                    verb = "arrived late by" if arrived else "is overdue by"
                    parts.append(
                        f"Supply arrival {arrival_id} for depot {depot_id} "
                        f"({fuel_type or 'unknown fuel'}) {verb} {delay_ticks:.0f} ticks "
                        f"against a plan of tick {planned_tick:.0f}"
                    )
                else:
                    parts.append(
                        f"Supply arrival {arrival_id} for depot {depot_id} "
                        f"({fuel_type or 'unknown fuel'}) is flagged DELAYED with no "
                        f"planned tick on record"
                    )
            if short_enough and required_liters is not None and projected_liters is not None:
                basis = (
                    f"{self.coverage_warning_ticks:.0f} ticks of regional cover"
                    if required_basis == "region_demand"
                    else "the depot's own capacity"
                )
                parts.append(
                    f"lands {quantity:.0f} L leaving {projected_liters:.0f} L against "
                    f"{required_liters:.0f} L needed for {basis} "
                    f"({shortfall_liters:.0f} L short)"
                )

            signals.append(
                RiskSignal(
                    kind="supply_shortfall",
                    severity=_severity_from_score(score),
                    entity_type="depot",
                    entity_id=depot_id,
                    detected_at_tick=index.tick,
                    summary="; ".join(parts),
                    evidence={
                        "arrival_id": arrival_id,
                        "fuel_type": fuel_type or None,
                        "arrival_status": status or None,
                        "planned_tick": _r(planned_tick, 0),
                        "actual_tick": _r(actual_tick, 0),
                        "delay_ticks": _r(delay_ticks, 1),
                        "arrived": arrived,
                        "declared_delayed": declared_delayed,
                        "quantity_liters": _r(quantity),
                        "projected_depot_liters": (
                            _r(projected_liters) if projected_liters is not None else None
                        ),
                        "required_liters": _r(required_liters) if required_liters is not None else None,
                        "required_basis": required_basis,
                        "shortfall_liters": _r(shortfall_liters),
                        "shortfall_ratio": _r(shortfall_ratio, 4),
                        "regional_demand_liters_per_tick": _r(rate, 3) if rate else None,
                        "region_id": region_id,
                        "delay_warning_ticks": self.supply_delay_warning_ticks,
                        "delay_score": _r(delay_score, 4),
                        "short_score": _r(short_score, 4),
                        "score": _r(score, 4),
                    },
                    confidence=round(confidence, 4),
                )
            )
        return signals


def _fuel_keys(capacity: Mapping[str, Any], inventory: Mapping[str, Any]) -> list[str]:
    """Union of the fuel keys present, in a deterministic order.

    The simulator's ``fuel_type`` enum is ``DIESEL | PETROL | OCTANE``; any
    unknown value the organisers introduce mid-event is still detected, it
    just sorts after the known ones.
    """
    seen = set(capacity) | set(inventory)
    known = [fuel for fuel in _KNOWN_FUELS if fuel in seen]
    extra = sorted(str(key) for key in seen if key not in _KNOWN_FUELS)
    return known + extra
