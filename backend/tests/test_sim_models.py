"""Model tests (CONTRACT.md 5.2).

Payloads are the live simulator's own responses, copied from
``http://localhost:8001``. The tolerance rules get as much attention as the
happy path: brief section 10 says the organisers may change the environment
mid-event, so an unrecognised enum value must be logged and carried, never
raised.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest

from app.sim import models as m
from app.sim.models import (
    STREAM_EVENT_NAMES,
    Allocation,
    AllocationRequest,
    AuditEntry,
    DemandObservation,
    DemandPoint,
    Depot,
    DomainEvent,
    EntityStatus,
    EventRequest,
    Fault,
    FaultRequest,
    FuelType,
    Health,
    InstanceStatus,
    Metrics,
    Region,
    Route,
    SimInstance,
    Snapshot,
    Station,
    StreamEvent,
    SupplyArrival,
    SupplyStatus,
    to_enum,
)

# --------------------------------------------------------------------------- #
# Live payloads
# --------------------------------------------------------------------------- #

HEALTH = {"status": "ok", "database": "ok", "simulation": {"status": "RUNNING", "tick": 3888}}

INSTANCE = {
    "id": 1,
    "scenario_id": "baseline",
    "scenario_version": "1.0",
    "seed": 12345,
    "sim_time": "2026-02-10T12:00:00",
    "tick": 3888,
    "tick_minutes": 15,
    "status": "RUNNING",
}

METRICS = {
    "served_demand_liters": 90900.0,
    "unmet_demand_liters": 3680414.477,
    "service_level": 0.024103,
    "allocation_liters": 5000.0,
    "allocation_failures": 0,
}

REGION = {"id": "region-dhaka", "name": "Dhaka Division", "demand_factor": 1.0}

DEPOT = {
    "id": "depot-gazipur",
    "name": "Gazipur Depot",
    "region_id": "region-dhaka",
    "status": "OPEN",
    "dispatch_capacity_per_tick": 12000.0,
    "capacity": {"DIESEL": 90000, "PETROL": 70000, "OCTANE": 45000},
    "inventory": {"DIESEL": 85000.0, "PETROL": 70000.0, "OCTANE": 45000.0},
}

STATION = {
    "id": "station-mirpur",
    "name": "Mirpur Fuel Station",
    "region_id": "region-dhaka",
    "status": "OPEN",
    "demand_profile": "urban_high",
    "demand_multiplier": 1.0,
    "capacity": {"DIESEL": 15000, "PETROL": 14000, "OCTANE": 9000},
    "inventory": {"DIESEL": 0, "PETROL": 0, "OCTANE": 0},
}

ROUTE = {
    "id": "route-gazipur-mirpur",
    "source_depot_id": "depot-gazipur",
    "destination_station_id": "station-mirpur",
    "transit_ticks": 2,
    "max_shipment": 7000.0,
    "status": "AVAILABLE",
}

SUPPLY_ARRIVAL = {
    "id": "supply-001",
    "depot_id": "depot-gazipur",
    "fuel_type": "DIESEL",
    "quantity": 18000.0,
    "planned_tick": 12,
    "actual_tick": 12,
    "status": "ARRIVED",
}

DOMAIN_EVENT = {
    "id": 1,
    "type": "demand_spike",
    "start_tick": 500,
    "end_tick": 520,
    "status": "RESOLVED",
    "parameters": {"region_id": "region-dhaka", "factor": 1.5},
}

ALLOCATION = {
    "id": 1,
    "idempotency_key": "smoke-verify-001",
    "source_depot_id": "depot-gazipur",
    "destination_station_id": "station-mirpur",
    "route_id": "route-gazipur-mirpur",
    "fuel_type": "DIESEL",
    "quantity": 5000.0,
    "created_tick": 480,
    "departure_tick": 480,
    "expected_arrival_tick": 482,
    "actual_arrival_tick": 482,
    "status": "ARRIVED",
    "failure_reason": None,
}

AUDIT_ENTRY = {
    "id": 4454,
    "wall_time": "2026-09-29T05:05:41.529952",
    "sim_time": "2026-02-16T02:00:00",
    "tick": 4424,
    "action": "simulation.tick",
    "entity_type": "instance",
    "entity_id": "1",
    "result": "OK",
    "metadata_json": {"tick": 4424},
}

DEMAND_OBSERVATION = {
    "id": 49212,
    "station_id": "station-coxsbazar",
    "fuel_type": "OCTANE",
    "tick": 4100,
    "sim_time": "2026-02-12T17:00:00",
    "demand_liters": 53.255,
    "served_liters": 0.0,
    "unmet_liters": 53.255,
}

FAULT = {
    "id": 1,
    "type": "latency",
    "start_wall_time": "2026-09-29T05:05:55.143004+00:00",
    "end_wall_time": "2026-09-29T05:05:56.143004+00:00",
    "active": True,
    "parameters": {},
}


# --------------------------------------------------------------------------- #
# Happy paths -- exact field names
# --------------------------------------------------------------------------- #


def test_health():
    health = Health.from_api(HEALTH)
    assert health.status == "ok"
    assert health.database == "ok"
    assert health.simulation.status == InstanceStatus.RUNNING
    assert health.simulation.tick == 3888
    assert health.is_healthy is True


def test_instance():
    instance = SimInstance.from_api(INSTANCE)
    assert instance.id == 1
    assert instance.scenario_id == "baseline"
    assert instance.scenario_version == "1.0"
    assert instance.seed == 12345
    assert instance.sim_time == "2026-02-10T12:00:00"
    assert instance.tick == 3888
    assert instance.tick_minutes == 15
    assert instance.status == InstanceStatus.RUNNING
    assert instance.is_running is True


def test_metrics():
    metrics = Metrics.from_api(METRICS)
    assert metrics.served_demand_liters == 90900.0
    assert metrics.unmet_demand_liters == 3680414.477
    assert metrics.service_level == 0.024103
    assert metrics.allocation_liters == 5000.0
    assert metrics.allocation_failures == 0


def test_region():
    region = Region.from_api(REGION)
    assert (region.id, region.name, region.demand_factor) == (
        "region-dhaka",
        "Dhaka Division",
        1.0,
    )


def test_depot():
    depot = Depot.from_api(DEPOT)
    assert depot.id == "depot-gazipur"
    assert depot.status == EntityStatus.OPEN
    assert depot.dispatch_capacity_per_tick == 12000.0
    # Integers on the wire for capacity, floats for inventory; both become float.
    assert depot.capacity == {"DIESEL": 90000.0, "PETROL": 70000.0, "OCTANE": 45000.0}
    assert all(isinstance(v, float) for v in depot.capacity.values())
    assert depot.inventory_for("DIESEL") == 85000.0
    assert depot.inventory_for("MISSING") == 0.0
    assert depot.capacity_for("OCTANE") == 45000.0


def test_station():
    station = Station.from_api(STATION)
    assert station.id == "station-mirpur"
    assert station.status == EntityStatus.OPEN
    assert station.demand_profile == "urban_high"
    assert station.demand_multiplier == 1.0
    assert station.inventory_for("DIESEL") == 0.0
    assert station.capacity_for("DIESEL") == 15000.0


def test_route():
    route = Route.from_api(ROUTE)
    assert route.source_depot_id == "depot-gazipur"
    assert route.destination_station_id == "station-mirpur"
    assert route.transit_ticks == 2
    assert route.max_shipment == 7000.0
    assert route.is_available is True


def test_route_disrupted_is_not_available():
    route = Route.from_api({**ROUTE, "status": "DISRUPTED"})
    assert route.is_available is False


def test_supply_arrival():
    arrival = SupplyArrival.from_api(SUPPLY_ARRIVAL)
    assert arrival.depot_id == "depot-gazipur"
    assert arrival.fuel_type == FuelType.DIESEL
    assert arrival.quantity == 18000.0
    assert arrival.planned_tick == 12
    assert arrival.actual_tick == 12
    assert arrival.status == SupplyStatus.ARRIVED


def test_supply_arrival_actual_tick_null_until_it_lands():
    arrival = SupplyArrival.from_api({**SUPPLY_ARRIVAL, "actual_tick": None, "status": "SCHEDULED"})
    assert arrival.actual_tick is None
    assert arrival.status == SupplyStatus.SCHEDULED


def test_domain_event():
    event = DomainEvent.from_api(DOMAIN_EVENT)
    assert event.id == 1
    assert event.type == "demand_spike"
    assert event.start_tick == 500
    assert event.end_tick == 520
    assert event.status == "RESOLVED"
    assert event.parameters == {"region_id": "region-dhaka", "factor": 1.5}
    assert event.is_active is False


def test_allocation():
    allocation = Allocation.from_api(ALLOCATION)
    assert allocation.idempotency_key == "smoke-verify-001"
    assert allocation.route_id == "route-gazipur-mirpur"
    assert allocation.quantity == 5000.0
    assert allocation.created_tick == 480
    assert allocation.expected_arrival_tick == 482
    assert allocation.failure_reason is None
    assert allocation.status == "ARRIVED"
    assert allocation.is_terminal is True


def test_allocation_in_flight_has_no_arrival_yet():
    allocation = Allocation.from_api(
        {
            **ALLOCATION,
            "status": "IN_TRANSIT",
            "actual_arrival_tick": None,
            "departure_tick": 481,
        }
    )
    assert allocation.actual_arrival_tick is None
    assert allocation.departure_tick == 481
    assert allocation.is_terminal is False


def test_audit_entry():
    entry = AuditEntry.from_api(AUDIT_ENTRY)
    assert entry.id == 4454
    assert entry.wall_time == "2026-09-29T05:05:41.529952"
    assert entry.tick == 4424
    assert entry.action == "simulation.tick"
    assert entry.entity_type == "instance"
    assert entry.entity_id == "1"
    assert entry.result == "OK"
    assert entry.metadata_json == {"tick": 4424}


def test_demand_observation():
    obs = DemandObservation.from_api(DEMAND_OBSERVATION)
    assert obs.station_id == "station-coxsbazar"
    assert obs.fuel_type == FuelType.OCTANE
    assert obs.tick == 4100
    assert obs.sim_time == "2026-02-12T17:00:00"
    assert obs.demand_liters == 53.255
    assert obs.served_liters == 0.0
    assert obs.unmet_liters == 53.255
    assert obs.liters == 53.255


def test_demand_observation_projects_onto_demand_point():
    obs = DemandObservation.from_api(DEMAND_OBSERVATION)
    point = obs.to_demand_point()
    assert point == DemandPoint(
        station_id="station-coxsbazar",
        fuel_type="OCTANE",
        tick=4100,
        liters=53.255,
    )


def test_fault():
    fault = Fault.from_api(FAULT)
    assert fault.id == 1
    assert fault.type == "latency"
    assert fault.active is True
    assert fault.parameters == {}
    assert fault.end_wall_time.startswith("2026-09-29T05:05:56")


# --------------------------------------------------------------------------- #
# Unknown enum values are tolerated, never fatal (brief section 10)
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _fresh_unknown_tracking():
    m.reset_unknown_value_tracking()
    yield
    m.reset_unknown_value_tracking()


def test_unknown_depot_status_is_tolerated():
    """A status we have never seen must not break a request."""
    depot = Depot.from_api({**DEPOT, "status": "BESIEGED"})
    assert depot.status == "BESIEGED"
    assert depot.id == "depot-gazipur"


def test_unknown_station_status_and_profile_are_tolerated():
    station = Station.from_api(
        {**STATION, "status": "MELTING", "demand_profile": "orbital"}
    )
    assert station.status == "MELTING"
    assert station.demand_profile == "orbital"
    # The rest of the record still parsed.
    assert station.capacity_for("DIESEL") == 15000.0


def test_unknown_instance_status_is_tolerated():
    instance = SimInstance.from_api({**INSTANCE, "status": "TRANSCENDENT"})
    assert instance.status == "TRANSCENDENT"
    assert instance.is_running is False


def test_unknown_route_allocation_supply_and_event_status_are_tolerated():
    assert Route.from_api({**ROUTE, "status": "HAUNTED"}).status == "HAUNTED"
    assert Allocation.from_api({**ALLOCATION, "status": "TELEPORTING"}).status == "TELEPORTING"
    assert SupplyArrival.from_api({**SUPPLY_ARRIVAL, "status": "LOST"}).status == "LOST"
    assert DomainEvent.from_api({**DOMAIN_EVENT, "status": "SIMMERING"}).status == "SIMMERING"
    assert Allocation.from_api({**ALLOCATION, "fuel_type": "PLASMA"}).fuel_type == "PLASMA"


def test_unknown_health_simulation_status_is_tolerated():
    health = Health.from_api(
        {"status": "ok", "database": "ok", "simulation": {"status": "CATAWAMPUS", "tick": 7}}
    )
    assert health.simulation.status == "CATAWAMPUS"
    assert health.simulation.tick == 7


def test_unknown_value_is_logged_as_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="app.sim.models"):
        Depot.from_api({**DEPOT, "status": "BESIEGED"})

    records = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "app.sim.models"
    ]
    assert records, "an unrecognised enum value must produce a log line"
    assert "status" in records[0].getMessage()
    assert getattr(records[0], "value", None) == "BESIEGED"
    assert getattr(records[0], "event", None) == "sim.unknown_enum"


def test_known_values_are_not_logged_as_unknown(caplog):
    with caplog.at_level(logging.WARNING, logger="app.sim.models"):
        Depot.from_api(DEPOT)
    assert not [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "app.sim.models"
    ]


def test_unknown_enum_member_still_compares_equal_to_its_wire_string():
    """The tolerance is only useful if downstream code keeps working."""
    depot = Depot.from_api({**DEPOT, "status": "BESIEGED"})
    assert depot.status == "BESIEGED"
    known = Depot.from_api(DEPOT)
    assert known.status == "OPEN"
    assert known.status == EntityStatus.OPEN
    assert known.status in {"OPEN", "CONSTRAINED", "OUTAGE"}


def test_to_enum_returns_none_for_none_and_passes_through_members():
    assert to_enum(FuelType, None) is None
    assert to_enum(FuelType, FuelType.DIESEL) is FuelType.DIESEL
    assert to_enum(FuelType, "DIESEL") is FuelType.DIESEL


def test_to_enum_tolerates_a_non_string_value():
    assert to_enum(FuelType, 7) == 7


# --------------------------------------------------------------------------- #
# Shape is validated, values are tolerated
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "model,payload",
    [
        (Health, [{"status": "ok"}]),
        (SimInstance, "RUNNING"),
        (Depot, []),
        (Station, 42),
        (Route, None),
        (Allocation, []),
        (DemandObservation, ["not", "an", "object"]),
        (Metrics, []),
    ],
)
def test_a_list_where_an_object_belongs_is_a_real_error(model, payload):
    """Same dict-before-list discipline as the error normaliser (5.3)."""
    with pytest.raises(ValueError):
        model.from_api(payload)


def test_a_wrong_type_where_a_number_belongs_raises_value_error():
    with pytest.raises(ValueError, match="quantity"):
        Allocation.from_api({**ALLOCATION, "quantity": "lots"})


def test_missing_fields_fall_back_to_safe_defaults():
    """A sparse payload yields a usable object rather than an exception."""
    depot = Depot.from_api({"id": "depot-nowhere"})
    assert depot.id == "depot-nowhere"
    assert depot.name == ""
    assert depot.status == "UNKNOWN"
    assert depot.dispatch_capacity_per_tick == 0.0
    assert depot.capacity == {}
    assert depot.inventory == {}

    allocation = Allocation.from_api({})
    assert allocation.id == 0
    assert allocation.quantity == 0.0
    assert allocation.departure_tick is None
    assert allocation.failure_reason is None


def test_a_non_mapping_capacity_block_is_rejected():
    with pytest.raises(ValueError, match="capacity"):
        Depot.from_api({**DEPOT, "capacity": ["DIESEL", 90000]})


def test_a_non_numeric_capacity_value_is_rejected():
    with pytest.raises(ValueError, match="capacity"):
        Depot.from_api({**DEPOT, "capacity": {"DIESEL": "plenty"}})


def test_extra_unknown_fields_are_ignored():
    depot = Depot.from_api({**DEPOT, "brand_new_field": {"nested": True}})
    assert depot.id == "depot-gazipur"


# --------------------------------------------------------------------------- #
# Immutability
# --------------------------------------------------------------------------- #


def test_demand_point_is_frozen():
    point = DemandPoint(station_id="s", fuel_type="DIESEL", tick=1, liters=2.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        point.liters = 3.0  # type: ignore[misc]


def test_snapshot_is_frozen():
    snapshot = _sample_snapshot()
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.tick = 99  # type: ignore[misc]


def test_models_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        Depot.from_api(DEPOT).status = "OUTAGE"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #


def _sample_snapshot() -> Snapshot:
    return Snapshot.assemble(
        instance=SimInstance.from_api(INSTANCE),
        depots=[Depot.from_api(DEPOT)],
        stations=[Station.from_api(STATION)],
        routes=[Route.from_api(ROUTE)],
        regions=[Region.from_api(REGION)],
        supply_arrivals=[SupplyArrival.from_api(SUPPLY_ARRIVAL)],
        events=[DomainEvent.from_api(DOMAIN_EVENT)],
        metrics=Metrics.from_api(METRICS),
        taken_at=1234.5,
    )


def test_snapshot_assemble_maps_the_instance_and_freezes_collections():
    snapshot = _sample_snapshot()
    assert snapshot.tick == 3888
    assert snapshot.sim_time == "2026-02-10T12:00:00"
    assert snapshot.status == InstanceStatus.RUNNING
    assert snapshot.taken_at == 1234.5
    assert isinstance(snapshot.depots, tuple)
    assert isinstance(snapshot.stations, tuple)
    assert isinstance(snapshot.events, tuple)
    assert len(snapshot.depots) == 1
    assert snapshot.metrics.service_level == 0.024103


def test_snapshot_defaults_are_fresh_not_stale():
    snapshot = _sample_snapshot()
    assert snapshot.stale is False
    assert snapshot.age_seconds == 0.0
    assert snapshot.is_degraded is False


def test_snapshot_as_stale_preserves_the_data_and_marks_the_age():
    snapshot = _sample_snapshot()
    stale = snapshot.as_stale(age_seconds=42.0)
    assert stale.stale is True
    assert stale.age_seconds == 42.0
    assert stale.is_degraded is True
    # The same underlying reading, not a copy that drifted.
    assert stale.depots == snapshot.depots
    assert stale.tick == snapshot.tick
    # The original is untouched -- these are frozen values.
    assert snapshot.stale is False


def test_snapshot_lookup_helpers():
    snapshot = _sample_snapshot()
    assert snapshot.stations_in("region-dhaka") == snapshot.stations
    assert snapshot.stations_in("region-nowhere") == ()
    assert snapshot.depots_in("region-dhaka") == snapshot.depots
    assert snapshot.routes_to("station-mirpur") == snapshot.routes
    assert snapshot.routes_to("station-nowhere") == ()


def test_snapshot_assemble_accepts_empty_collections():
    snapshot = Snapshot.assemble(
        instance=SimInstance.from_api(INSTANCE), metrics=Metrics.from_api(METRICS)
    )
    assert snapshot.depots == ()
    assert snapshot.stations == ()
    assert snapshot.events == ()


def test_snapshot_taken_at_defaults_to_the_monotonic_clock():
    snapshot = Snapshot.assemble(
        instance=SimInstance.from_api(INSTANCE), metrics=Metrics.from_api(METRICS)
    )
    assert snapshot.taken_at > 0


# --------------------------------------------------------------------------- #
# Request bodies and stream events
# --------------------------------------------------------------------------- #


def test_allocation_request_payload_matches_the_simulator_schema():
    req = AllocationRequest(
        idempotency_key="k-1",
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type=FuelType.DIESEL,
        quantity=5000,
    )
    assert req.to_payload() == {
        "idempotency_key": "k-1",
        "source_depot_id": "depot-gazipur",
        "destination_station_id": "station-mirpur",
        "route_id": "route-gazipur-mirpur",
        "fuel_type": "DIESEL",
        "quantity": 5000.0,
    }


def test_enum_members_serialise_to_their_wire_value_not_their_repr():
    """Regression guard.

    On Python 3.11+, ``str(FuelType.DIESEL)`` is ``"FuelType.DIESEL"``, not
    ``"DIESEL"`` -- a payload carrying that would be rejected by the simulator's
    own schema. Every request body must unwrap the enum value instead.
    """
    assert str(FuelType.DIESEL) == "FuelType.DIESEL", (
        "if this ever changes, the wire_value helper is still correct but this "
        "test's premise needs revisiting"
    )

    from app.sim.models import FaultType, wire_value

    assert wire_value(FuelType.DIESEL) == "DIESEL"
    assert wire_value(FaultType.ERROR_RATE) == "error_rate"
    assert wire_value("DIESEL") == "DIESEL"

    assert AllocationRequest(
        idempotency_key="k",
        source_depot_id="d",
        destination_station_id="s",
        route_id="r",
        fuel_type=FuelType.PETROL,
        quantity=1,
    ).to_payload()["fuel_type"] == "PETROL"

    from app.sim.models import EventType

    assert EventRequest(EventType.DEMAND_SPIKE, 1, 2).to_payload()["type"] == "demand_spike"
    assert FaultRequest(FaultType.LATENCY, 5).to_payload()["type"] == "latency"


def test_event_and_fault_request_payloads():
    assert EventRequest(
        type="demand_spike", start_tick=10, duration_ticks=5, parameters={"factor": 2}
    ).to_payload() == {
        "type": "demand_spike",
        "start_tick": 10,
        "duration_ticks": 5,
        "parameters": {"factor": 2},
    }
    assert FaultRequest(type="latency", duration_seconds=30).to_payload() == {
        "type": "latency",
        "duration_seconds": 30,
        "parameters": {},
    }


def test_stream_event_knows_the_documented_names():
    assert "simulation.tick" in STREAM_EVENT_NAMES
    assert "allocation.status_changed" in STREAM_EVENT_NAMES
    assert "inventory.updated" in STREAM_EVENT_NAMES
    assert "simulator.notice" in STREAM_EVENT_NAMES
    assert len(STREAM_EVENT_NAMES) == 4

    assert StreamEvent(name="simulation.tick", data={}, received_at=1.0).is_known is True
    # An unfamiliar event is still carried -- it is still a hint that something
    # changed, just not one we have a name for.
    assert StreamEvent(name="brand.new", data={}, received_at=1.0).is_known is False
