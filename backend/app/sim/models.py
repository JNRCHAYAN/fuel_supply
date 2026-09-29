"""Typed models for the BUP Fuel Supply Simulator.

CONTRACT.md section 5.2. Every field name below was observed from a live
response at ``http://localhost:8001`` (or from the simulator's own
``/openapi.json``); none of them were guessed. Two wire shapes that the contract
lists but does not enumerate -- ``DemandObservation`` and ``Fault`` -- are
modelled from the live payload, and that gap is called out in the module
docstring of :mod:`app.sim.client`.

Two rules run through this module:

1. **Tolerate unknown enum values.** Brief section 10 says the organisers may
   change the environment mid-event. A status string we have never seen is
   logged and passed through as a plain ``str`` -- it never raises. Every
   modelling enum subclasses ``str``, so a member and the raw wire value compare
   equal and existing code keeps working either way.

2. **Validate shape, tolerate values.** ``X.from_api`` requires a JSON object
   (or array, for list endpoints) but fills in a missing key with a safe
   default rather than raising. A wrong *type* where a number belongs is a
   genuine contract violation and raises ``ValueError``, which
   :class:`app.sim.client.SimulatorClient` converts into a typed
   ``SimulatorError`` and a log line -- never a bare crash.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "FuelType",
    "EntityStatus",
    "RouteStatus",
    "SupplyStatus",
    "AllocationStatus",
    "EventStatus",
    "InstanceStatus",
    "DemandProfile",
    "EventType",
    "FaultType",
    "CircuitState",
    "to_enum",
    "reset_unknown_value_tracking",
    "Health",
    "SimulationState",
    "SimInstance",
    "Metrics",
    "Region",
    "Depot",
    "Station",
    "Route",
    "SupplyArrival",
    "DomainEvent",
    "Allocation",
    "AuditEntry",
    "DemandObservation",
    "Fault",
    "AllocationRequest",
    "EventRequest",
    "FaultRequest",
    "StreamEvent",
    "STREAM_EVENT_NAMES",
    "Snapshot",
    "DemandPoint",
    "wire_value",
]

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
# All are str-mixins on purpose: `Depot.status == "OPEN"` and
# `Depot.status == EntityStatus.OPEN` are both true, so downstream code does not
# have to care whether a value was recognised.


class FuelType(str, Enum):
    DIESEL = "DIESEL"
    PETROL = "PETROL"
    OCTANE = "OCTANE"


class EntityStatus(str, Enum):
    """Depot and station status (they share one vocabulary on the wire)."""

    OPEN = "OPEN"
    CONSTRAINED = "CONSTRAINED"
    OUTAGE = "OUTAGE"


class RouteStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    DISRUPTED = "DISRUPTED"


class SupplyStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    DELAYED = "DELAYED"
    ARRIVED = "ARRIVED"


class AllocationStatus(str, Enum):
    PENDING = "PENDING"
    IN_TRANSIT = "IN_TRANSIT"
    ARRIVED = "ARRIVED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class EventStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"


class InstanceStatus(str, Enum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"


class DemandProfile(str, Enum):
    URBAN_HIGH = "urban_high"
    INDUSTRIAL = "industrial"
    HIGHWAY = "highway"
    REGIONAL = "regional"


class EventType(str, Enum):
    """Observed in the simulator's own ``EventCreate`` schema."""

    DEMAND_SPIKE = "demand_spike"
    SHIPMENT_DELAY = "shipment_delay"
    ROUTE_DISRUPTION = "route_disruption"
    STATION_OUTAGE = "station_outage"
    DEPOT_CONSTRAINT = "depot_constraint"
    SUPPLY_SHORTFALL = "supply_shortfall"


class FaultType(str, Enum):
    """Observed in the simulator's own ``FaultCreate`` schema."""

    LATENCY = "latency"
    UNAVAILABLE = "unavailable"
    ERROR_RATE = "error_rate"
    STALE_DATA = "stale_data"
    STREAM_DISCONNECT = "stream_disconnect"


class CircuitState(str, Enum):
    """Re-exported here so `breaker_state` has a home alongside the models."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# -- tolerant coercion ------------------------------------------------------- #

#: Distinct ``(enum name, value)`` pairs already warned about. An unknown value
#: is a genuine anomaly and must be logged -- but a station whose status we do
#: not recognise would otherwise emit a warning on every poll. We warn once per
#: distinct surprise and drop to DEBUG afterwards.
_UNKNOWN_VALUES_SEEN: set[tuple[str, str]] = set()

_MISSING = object()


def reset_unknown_value_tracking() -> None:
    """Forget which unknown enum values have already been warned about.

    Exists so a test can assert the WARNING is emitted regardless of what other
    tests ran first. Production code has no reason to call this.
    """
    _UNKNOWN_VALUES_SEEN.clear()


def to_enum(enum_cls: type[Enum], value: Any, *, field_name: str = "") -> Any:
    """Coerce ``value`` to ``enum_cls``, tolerating anything unexpected.

    Returns the enum member when the value is recognised, and the raw value
    unchanged when it is not -- after logging it. This function never raises:
    that is the whole point (CONTRACT.md 5.2, brief section 10).
    """
    if value is None:
        return None
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except ValueError:
        label = field_name or enum_cls.__name__
        key = (enum_cls.__name__, str(value))
        if key not in _UNKNOWN_VALUES_SEEN:
            _UNKNOWN_VALUES_SEEN.add(key)
            logger.warning(
                "simulator returned an unrecognised %s value; tolerating it",
                label,
                extra={
                    "event": "sim.unknown_enum",
                    "enum": enum_cls.__name__,
                    "field": label,
                    "value": str(value),
                },
            )
        else:
            logger.debug("simulator still returning unrecognised %s", label)
        return value


# --------------------------------------------------------------------------- #
# Field helpers
# --------------------------------------------------------------------------- #


def _require_mapping(payload: Any, model: str) -> Mapping[str, Any]:
    """Shape check only: a list where an object belongs is a real error.

    Note this is the same ``isinstance(x, dict)``-before-list discipline that
    :mod:`app.sim.errors` uses; see CONTRACT.md 5.3.
    """
    if not isinstance(payload, Mapping):
        raise ValueError(
            f"{model}: expected a JSON object, got {type(payload).__name__}"
        )
    return payload


def _require_sequence(payload: Any, model: str) -> Sequence[Any]:
    if isinstance(payload, (str, bytes, bytearray)) or not isinstance(
        payload, Sequence
    ):
        raise ValueError(
            f"{model}: expected a JSON array, got {type(payload).__name__}"
        )
    return payload


def _text(payload: Mapping[str, Any], key: str, default: str = "") -> str:
    value = payload.get(key, _MISSING)
    if value is _MISSING or value is None:
        return default
    return value if isinstance(value, str) else str(value)


def _number(
    payload: Mapping[str, Any], key: str, *, model: str, default: float | None = None
) -> float:
    value = payload.get(key, _MISSING)
    if value is _MISSING or value is None:
        if default is None:
            raise ValueError(f"{model}.{key}: required number is missing")
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{model}.{key}: expected a number, got {value!r}"
        ) from exc


def _integer(
    payload: Mapping[str, Any], key: str, *, model: str, default: int | None = None
) -> int:
    value = payload.get(key, _MISSING)
    if value is _MISSING or value is None:
        if default is None:
            raise ValueError(f"{model}.{key}: required integer is missing")
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{model}.{key}: expected an integer, got {value!r}"
        ) from exc


def _optional_integer(payload: Mapping[str, Any], key: str, *, model: str) -> int | None:
    value = payload.get(key, _MISSING)
    if value is _MISSING or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{model}.{key}: expected an integer or null, got {value!r}"
        ) from exc


def _optional_text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _mapping_of_floats(
    payload: Mapping[str, Any], key: str, *, model: str
) -> dict[str, float]:
    """``{"DIESEL": 85000.0, ...}`` -- capacity / inventory blocks."""
    raw = payload.get(key, _MISSING)
    if raw is _MISSING or raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"{model}.{key}: expected an object of numbers")
    out: dict[str, float] = {}
    for fuel, value in raw.items():
        try:
            out[str(fuel)] = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{model}.{key}[{fuel!r}]: expected a number, got {value!r}"
            ) from exc
    return out


def _free_mapping(payload: Mapping[str, Any], key: str) -> dict[str, Any]:
    raw = payload.get(key)
    return dict(raw) if isinstance(raw, Mapping) else {}


def wire_value(value: Any) -> Any:
    """Render a value the way the simulator expects it on the wire.

    ``str()`` is the wrong tool for a ``str``-mixin enum: on Python 3.11+
    ``str(FuelType.DIESEL)`` is ``"FuelType.DIESEL"``, not ``"DIESEL"``, and a
    body carrying that would be rejected by the simulator's own schema. Unwrap
    the enum's value explicitly instead. This applies to every field the caller
    might hand us either as an enum member or as a bare string.
    """
    if isinstance(value, Enum):
        return value.value
    return value


# --------------------------------------------------------------------------- #
# Leaf models
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SimulationState:
    """::

        {"status": "RUNNING", "tick": 3888}
    """

    status: str
    tick: int

    @classmethod
    def from_api(cls, payload: Any) -> "SimulationState":
        data = _require_mapping(payload, "SimulationState")
        return cls(
            status=to_enum(
                InstanceStatus,
                data.get("status"),
                field_name="simulation.status",
            )
            or "UNKNOWN",
            tick=_integer(data, "tick", model="SimulationState", default=0),
        )


@dataclass(frozen=True, slots=True)
class Health:
    """``GET /v1/health``::

        {"status": "ok", "database": "ok",
         "simulation": {"status": "RUNNING", "tick": 3888}}
    """

    status: str
    database: str
    simulation: SimulationState

    @classmethod
    def from_api(cls, payload: Any) -> "Health":
        data = _require_mapping(payload, "Health")
        return cls(
            status=_text(data, "status", "unknown"),
            database=_text(data, "database", "unknown"),
            simulation=SimulationState.from_api(data.get("simulation") or {}),
        )

    @property
    def is_healthy(self) -> bool:
        return self.status == "ok" and self.database == "ok"


@dataclass(frozen=True, slots=True)
class SimInstance:
    """``GET /v1/instance``::

        {"id": 1, "scenario_id": "baseline", "scenario_version": "1.0",
         "seed": 12345, "sim_time": "2026-02-10T12:00:00", "tick": 3888,
         "tick_minutes": 15, "status": "RUNNING"}
    """

    id: int
    scenario_id: str
    scenario_version: str
    seed: int
    sim_time: str
    tick: int
    tick_minutes: int
    status: str

    @classmethod
    def from_api(cls, payload: Any) -> "SimInstance":
        data = _require_mapping(payload, "SimInstance")
        return cls(
            id=_integer(data, "id", model="SimInstance", default=0),
            scenario_id=_text(data, "scenario_id"),
            scenario_version=_text(data, "scenario_version"),
            seed=_integer(data, "seed", model="SimInstance", default=0),
            sim_time=_text(data, "sim_time"),
            tick=_integer(data, "tick", model="SimInstance", default=0),
            tick_minutes=_integer(data, "tick_minutes", model="SimInstance", default=0),
            status=to_enum(InstanceStatus, data.get("status"), field_name="status")
            or "UNKNOWN",
        )

    @property
    def is_running(self) -> bool:
        return self.status == InstanceStatus.RUNNING


@dataclass(frozen=True, slots=True)
class Metrics:
    """``GET /v1/metrics`` under three decimals of rounding::

        {"served_demand_liters": 90900.0, "unmet_demand_liters": 3680414.477,
         "service_level": 0.024103, "allocation_liters": 5000.0,
         "allocation_failures": 0}
    """

    served_demand_liters: float
    unmet_demand_liters: float
    service_level: float
    allocation_liters: float
    allocation_failures: int

    @classmethod
    def from_api(cls, payload: Any) -> "Metrics":
        data = _require_mapping(payload, "Metrics")
        return cls(
            served_demand_liters=_number(
                data, "served_demand_liters", model="Metrics", default=0.0
            ),
            unmet_demand_liters=_number(
                data, "unmet_demand_liters", model="Metrics", default=0.0
            ),
            service_level=_number(data, "service_level", model="Metrics", default=0.0),
            allocation_liters=_number(
                data, "allocation_liters", model="Metrics", default=0.0
            ),
            allocation_failures=_integer(
                data, "allocation_failures", model="Metrics", default=0
            ),
        )


@dataclass(frozen=True, slots=True)
class Region:
    """``{"id": "region-dhaka", "name": "Dhaka Division", "demand_factor": 1.0}``"""

    id: str
    name: str
    demand_factor: float

    @classmethod
    def from_api(cls, payload: Any) -> "Region":
        data = _require_mapping(payload, "Region")
        return cls(
            id=_text(data, "id"),
            name=_text(data, "name"),
            demand_factor=_number(data, "demand_factor", model="Region", default=1.0),
        )


@dataclass(frozen=True, slots=True)
class Depot:
    """``GET /v1/depots``::

        {"id": "depot-gazipur", "name": "Gazipur Depot",
         "region_id": "region-dhaka", "status": "OPEN",
         "dispatch_capacity_per_tick": 12000.0,
         "capacity": {"DIESEL": 90000, ...},
         "inventory": {"DIESEL": 85000.0, ...}}
    """

    id: str
    name: str
    region_id: str
    status: str
    dispatch_capacity_per_tick: float
    capacity: dict[str, float]
    inventory: dict[str, float]

    @classmethod
    def from_api(cls, payload: Any) -> "Depot":
        data = _require_mapping(payload, "Depot")
        return cls(
            id=_text(data, "id"),
            name=_text(data, "name"),
            region_id=_text(data, "region_id"),
            status=to_enum(EntityStatus, data.get("status"), field_name="status")
            or "UNKNOWN",
            dispatch_capacity_per_tick=_number(
                data, "dispatch_capacity_per_tick", model="Depot", default=0.0
            ),
            capacity=_mapping_of_floats(data, "capacity", model="Depot"),
            inventory=_mapping_of_floats(data, "inventory", model="Depot"),
        )

    def inventory_for(self, fuel_type: str) -> float:
        return float(self.inventory.get(fuel_type, 0.0))

    def capacity_for(self, fuel_type: str) -> float:
        return float(self.capacity.get(fuel_type, 0.0))


@dataclass(frozen=True, slots=True)
class Station:
    """``GET /v1/stations``::

        {"id": "station-mirpur", "name": "Mirpur Fuel Station",
         "region_id": "region-dhaka", "status": "OPEN",
         "demand_profile": "urban_high", "demand_multiplier": 1.0,
         "capacity": {"DIESEL": 15000, ...}, "inventory": {...}}
    """

    id: str
    name: str
    region_id: str
    status: str
    demand_profile: str
    demand_multiplier: float
    capacity: dict[str, float]
    inventory: dict[str, float]

    @classmethod
    def from_api(cls, payload: Any) -> "Station":
        data = _require_mapping(payload, "Station")
        return cls(
            id=_text(data, "id"),
            name=_text(data, "name"),
            region_id=_text(data, "region_id"),
            status=to_enum(EntityStatus, data.get("status"), field_name="status")
            or "UNKNOWN",
            demand_profile=to_enum(
                DemandProfile, data.get("demand_profile"), field_name="demand_profile"
            )
            or "unknown",
            demand_multiplier=_number(
                data, "demand_multiplier", model="Station", default=1.0
            ),
            capacity=_mapping_of_floats(data, "capacity", model="Station"),
            inventory=_mapping_of_floats(data, "inventory", model="Station"),
        )

    def inventory_for(self, fuel_type: str) -> float:
        return float(self.inventory.get(fuel_type, 0.0))

    def capacity_for(self, fuel_type: str) -> float:
        return float(self.capacity.get(fuel_type, 0.0))


@dataclass(frozen=True, slots=True)
class Route:
    """``GET /v1/routes``::

        {"id": "route-gazipur-mirpur", "source_depot_id": "depot-gazipur",
         "destination_station_id": "station-mirpur", "transit_ticks": 2,
         "max_shipment": 7000.0, "status": "AVAILABLE"}
    """

    id: str
    source_depot_id: str
    destination_station_id: str
    transit_ticks: int
    max_shipment: float
    status: str

    @classmethod
    def from_api(cls, payload: Any) -> "Route":
        data = _require_mapping(payload, "Route")
        return cls(
            id=_text(data, "id"),
            source_depot_id=_text(data, "source_depot_id"),
            destination_station_id=_text(data, "destination_station_id"),
            transit_ticks=_integer(data, "transit_ticks", model="Route", default=0),
            max_shipment=_number(data, "max_shipment", model="Route", default=0.0),
            status=to_enum(RouteStatus, data.get("status"), field_name="status")
            or "UNKNOWN",
        )

    @property
    def is_available(self) -> bool:
        """The decision engine must never route over a DISRUPTED route."""
        return self.status == RouteStatus.AVAILABLE


@dataclass(frozen=True, slots=True)
class SupplyArrival:
    """``GET /v1/supply-arrivals``::

        {"id": "supply-001", "depot_id": "depot-gazipur", "fuel_type": "DIESEL",
         "quantity": 18000.0, "planned_tick": 12, "actual_tick": 12,
         "status": "ARRIVED"}
    """

    id: str
    depot_id: str
    fuel_type: str
    quantity: float
    planned_tick: int
    actual_tick: int | None
    status: str

    @classmethod
    def from_api(cls, payload: Any) -> "SupplyArrival":
        data = _require_mapping(payload, "SupplyArrival")
        return cls(
            id=_text(data, "id"),
            depot_id=_text(data, "depot_id"),
            fuel_type=to_enum(FuelType, data.get("fuel_type"), field_name="fuel_type")
            or "UNKNOWN",
            quantity=_number(data, "quantity", model="SupplyArrival", default=0.0),
            planned_tick=_integer(
                data, "planned_tick", model="SupplyArrival", default=0
            ),
            actual_tick=_optional_integer(data, "actual_tick", model="SupplyArrival"),
            status=to_enum(SupplyStatus, data.get("status"), field_name="status")
            or "UNKNOWN",
        )


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """``GET /v1/events``::

        {"id": 1, "type": "demand_spike", "start_tick": 500, "end_tick": 520,
         "status": "RESOLVED", "parameters": {"region_id": "region-dhaka",
                                              "factor": 1.5}}
    """

    id: int
    type: str
    start_tick: int
    end_tick: int | None
    status: str
    parameters: dict[str, Any]

    @classmethod
    def from_api(cls, payload: Any) -> "DomainEvent":
        data = _require_mapping(payload, "DomainEvent")
        return cls(
            id=_integer(data, "id", model="DomainEvent", default=0),
            type=to_enum(EventType, data.get("type"), field_name="type")
            or _text(data, "type"),
            start_tick=_integer(data, "start_tick", model="DomainEvent", default=0),
            end_tick=_optional_integer(data, "end_tick", model="DomainEvent"),
            status=to_enum(EventStatus, data.get("status"), field_name="status")
            or "UNKNOWN",
            parameters=_free_mapping(data, "parameters"),
        )

    @property
    def is_active(self) -> bool:
        return self.status == EventStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class Allocation:
    """``GET /v1/allocations``::

        {"id": 1, "idempotency_key": "smoke-verify-001",
         "source_depot_id": "depot-gazipur",
         "destination_station_id": "station-mirpur",
         "route_id": "route-gazipur-mirpur", "fuel_type": "DIESEL",
         "quantity": 5000.0, "created_tick": 480, "departure_tick": 480,
         "expected_arrival_tick": 482, "actual_arrival_tick": 482,
         "status": "ARRIVED", "failure_reason": null}
    """

    id: int
    idempotency_key: str
    source_depot_id: str
    destination_station_id: str
    route_id: str
    fuel_type: str
    quantity: float
    created_tick: int
    departure_tick: int | None
    expected_arrival_tick: int | None
    actual_arrival_tick: int | None
    status: str
    failure_reason: str | None

    @classmethod
    def from_api(cls, payload: Any) -> "Allocation":
        data = _require_mapping(payload, "Allocation")
        return cls(
            id=_integer(data, "id", model="Allocation", default=0),
            idempotency_key=_text(data, "idempotency_key"),
            source_depot_id=_text(data, "source_depot_id"),
            destination_station_id=_text(data, "destination_station_id"),
            route_id=_text(data, "route_id"),
            fuel_type=to_enum(FuelType, data.get("fuel_type"), field_name="fuel_type")
            or "UNKNOWN",
            quantity=_number(data, "quantity", model="Allocation", default=0.0),
            created_tick=_integer(data, "created_tick", model="Allocation", default=0),
            departure_tick=_optional_integer(
                data, "departure_tick", model="Allocation"
            ),
            expected_arrival_tick=_optional_integer(
                data, "expected_arrival_tick", model="Allocation"
            ),
            actual_arrival_tick=_optional_integer(
                data, "actual_arrival_tick", model="Allocation"
            ),
            status=to_enum(AllocationStatus, data.get("status"), field_name="status")
            or "UNKNOWN",
            failure_reason=_optional_text(data, "failure_reason"),
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            AllocationStatus.ARRIVED,
            AllocationStatus.FAILED,
            AllocationStatus.CANCELLED,
        )


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """``GET /admin/audit``::

        {"id": 4454, "wall_time": "2026-09-29T05:05:41.529952",
         "sim_time": "2026-02-16T02:00:00", "tick": 4424,
         "action": "simulation.tick", "entity_type": "instance",
         "entity_id": "1", "result": "OK", "metadata_json": {"tick": 4424}}
    """

    id: int
    wall_time: str
    sim_time: str
    tick: int
    action: str
    entity_type: str
    entity_id: str
    result: str
    metadata_json: dict[str, Any]

    @classmethod
    def from_api(cls, payload: Any) -> "AuditEntry":
        data = _require_mapping(payload, "AuditEntry")
        return cls(
            id=_integer(data, "id", model="AuditEntry", default=0),
            wall_time=_text(data, "wall_time"),
            sim_time=_text(data, "sim_time"),
            tick=_integer(data, "tick", model="AuditEntry", default=0),
            action=_text(data, "action"),
            entity_type=_text(data, "entity_type"),
            entity_id=_text(data, "entity_id"),
            result=_text(data, "result"),
            metadata_json=_free_mapping(data, "metadata_json"),
        )


@dataclass(frozen=True, slots=True)
class DemandObservation:
    """``GET /v1/demand-history``::

        {"id": 49212, "station_id": "station-coxsbazar", "fuel_type": "OCTANE",
         "tick": 4100, "sim_time": "2026-02-12T17:00:00",
         "demand_liters": 53.255, "served_liters": 0.0, "unmet_liters": 53.255}

    **Contract gap.** ``DemandObservation`` appears in the ``SimulatorClient``
    signature (CONTRACT.md 5.4) but its fields are not pinned in 5.2. The fields
    below are the ones the live simulator actually returns; ``sim_time``,
    ``served_liters`` and ``unmet_liters`` are carried through for the API layer
    even though the forecasting contract only needs ``demand_liters``.
    """

    id: int
    station_id: str
    fuel_type: str
    tick: int
    sim_time: str
    demand_liters: float
    served_liters: float
    unmet_liters: float

    @classmethod
    def from_api(cls, payload: Any) -> "DemandObservation":
        data = _require_mapping(payload, "DemandObservation")
        return cls(
            id=_integer(data, "id", model="DemandObservation", default=0),
            station_id=_text(data, "station_id"),
            fuel_type=to_enum(FuelType, data.get("fuel_type"), field_name="fuel_type")
            or "UNKNOWN",
            tick=_integer(data, "tick", model="DemandObservation", default=0),
            sim_time=_text(data, "sim_time"),
            demand_liters=_number(
                data, "demand_liters", model="DemandObservation", default=0.0
            ),
            served_liters=_number(
                data, "served_liters", model="DemandObservation", default=0.0
            ),
            unmet_liters=_number(
                data, "unmet_liters", model="DemandObservation", default=0.0
            ),
        )

    @property
    def liters(self) -> float:
        """Alias so an observation projects onto :class:`DemandPoint`."""
        return self.demand_liters

    def to_demand_point(self) -> "DemandPoint":
        return DemandPoint(
            station_id=self.station_id,
            fuel_type=self.fuel_type,
            tick=self.tick,
            liters=self.demand_liters,
        )


@dataclass(frozen=True, slots=True)
class Fault:
    """``POST /admin/faults`` and ``GET /admin/faults``::

        {"id": 1, "type": "latency",
         "start_wall_time": "2026-09-29T05:05:55.143004+00:00",
         "end_wall_time": "2026-09-29T05:05:56.143004+00:00",
         "active": true, "parameters": {}}

    **Contract gap.** ``Fault`` is named in the client signature but not pinned
    in 5.2. Observed live (a one-second ``latency`` fault, immediately cleared).
    Note the wire mixes an offset-suffixed timestamp on create with a naive one
    on list, so both are kept as raw strings rather than parsed.
    """

    id: int
    type: str
    start_wall_time: str
    end_wall_time: str
    active: bool
    parameters: dict[str, Any]

    @classmethod
    def from_api(cls, payload: Any) -> "Fault":
        data = _require_mapping(payload, "Fault")
        return cls(
            id=_integer(data, "id", model="Fault", default=0),
            type=to_enum(FaultType, data.get("type"), field_name="type")
            or _text(data, "type"),
            start_wall_time=_text(data, "start_wall_time"),
            end_wall_time=_text(data, "end_wall_time"),
            active=bool(data.get("active", False)),
            parameters=_free_mapping(data, "parameters"),
        )


# --------------------------------------------------------------------------- #
# Request bodies (POST payloads, from the simulator's own /openapi.json)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AllocationRequest:
    """Body of ``POST /v1/allocations`` (live ``AllocationCreate`` schema).

    ``idempotency_key`` is required by the simulator and is what makes a retry
    after a timeout safe. CONTRACT.md 5.3: a replay is de-duplicated, but comes
    back as **201**, not the 200 the integration guide documents.
    """

    idempotency_key: str
    source_depot_id: str
    destination_station_id: str
    route_id: str
    fuel_type: str
    quantity: float

    def to_payload(self) -> dict[str, Any]:
        return {
            "idempotency_key": self.idempotency_key,
            "source_depot_id": self.source_depot_id,
            "destination_station_id": self.destination_station_id,
            "route_id": self.route_id,
            "fuel_type": wire_value(self.fuel_type),
            "quantity": float(self.quantity),
        }


@dataclass(frozen=True, slots=True)
class EventRequest:
    """Body of ``POST /admin/events`` (live ``EventCreate`` schema)."""

    type: str
    start_tick: int
    duration_ticks: int
    parameters: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "type": wire_value(self.type),
            "start_tick": int(self.start_tick),
            "duration_ticks": int(self.duration_ticks),
            "parameters": dict(self.parameters),
        }


@dataclass(frozen=True, slots=True)
class FaultRequest:
    """Body of ``POST /admin/faults`` (live ``FaultCreate`` schema)."""

    type: str
    duration_seconds: int
    parameters: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "type": wire_value(self.type),
            "duration_seconds": int(self.duration_seconds),
            "parameters": dict(self.parameters),
        }


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #

#: Event names the simulator is known to emit (CONTRACT.md 5.4). An unrecognised
#: name is *not* an error -- it is still a hint that something changed.
STREAM_EVENT_NAMES = frozenset(
    {
        "simulation.tick",
        "allocation.status_changed",
        "inventory.updated",
        "simulator.notice",
    }
)


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One Server-Sent Event off ``GET /v1/stream``.

    **This is a hint, never a fact.** ``data`` is whatever the simulator put on
    the wire, kept as an opaque dict. Nothing in this platform may compute a
    number from it: a dropped, duplicated or reordered event has to be able to
    make a figure *late*, never *wrong*. Every consumer responds to an event by
    re-fetching REST, which is the source of truth (CONTRACT.md 5.5).
    """

    name: str
    data: dict[str, Any]
    received_at: float

    @property
    def is_known(self) -> bool:
        return self.name in STREAM_EVENT_NAMES


# --------------------------------------------------------------------------- #
# Shared composite types (CONTRACT.md 5.2 -- consumed by A3, A4, A5, A6, A8)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DemandPoint:
    """One (station, fuel, tick) demand observation. Pinned by CONTRACT.md 5.2.

    Deliberately four fields and no more: the forecasting engine (A4) and the
    detector (A5) are pure functions of a ``Sequence[DemandPoint]``.
    """

    station_id: str
    fuel_type: str
    tick: int
    liters: float


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A consistent view of the simulated network. Pinned by CONTRACT.md 5.2.

    ``stale`` and ``age_seconds`` are the platform's *only* way of saying "this
    is the last good answer, not a fresh one" -- there is deliberately no path
    that invents a number when the simulator is unreachable (brief section 11).
    """

    taken_at: float
    tick: int
    sim_time: str
    status: str
    depots: tuple[Depot, ...]
    stations: tuple[Station, ...]
    routes: tuple[Route, ...]
    regions: tuple[Region, ...]
    supply_arrivals: tuple[SupplyArrival, ...]
    events: tuple[DomainEvent, ...]
    metrics: Metrics
    stale: bool = False
    age_seconds: float = 0.0

    @classmethod
    def assemble(
        cls,
        *,
        instance: SimInstance,
        depots: Iterable[Depot] = (),
        stations: Iterable[Station] = (),
        routes: Iterable[Route] = (),
        regions: Iterable[Region] = (),
        supply_arrivals: Iterable[SupplyArrival] = (),
        events: Iterable[DomainEvent] = (),
        metrics: Metrics,
        taken_at: float | None = None,
        stale: bool = False,
        age_seconds: float = 0.0,
    ) -> "Snapshot":
        """Build a snapshot from the per-endpoint reads.

        Part of this workstream because ``Snapshot`` is owned here and somebody
        has to assemble it; the client's ``build_snapshot`` is the cache-aware
        caller. Everything is frozen into tuples so a snapshot cannot be mutated
        after it has been handed to the intelligence engines.
        """
        return cls(
            taken_at=time.monotonic() if taken_at is None else float(taken_at),
            tick=instance.tick,
            sim_time=instance.sim_time,
            status=instance.status,
            depots=tuple(depots),
            stations=tuple(stations),
            routes=tuple(routes),
            regions=tuple(regions),
            supply_arrivals=tuple(supply_arrivals),
            events=tuple(events),
            metrics=metrics,
            stale=stale,
            age_seconds=float(age_seconds),
        )

    @property
    def is_degraded(self) -> bool:
        return self.stale

    def as_stale(self, *, age_seconds: float) -> "Snapshot":
        """Same data, honestly relabelled as old."""
        return replace(self, stale=True, age_seconds=float(age_seconds))

    def stations_in(self, region_id: str) -> tuple[Station, ...]:
        return tuple(s for s in self.stations if s.region_id == region_id)

    def depots_in(self, region_id: str) -> tuple[Depot, ...]:
        return tuple(d for d in self.depots if d.region_id == region_id)

    def routes_to(self, station_id: str) -> tuple[Route, ...]:
        return tuple(r for r in self.routes if r.destination_station_id == station_id)
