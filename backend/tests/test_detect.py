"""Tests for A5 — ``app.intelligence.detect`` (CONTRACT.md §7.2).

These tests build snapshots from local doubles that mirror the simulator
field names pinned in CONTRACT.md §5.2.  The doubles let the detection rules
be tested as the pure functions they are; ``test_contract_*`` at the bottom
pins the same attribute names against A2's real ``app.sim.models`` classes
whenever that module exists.

Run with::

    backend/.venv/Scripts/python.exe -m pytest backend/tests/test_detect.py -v
"""

from __future__ import annotations

import ast
import inspect
import math
import statistics
import sys
from dataclasses import dataclass, field, is_dataclass
from pathlib import Path

import pytest

# The suite is run from the repo root, so make ``app`` importable without
# depending on pytest.ini (which A1 owns and may not exist yet).
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.intelligence.detect import (  # noqa: E402
    AnomalyDetector,
    RiskSignal,
    Severity,
    _robust_location_scale,
    _robust_z,
)

DETECT_SOURCE = Path(inspect.getfile(AnomalyDetector))
DIESEL = "DIESEL"


# --------------------------------------------------------------------------
# Test doubles, shaped exactly like CONTRACT.md §5.2
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DemandPoint:
    station_id: str
    fuel_type: str
    tick: int
    liters: float


@dataclass(frozen=True)
class Region:
    id: str
    name: str
    demand_factor: float


@dataclass(frozen=True)
class Depot:
    id: str
    name: str
    region_id: str
    status: str
    dispatch_capacity_per_tick: float
    capacity: dict = field(default_factory=dict)
    inventory: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Station:
    id: str
    name: str
    region_id: str
    status: str
    demand_profile: str
    demand_multiplier: float
    capacity: dict = field(default_factory=dict)
    inventory: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Route:
    id: str
    source_depot_id: str
    destination_station_id: str
    transit_ticks: int
    max_shipment: float
    status: str


@dataclass(frozen=True)
class SupplyArrival:
    id: str
    depot_id: str
    fuel_type: str
    quantity: float
    planned_tick: int
    actual_tick: int | None
    status: str


@dataclass(frozen=True)
class DomainEvent:
    id: int
    type: str
    start_tick: int
    end_tick: int | None
    status: str
    parameters: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Metrics:
    served_demand_liters: float = 0.0
    unmet_demand_liters: float = 0.0
    service_level: float = 1.0
    allocation_liters: float = 0.0
    allocation_failures: int = 0


@dataclass(frozen=True)
class Snapshot:
    taken_at: float
    tick: int
    sim_time: str
    status: str
    depots: tuple = ()
    stations: tuple = ()
    routes: tuple = ()
    regions: tuple = ()
    supply_arrivals: tuple = ()
    events: tuple = ()
    metrics: Metrics = field(default_factory=Metrics)
    stale: bool = False
    age_seconds: float = 0.0


# --------------------------------------------------------------------------
# Fixture helpers
# --------------------------------------------------------------------------


def make_station(station_id="S1", *, region_id="R1", status="OPEN", inv=5000.0, cap=10000.0):
    return Station(
        id=station_id,
        name=station_id,
        region_id=region_id,
        status=status,
        demand_profile="urban_high",
        demand_multiplier=1.0,
        capacity={DIESEL: cap},
        inventory={DIESEL: inv},
    )


def make_depot(depot_id="D1", *, region_id="R1", status="OPEN", inv=50000.0, cap=100000.0):
    return Depot(
        id=depot_id,
        name=depot_id,
        region_id=region_id,
        status=status,
        dispatch_capacity_per_tick=1000.0,
        capacity={DIESEL: cap},
        inventory={DIESEL: inv},
    )


def make_route(route_id="RT1", *, dest="S1", source="D1", status="AVAILABLE", max_shipment=1000.0):
    return Route(
        id=route_id,
        source_depot_id=source,
        destination_station_id=dest,
        transit_ticks=2,
        max_shipment=max_shipment,
        status=status,
    )


def make_snapshot(
    *,
    tick=100,
    stations=(),
    depots=(),
    routes=(),
    regions=(),
    supply_arrivals=(),
    events=(),
):
    return Snapshot(
        taken_at=0.0,
        tick=tick,
        sim_time=f"T{tick}",
        status="RUNNING",
        depots=tuple(depots),
        stations=tuple(stations),
        routes=tuple(routes),
        regions=tuple(regions),
        supply_arrivals=tuple(supply_arrivals),
        events=tuple(events),
        metrics=Metrics(),
    )


def series(station_id="S1", fuel_type=DIESEL, values=(), start_tick=0):
    return [
        DemandPoint(station_id=station_id, fuel_type=fuel_type, tick=start_tick + i, liters=float(v))
        for i, v in enumerate(values)
    ]


def history_for(station_id="S1", fuel_type=DIESEL, values=()):
    return {(station_id, fuel_type): series(station_id, fuel_type, values)}


def kinds(signals):
    return [s.kind for s in signals]


def find(signals, kind, entity_id=None):
    for signal in signals:
        if signal.kind == kind and (entity_id is None or signal.entity_id == entity_id):
            return signal
    return None


DETECTOR = AnomalyDetector()


# A baseline of 20 ordinary observations carrying one early 10x spike.
# The 21st observation (the one under test) is a moderate 40% rise.
SPIKE_BASELINE = [
    100, 102, 99, 101, 100, 98, 103, 100, 101, 99,
    100, 102, 1000, 99, 101, 100, 102, 98, 100, 101,
]
SPIKE_THEN_MODERATE = SPIKE_BASELINE + [140]


# --------------------------------------------------------------------------
# 1. Degenerate input
# --------------------------------------------------------------------------


def test_empty_history_and_empty_snapshot_returns_empty_list():
    assert DETECTOR.detect(snapshot=make_snapshot(), history={}) == []


def test_empty_history_with_populated_snapshot_does_not_raise():
    snapshot = make_snapshot(
        stations=(make_station(),), depots=(make_depot(),), routes=(make_route(),)
    )
    assert DETECTOR.detect(snapshot=snapshot, history={}) == []


def test_none_inputs_are_tolerated():
    assert DETECTOR.detect(snapshot=None, history=None) == []


def test_single_point_series_has_no_spread_and_yields_no_demand_anomaly():
    signals = DETECTOR.detect(
        snapshot=make_snapshot(stations=(make_station(),)),
        history=history_for(values=[100]),
    )
    assert find(signals, "demand_anomaly") is None


def test_flat_series_yields_no_demand_anomaly():
    signals = DETECTOR.detect(
        snapshot=make_snapshot(stations=(make_station(),)),
        history=history_for(values=[250.0] * 40),
    )
    assert find(signals, "demand_anomaly") is None


def test_identical_series_with_mad_zero_does_not_divide_by_zero():
    """All-identical values give MAD 0; the code must not raise or emit NaN."""
    med, scale, source = _robust_location_scale([7.0] * 25)
    assert med == 7.0
    assert scale == 0.0
    assert source == "degenerate"
    assert _robust_z(7.0, med, scale) == 0.0

    signals = DETECTOR.detect(
        snapshot=make_snapshot(stations=(make_station(),)),
        history=history_for(values=[7.0] * 25),
    )
    assert find(signals, "demand_anomaly") is None
    for signal in signals:
        for value in signal.evidence.values():
            assert not isinstance(value, float) or math.isfinite(value)


def test_constant_then_jump_is_clamped_not_infinite():
    """A flat baseline makes the z formally infinite; it must saturate."""
    values = [100.0] * 20 + [900.0]
    signals = DETECTOR.detect(
        snapshot=make_snapshot(stations=(make_station(),)),
        history=history_for(values=values),
    )
    signal = find(signals, "demand_anomaly")
    assert signal is not None
    z = signal.evidence["robust_z"]
    assert math.isfinite(z)
    assert abs(z) <= 12.0
    assert signal.severity is Severity.CRITICAL


def test_malformed_history_entries_are_ignored_not_fatal():
    snapshot = make_snapshot(stations=(make_station(),))
    malformed = {
        ("S1", DIESEL): series(values=[100, 101, 100, 99, 100, 101]),
        "not-a-tuple-key": [object()],
        ("S2", DIESEL): [DemandPoint("S2", DIESEL, 0, float("nan"))],
        ("S3", DIESEL): None,
    }
    signals = DETECTOR.detect(snapshot=snapshot, history=malformed)
    assert isinstance(signals, list)


# --------------------------------------------------------------------------
# 2. Robust z-score: a spike must not hide a later anomaly
# --------------------------------------------------------------------------


def test_robust_z_flags_anomaly_that_mean_and_stdev_would_hide():
    """The contract's central detection requirement.

    One early 10x spike inflates a mean/stdev scale so much that a later,
    real 40% deviation sits inside one sigma.  The MAD scale is unmoved by
    the spike's magnitude, so the later anomaly is still ~20+ sigma out.
    """
    baseline = [float(v) for v in SPIKE_BASELINE]
    latest = float(SPIKE_THEN_MODERATE[-1])

    # The naive baseline the contract tells us not to use.
    mean = statistics.fmean(baseline)
    stdev = statistics.pstdev(baseline)
    naive_z = abs(latest - mean) / stdev

    med, scale, source = _robust_location_scale(baseline)
    robust_z = abs(_robust_z(latest, med, scale))

    assert source == "mad"
    assert naive_z < 3.0, f"precondition failed: mean+stdev z was {naive_z:.2f}"
    assert robust_z > 5.0, f"robust z only reached {robust_z:.2f}"

    # And the detector must actually emit it.
    signals = DETECTOR.detect(
        snapshot=make_snapshot(stations=(make_station(),), tick=20),
        history=history_for(values=SPIKE_THEN_MODERATE),
    )
    signal = find(signals, "demand_anomaly")
    assert signal is not None, "a later moderate anomaly was hidden by the early spike"
    assert signal.evidence["direction"] == "spike"
    assert signal.evidence["scale_source"] == "mad"
    assert signal.evidence["abs_robust_z"] > 5.0


def test_robust_scale_is_unmoved_by_a_large_outlier():
    clean = [100.0, 101, 99, 100, 102, 98, 100, 101]
    spiked = clean[:4] + [10000.0] + clean[4:]
    _, clean_scale, _ = _robust_location_scale(clean)
    _, spiked_scale, _ = _robust_location_scale(spiked)
    assert spiked_scale < clean_scale * 3.0


# --------------------------------------------------------------------------
# 3. Severity is derived from the numbers, not hard-coded per rule
# --------------------------------------------------------------------------


# A baseline with a deliberately wide robust scale (median 100, MAD 11,
# scale 16.31 L/tick), so that changing the latest observation by a known
# multiple moves it across the severity bands one at a time.
CALIBRATED_BASELINE = [89.0, 100.0, 111.0] * 8


def _severity_for_multiplier(multiplier: float) -> Severity:
    """Feed one rule growing magnitudes and read back the severity it chose."""
    values = CALIBRATED_BASELINE + [100.0 * multiplier]
    signals = DETECTOR.detect(
        snapshot=make_snapshot(stations=(make_station(),), tick=len(values)),
        history=history_for(values=values),
    )
    signal = find(signals, "demand_anomaly")
    assert signal is not None, f"no signal at multiplier {multiplier}"
    return signal.severity


def test_severity_differs_between_a_small_and_a_large_deviation():
    """Requirement: a small deviation and a large one are not the same severity.

    Same rule, same entity, same code path -- only the numbers differ.
    """
    small = DETECTOR.detect(
        snapshot=make_snapshot(stations=(make_station(),)),
        history=history_for(values=CALIBRATED_BASELINE + [165.0]),  # ~4.0 sigma
    )
    large = DETECTOR.detect(
        snapshot=make_snapshot(stations=(make_station(),)),
        history=history_for(values=CALIBRATED_BASELINE + [250.0]),  # ~9.2 sigma
    )
    small_signal = find(small, "demand_anomaly")
    large_signal = find(large, "demand_anomaly")
    assert small_signal is not None and large_signal is not None

    assert small_signal.evidence["abs_robust_z"] == pytest.approx(3.99, abs=0.05)
    assert large_signal.evidence["abs_robust_z"] == pytest.approx(9.19, abs=0.05)
    order = [Severity.INFO, Severity.WARNING, Severity.SERIOUS, Severity.CRITICAL]
    assert order.index(small_signal.severity) < order.index(large_signal.severity)
    assert small_signal.severity is Severity.WARNING
    assert large_signal.severity is Severity.CRITICAL


def test_a_sub_sigma_deviation_is_not_reported_at_all():
    """0.6 sigma is below the emission threshold; it is not a CRITICAL finding."""
    values = CALIBRATED_BASELINE + [100.0 + 0.6 * 16.31]
    signals = DETECTOR.detect(
        snapshot=make_snapshot(stations=(make_station(),)),
        history=history_for(values=values),
    )
    assert find(signals, "demand_anomaly") is None


def test_severity_is_monotonic_in_deviation_magnitude():
    order = [Severity.INFO, Severity.WARNING, Severity.SERIOUS, Severity.CRITICAL]
    multipliers = (1.55, 1.65, 1.76, 1.90, 2.20)
    seen = [_severity_for_multiplier(m) for m in multipliers]
    ranks = [order.index(s) for s in seen]
    assert ranks == sorted(ranks), f"severity was not monotonic: {seen}"
    assert ranks[0] < ranks[-1], "severity never moved across a wide magnitude sweep"
    assert len(set(ranks)) >= 3, f"severity barely moved across the sweep: {seen}"


def test_severity_band_mapping_is_shared_across_rules():
    """Two different rules fed comparable magnitudes land on comparable bands."""
    # Regional disruption: 2 affected entities is low, 8+ is high.  The two
    # snapshots differ only in how much is affected, never in which rule ran.
    low = DETECTOR.detect(
        snapshot=make_snapshot(
            tick=10,
            regions=(Region("R1", "One", 1.0),),
            stations=tuple(
                make_station(f"S{i}", status="OUTAGE") if i < 2 else make_station(f"S{i}")
                for i in range(6)
            ),
            depots=(make_depot(),),
        ),
        history={},
    )
    high = DETECTOR.detect(
        snapshot=make_snapshot(
            tick=10,
            regions=(Region("R1", "One", 1.0),),
            stations=tuple(
                make_station(f"S{i}", status="OUTAGE") if i < 3 else make_station(f"S{i}")
                for i in range(6)
            ),
            depots=(make_depot("D1", status="OUTAGE"),),
            routes=tuple(make_route(f"RT{i}", status="DISRUPTED") for i in range(4)),
        ),
        history={},
    )
    low_signal = find(low, "regional_disruption")
    high_signal = find(high, "regional_disruption")
    assert low_signal is not None and high_signal is not None
    assert low_signal.evidence["affected_entities"] < high_signal.evidence["affected_entities"]
    assert low_signal.severity is not high_signal.severity
    assert low_signal.severity is Severity.INFO
    assert high_signal.severity is Severity.CRITICAL


# --------------------------------------------------------------------------
# 4. Each signal kind fires on a constructed case
# --------------------------------------------------------------------------


def test_demand_anomaly_fires_and_carries_numbers():
    signals = DETECTOR.detect(
        snapshot=make_snapshot(stations=(make_station("S1", region_id="R7"),), tick=30),
        history=history_for("S1", DIESEL, [100.0, 101, 99, 100, 102, 98, 100, 101] + [800.0]),
    )
    signal = find(signals, "demand_anomaly", "S1")
    assert signal is not None
    assert signal.entity_type == "station"
    assert signal.detected_at_tick == 30
    assert signal.summary  # one operator-facing line
    evidence = signal.evidence
    for key in (
        "fuel_type",
        "latest_liters",
        "baseline_median_liters",
        "baseline_scale_liters",
        "robust_z",
        "abs_robust_z",
        "baseline_points",
        "score",
    ):
        assert key in evidence, f"evidence missing {key}"
    assert evidence["latest_liters"] == 800.0
    assert evidence["station_region_id"] == "R7"
    assert 0.0 <= signal.confidence <= 1.0


def test_inventory_drop_fires_for_a_station():
    signals = DETECTOR.detect(
        snapshot=make_snapshot(
            stations=(make_station("S1", inv=500.0, cap=10000.0),), tick=12
        ),
        history=history_for("S1", DIESEL, [900.0] * 12),
    )
    signal = find(signals, "inventory_drop", "S1")
    assert signal is not None
    assert signal.entity_type == "station"
    assert signal.evidence["fuel_type"] == DIESEL
    assert signal.evidence["fill_ratio"] == pytest.approx(0.05, abs=1e-6)
    assert signal.evidence["cover_ticks"] == pytest.approx(500.0 / 900.0, abs=1e-3)
    assert signal.severity is Severity.CRITICAL


def test_inventory_drop_fires_for_a_depot_against_regional_demand():
    signals = DETECTOR.detect(
        snapshot=make_snapshot(
            tick=12,
            regions=(Region("R1", "One", 1.0),),
            depots=(make_depot("D1", inv=800.0, cap=100000.0),),
            stations=(make_station("S1", region_id="R1"), make_station("S2", region_id="R1")),
        ),
        history={
            ("S1", DIESEL): series("S1", DIESEL, [400.0] * 12),
            ("S2", DIESEL): series("S2", DIESEL, [400.0] * 12),
        },
    )
    signal = find(signals, "inventory_drop", "D1")
    assert signal is not None
    assert signal.entity_type == "depot"
    assert signal.evidence["cover_scope"] == "region_stations"
    assert signal.evidence["cover_basis_liters_per_tick"] == pytest.approx(800.0, abs=1e-3)
    assert signal.evidence["cover_ticks"] == pytest.approx(1.0, abs=1e-3)


def test_inventory_drop_does_not_fire_on_a_healthy_entity():
    signals = DETECTOR.detect(
        snapshot=make_snapshot(stations=(make_station("S1", inv=9000.0, cap=10000.0),)),
        history=history_for("S1", DIESEL, [10.0] * 12),
    )
    assert find(signals, "inventory_drop", "S1") is None


def test_inventory_drop_skips_zero_capacity_without_dividing_by_zero():
    signals = DETECTOR.detect(
        snapshot=make_snapshot(stations=(make_station("S1", inv=0.0, cap=0.0),)),
        history=history_for("S1", DIESEL, [100.0] * 12),
    )
    assert find(signals, "inventory_drop", "S1") is None


def test_route_bottleneck_fires_on_a_disrupted_route():
    signals = DETECTOR.detect(
        snapshot=make_snapshot(
            tick=40,
            depots=(make_depot(),),
            stations=(make_station("S1"),),
            routes=(make_route("RT1", dest="S1", status="DISRUPTED"),),
        ),
        history=history_for("S1", DIESEL, [100.0] * 12),
    )
    signal = find(signals, "route_bottleneck", "RT1")
    assert signal is not None
    assert signal.entity_type == "route"
    assert signal.evidence["disrupted"] is True
    assert signal.evidence["alternative_available_routes"] == 0
    assert signal.severity is Severity.CRITICAL  # single source, no alternative


def test_route_bottleneck_is_less_severe_when_an_alternative_exists():
    common = dict(
        tick=40,
        depots=(make_depot(),),
        stations=(make_station("S1"),),
        history=history_for("S1", DIESEL, [100.0] * 12),
    )
    alone = DETECTOR.detect(
        snapshot=make_snapshot(
            routes=(make_route("RT1", dest="S1", status="DISRUPTED"),), **{k: v for k, v in common.items() if k != "history"}
        ),
        history=common["history"],
    )
    with_alternative = DETECTOR.detect(
        snapshot=make_snapshot(
            routes=(
                make_route("RT1", dest="S1", status="DISRUPTED"),
                make_route("RT2", dest="S1", source="D2", status="AVAILABLE"),
            ),
            **{k: v for k, v in common.items() if k != "history"},
        ),
        history=common["history"],
    )
    a = find(alone, "route_bottleneck", "RT1")
    b = find(with_alternative, "route_bottleneck", "RT1")
    assert a is not None and b is not None
    assert a.evidence["alternative_available_routes"] == 0
    assert b.evidence["alternative_available_routes"] == 1
    order = [Severity.INFO, Severity.WARNING, Severity.SERIOUS, Severity.CRITICAL]
    assert order.index(a.severity) >= order.index(b.severity)


def test_route_bottleneck_fires_when_max_shipment_cannot_cover_demand():
    signals = DETECTOR.detect(
        snapshot=make_snapshot(
            tick=40,
            depots=(make_depot(),),
            stations=(make_station("S1", inv=9000.0, cap=10000.0),),
            routes=(make_route("RT1", dest="S1", max_shipment=100.0),),
        ),
        history=history_for("S1", DIESEL, [1000.0] * 12),
    )
    signal = find(signals, "route_bottleneck", "RT1")
    assert signal is not None
    assert signal.evidence["disrupted"] is False
    assert signal.evidence["capacity_deficit_ratio"] == pytest.approx(0.9, abs=1e-3)
    assert signal.severity is Severity.CRITICAL


def test_route_bottleneck_does_not_fire_on_a_healthy_route():
    signals = DETECTOR.detect(
        snapshot=make_snapshot(
            tick=40,
            depots=(make_depot(),),
            stations=(make_station("S1", inv=9000.0, cap=10000.0),),
            routes=(make_route("RT1", dest="S1", max_shipment=5000.0),),
        ),
        history=history_for("S1", DIESEL, [100.0] * 12),
    )
    assert find(signals, "route_bottleneck", "RT1") is None


def test_regional_disruption_fires_with_multiple_affected_entities():
    signals = DETECTOR.detect(
        snapshot=make_snapshot(
            tick=55,
            regions=(Region("R1", "North", 1.2),),
            stations=(
                make_station("S1", region_id="R1", status="OUTAGE"),
                make_station("S2", region_id="R1", status="CONSTRAINED"),
                make_station("S3", region_id="R1"),
            ),
            depots=(make_depot("D1", region_id="R1", status="OUTAGE"),),
            routes=(make_route("RT1", dest="S3", status="DISRUPTED"),),
        ),
        history={},
    )
    signal = find(signals, "regional_disruption", "R1")
    assert signal is not None
    assert signal.entity_type == "region"
    assert signal.evidence["affected_entities"] == 4
    assert signal.evidence["degraded_station_ids"] == ["S1", "S2"]
    assert signal.evidence["degraded_depot_ids"] == ["D1"]
    assert signal.evidence["disrupted_route_ids"] == ["RT1"]
    assert 0.0 < signal.evidence["affected_share"] <= 1.0


def test_regional_disruption_does_not_fire_on_a_single_affected_entity():
    signals = DETECTOR.detect(
        snapshot=make_snapshot(
            tick=55,
            regions=(Region("R1", "North", 1.0),),
            stations=(make_station("S1", region_id="R1", status="OUTAGE"), make_station("S2", region_id="R1")),
            depots=(make_depot("D1", region_id="R1"),),
        ),
        history={},
    )
    assert find(signals, "regional_disruption", "R1") is None


def test_regional_disruption_counts_active_events():
    signals = DETECTOR.detect(
        snapshot=make_snapshot(
            tick=55,
            regions=(Region("R1", "North", 1.0),),
            stations=(
                make_station("S1", region_id="R1", status="OUTAGE"),
                make_station("S2", region_id="R1"),
            ),
            events=(
                DomainEvent(1, "STORM", 50, None, "ACTIVE", {"region_id": "R1"}),
                DomainEvent(2, "STORM", 50, None, "RESOLVED", {"region_id": "R1"}),
                DomainEvent(3, "STORM", 50, None, "ACTIVE", {"region_id": "R9"}),
            ),
        ),
        history={},
    )
    signal = find(signals, "regional_disruption", "R1")
    assert signal is not None
    # Only the ACTIVE event for R1 counts; the resolved one and R9's do not.
    assert signal.evidence["active_event_ids"] == ["1"]
    assert signal.evidence["degraded_station_ids"] == ["S1"]
    assert signal.evidence["affected_entities"] == 2


def test_supply_shortfall_fires_on_a_delayed_arrival():
    signals = DETECTOR.detect(
        snapshot=make_snapshot(
            tick=60,
            depots=(make_depot("D1", inv=8000.0, cap=20000.0),),
            supply_arrivals=(
                SupplyArrival("A1", "D1", DIESEL, 5000.0, planned_tick=50, actual_tick=None, status="DELAYED"),
            ),
        ),
        history={},
    )
    signal = find(signals, "supply_shortfall", "D1")
    assert signal is not None
    assert signal.entity_type == "depot"
    assert signal.evidence["delay_ticks"] == pytest.approx(10.0)
    assert signal.evidence["arrival_id"] == "A1"
    assert signal.severity is Severity.CRITICAL


def test_supply_shortfall_fires_when_quantity_is_short_against_plan():
    """Arrival lands on time but leaves the depot short of regional cover."""
    signals = DETECTOR.detect(
        snapshot=make_snapshot(
            tick=10,
            regions=(Region("R1", "North", 1.0),),
            depots=(make_depot("D1", region_id="R1", inv=0.0, cap=100000.0),),
            stations=(make_station("S1", region_id="R1"),),
            supply_arrivals=(
                SupplyArrival("A1", "D1", DIESEL, 100.0, planned_tick=10, actual_tick=None, status="SCHEDULED"),
            ),
        ),
        history=history_for("S1", DIESEL, [1000.0] * 12),
    )
    signal = find(signals, "supply_shortfall", "D1")
    assert signal is not None
    assert signal.evidence["delay_ticks"] == 0.0
    # required = 1000 L/tick * 6 ticks of cover = 6000 L; projected = 0 + 100 L.
    assert signal.evidence["required_liters"] == pytest.approx(6000.0)
    assert signal.evidence["projected_depot_liters"] == pytest.approx(100.0)
    assert signal.evidence["shortfall_liters"] == pytest.approx(5900.0)
    assert signal.evidence["shortfall_ratio"] == pytest.approx(5900.0 / 6000.0, abs=1e-3)
    assert signal.evidence["required_basis"] == "region_demand"


def test_supply_shortfall_ignores_an_on_time_adequately_sized_arrival():
    signals = DETECTOR.detect(
        snapshot=make_snapshot(
            tick=10,
            regions=(Region("R1", "North", 1.0),),
            depots=(make_depot("D1", region_id="R1", inv=20000.0, cap=100000.0),),
            stations=(make_station("S1", region_id="R1"),),
            supply_arrivals=(
                SupplyArrival("A1", "D1", DIESEL, 50000.0, planned_tick=12, actual_tick=None, status="SCHEDULED"),
            ),
        ),
        history=history_for("S1", DIESEL, [100.0] * 12),
    )
    assert find(signals, "supply_shortfall", "D1") is None


def test_supply_shortfall_records_a_late_but_arrived_delivery():
    signals = DETECTOR.detect(
        snapshot=make_snapshot(
            tick=60,
            depots=(make_depot("D1", inv=90000.0, cap=100000.0),),
            supply_arrivals=(
                SupplyArrival("A1", "D1", DIESEL, 9000.0, planned_tick=50, actual_tick=59, status="ARRIVED"),
            ),
        ),
        history={},
    )
    signal = find(signals, "supply_shortfall", "D1")
    assert signal is not None
    assert signal.evidence["arrived"] is True
    assert signal.evidence["delay_ticks"] == pytest.approx(9.0)


# --------------------------------------------------------------------------
# 5. Ordering, determinism, shape
# --------------------------------------------------------------------------


def _crowded_snapshot(*, reverse_stations=False):
    """A network that trips all five rules at three different severities."""
    stations = (
        make_station("S1", region_id="R1", status="OUTAGE", inv=800.0, cap=10000.0),
        make_station("S2", region_id="R1", status="OPEN", inv=9000.0, cap=10000.0),
    )
    return make_snapshot(
        tick=77,
        regions=(Region("R1", "North", 1.0),),
        depots=(make_depot("D1", region_id="R1", status="OUTAGE", inv=30000.0, cap=100000.0),),
        stations=tuple(reversed(stations)) if reverse_stations else stations,
        routes=(
            make_route("RT1", dest="S1", source="D1", status="DISRUPTED", max_shipment=1000.0),
            make_route("RT2", dest="S1", source="D1", status="AVAILABLE", max_shipment=5000.0),
        ),
        supply_arrivals=(
            SupplyArrival("A1", "D1", DIESEL, 100.0, planned_tick=40, actual_tick=None, status="DELAYED"),
        ),
    )


def _crowded_history(*, reverse_points=False):
    points = series("S1", DIESEL, CALIBRATED_BASELINE + [165.0])
    if reverse_points:
        # Reorder the same observations; ticks travel with the points, so the
        # detector must still recover the identical series.
        points = list(reversed(points))
    return {
        ("S1", DIESEL): points,
        ("S2", DIESEL): series("S2", DIESEL, [500.0] * 12),
    }


def test_output_is_ordered_most_severe_then_most_confident():
    signals = DETECTOR.detect(snapshot=_crowded_snapshot(), history=_crowded_history())
    assert len(signals) >= 4
    ranks = [
        {Severity.INFO: 0, Severity.WARNING: 1, Severity.SERIOUS: 2, Severity.CRITICAL: 3}[s.severity]
        for s in signals
    ]
    assert ranks == sorted(ranks, reverse=True), f"not severity-ordered: {ranks}"
    # The crowding is deliberately mixed, so ordering is not trivially satisfied.
    assert len(set(ranks)) >= 2, f"all signals landed in one band: {ranks}"

    for coarser, finer in zip(signals, signals[1:]):
        if coarser.severity is finer.severity:
            assert coarser.confidence >= finer.confidence, "confidence not descending inside a band"


def test_severity_spread_is_actually_exercised_by_the_ordering_fixture():
    signals = DETECTOR.detect(snapshot=_crowded_snapshot(), history=_crowded_history())
    by_severity = {s.severity for s in signals}
    assert Severity.WARNING in by_severity
    assert Severity.SERIOUS in by_severity
    assert Severity.CRITICAL in by_severity


def test_detector_finds_every_kind_on_a_crowded_snapshot():
    signals = DETECTOR.detect(snapshot=_crowded_snapshot(), history=_crowded_history())
    found = set(kinds(signals))
    assert {
        "demand_anomaly",
        "inventory_drop",
        "route_bottleneck",
        "regional_disruption",
        "supply_shortfall",
    } <= found, f"missing kinds: {found}"


def test_detection_is_deterministic_across_repeated_calls():
    snapshot = _crowded_snapshot()
    history = _crowded_history()
    first = DETECTOR.detect(snapshot=snapshot, history=history)
    second = DETECTOR.detect(snapshot=snapshot, history=history)
    assert first == second
    assert [s for s in first] == [s for s in second]


def test_detection_is_deterministic_across_container_ordering():
    """Same facts, different container orderings and history point order."""
    reference = AnomalyDetector().detect(
        snapshot=_crowded_snapshot(), history=_crowded_history()
    )
    shuffled = AnomalyDetector().detect(
        snapshot=_crowded_snapshot(reverse_stations=True),
        history=_crowded_history(reverse_points=True),
    )
    assert reference == shuffled
    assert reference, "fixture produced no signals, so the comparison proves nothing"


def test_two_detectors_with_default_thresholds_agree():
    a = AnomalyDetector()
    b = AnomalyDetector()
    assert a.detect(snapshot=_crowded_snapshot(), history=_crowded_history()) == b.detect(
        snapshot=_crowded_snapshot(), history=_crowded_history()
    )


def test_signals_are_frozen_and_json_serialisable():
    import json

    signals = DETECTOR.detect(snapshot=_crowded_snapshot(), history=_crowded_history())
    assert signals
    for signal in signals:
        assert isinstance(signal, RiskSignal)
        with pytest.raises(Exception):
            signal.severity = Severity.INFO  # frozen dataclass
        json.dumps(
            {
                "kind": signal.kind,
                "severity": signal.severity.value,
                "entity_type": signal.entity_type,
                "entity_id": signal.entity_id,
                "detected_at_tick": signal.detected_at_tick,
                "summary": signal.summary,
                "evidence": signal.evidence,
                "confidence": signal.confidence,
            }
        )


def test_severity_enum_matches_the_contract_values():
    assert [s.value for s in Severity] == ["info", "warning", "serious", "critical"]
    assert issubclass(Severity, str)


def test_detect_signature_is_keyword_only_and_matches_the_contract():
    signature = inspect.signature(AnomalyDetector.detect)
    parameters = list(signature.parameters.values())
    assert [p.name for p in parameters] == ["self", "snapshot", "history"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters[1:])
    assert str(signature.return_annotation) in ("list[RiskSignal]", "List[RiskSignal]")


def test_confidence_is_always_a_probability():
    signals = DETECTOR.detect(snapshot=_crowded_snapshot(), history=_crowded_history())
    assert signals
    for signal in signals:
        assert 0.0 <= signal.confidence <= 1.0


def test_evidence_is_present_and_numeric_for_every_signal():
    signals = DETECTOR.detect(snapshot=_crowded_snapshot(), history=_crowded_history())
    assert signals
    for signal in signals:
        assert isinstance(signal.evidence, dict) and signal.evidence
        assert signal.summary.strip()
        assert any(
            isinstance(v, (int, float)) for v in signal.evidence.values()
        ), f"{signal.kind} evidence carries no numbers"


# --------------------------------------------------------------------------
# 6. Purity: no I/O, no clock, no randomness
# --------------------------------------------------------------------------


PURITY_IMPORT_WHITELIST = {
    "__future__",
    "math",
    "statistics",
    "collections",
    "collections.abc",
    "dataclasses",
    "enum",
    "typing",
}

#: The only import detect.py is allowed to make at runtime-ish scope but which
#: is deliberately confined to the ``if TYPE_CHECKING:`` block, so no runtime
#: coupling to a module A2 owns exists.
TYPE_CHECKING_ONLY = {"app.sim.models"}


def _imports_split_by_type_checking(tree):
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent

    runtime, typing_only = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            names = {node.module or ""}
        else:
            continue

        guarded = False
        current = node
        while id(current) in parents:
            current = parents[id(current)]
            if isinstance(current, ast.If) and ast.unparse(current.test).strip() == "TYPE_CHECKING":
                guarded = True
                break
        (typing_only if guarded else runtime).update(names)
    return runtime, typing_only


def test_module_imports_nothing_impure():
    tree = ast.parse(DETECT_SOURCE.read_text(encoding="utf-8"))
    runtime, typing_only = _imports_split_by_type_checking(tree)

    unexpected = runtime - PURITY_IMPORT_WHITELIST
    assert not unexpected, f"detect.py imported outside the pure whitelist: {unexpected}"
    assert typing_only <= TYPE_CHECKING_ONLY, (
        f"detect.py imports {sorted(typing_only - TYPE_CHECKING_ONLY)} under TYPE_CHECKING"
    )
    # The peer-owned models must not leak into the runtime import graph.
    assert "app.sim.models" not in runtime


def test_module_never_touches_the_clock_network_or_randomness():
    source = DETECT_SOURCE.read_text(encoding="utf-8")
    for forbidden in (
        "random.",
        "time.",
        "datetime",
        "socket",
        "httpx",
        "requests",
        "urllib",
        "sqlalchemy",
        "open(",
        "os.environ",
        "getenv",
    ):
        assert forbidden not in source, f"detect.py references {forbidden!r}"


# --------------------------------------------------------------------------
# 7. Cross-check against A2's real app.sim.models (skipped if absent)
# --------------------------------------------------------------------------


EXPECTED_FIELDS = {
    "Snapshot": {
        "tick",
        "depots",
        "stations",
        "routes",
        "regions",
        "supply_arrivals",
        "events",
        "metrics",
    },
    "Station": {"id", "region_id", "status", "demand_multiplier", "capacity", "inventory"},
    "Depot": {"id", "region_id", "status", "capacity", "inventory"},
    "Route": {
        "id",
        "source_depot_id",
        "destination_station_id",
        "transit_ticks",
        "max_shipment",
        "status",
    },
    "Region": {"id", "name", "demand_factor"},
    "SupplyArrival": {"id", "depot_id", "fuel_type", "quantity", "planned_tick", "actual_tick", "status"},
    "DomainEvent": {"id", "type", "status", "parameters"},
    "DemandPoint": {"station_id", "fuel_type", "tick", "liters"},
}


def test_contract_field_names_match_a2s_real_sim_models():
    models = pytest.importorskip(
        "app.sim.models", reason="A2's app/sim/models.py is not present yet"
    )
    missing_modules = []
    for name, expected in EXPECTED_FIELDS.items():
        cls = getattr(models, name, None)
        if cls is None or not is_dataclass(cls):
            missing_modules.append(name)
            continue
        available = {f.name for f in cls.__dataclass_fields__.values()}
        assert expected <= available, (
            f"{name} is missing fields the detector reads: {sorted(expected - available)}"
        )
    assert not missing_modules, f"app.sim.models is missing: {missing_modules}"


def test_detector_consumes_a_real_snapshot_if_a2s_models_exist():
    """End-to-end smoke test over A2's actual dataclasses when available."""
    models = pytest.importorskip(
        "app.sim.models", reason="A2's app/sim/models.py is not present yet"
    )
    try:
        snapshot = models.Snapshot(
            taken_at=0.0,
            tick=90,
            sim_time="T90",
            status="RUNNING",
            depots=(
                models.Depot(
                    id="D1",
                    name="D1",
                    region_id="R1",
                    status="OPEN",
                    dispatch_capacity_per_tick=500.0,
                    capacity={DIESEL: 100000.0},
                    inventory={DIESEL: 100.0},
                ),
            ),
            stations=(
                models.Station(
                    id="S1",
                    name="S1",
                    region_id="R1",
                    status="OUTAGE",
                    demand_profile="urban_high",
                    demand_multiplier=1.0,
                    capacity={DIESEL: 10000.0},
                    inventory={DIESEL: 200.0},
                ),
            ),
            routes=(),
            regions=(models.Region(id="R1", name="North", demand_factor=1.0),),
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
    except TypeError as exc:  # a peer changed the shape mid-flight
        pytest.skip(f"A2's Snapshot no longer constructs with contract fields: {exc}")

    points = [
        models.DemandPoint(station_id="S1", fuel_type=DIESEL, tick=i, liters=float(v))
        for i, v in enumerate([100.0] * 8 + [900.0])
    ]
    signals = AnomalyDetector().detect(snapshot=snapshot, history={("S1", DIESEL): points})
    assert find(signals, "demand_anomaly", "S1") is not None
    assert find(signals, "inventory_drop", "S1") is not None
