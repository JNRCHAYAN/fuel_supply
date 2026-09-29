"""`GET /api/v1/stockout` — the stockout / shortage projection envelope.

Route-level tests: the route is exercised through FastAPI with the same
`app.dependency_overrides` harness the rest of the API suite uses
(`test_api_support.build_app`), so the real composition runs — snapshot →
history → forecasts → the stockout engine — with only the four peer objects
(Settings, SimulatorClient, Repository, Metrics) and the engines faked.

Covered here: the envelope, summary completeness, per-row field round-tripping
and the `risk` string spelling, the four filters, the deliberate summary-vs-risk
-filter semantics, the typed 404 for an unknown station, and degradation (a dead
allocation ledger, a dead demand history, malformed peer payloads, and a
forecaster that raises).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import test_api_support  # noqa: F401  (installs the backend on sys.path)
from test_api_support import (
    TICK,
    FakeForecaster,
    FakeSimulatorClient,
    build_app,
    client,
)

ROUTE = "/api/v1/stockout"

#: The wire spelling of every band. `RiskLevel` is a `str`-mixin enum, but a
#: response that leaked the enum would read `"RiskLevel.CRITICAL"` and match
#: none of these — which is exactly what the risk assertions exist to catch.
RISK_LEVELS = frozenset({"LOW", "MEDIUM", "HIGH", "CRITICAL"})

#: The fields each row must round-trip from the engine (CONTRACT.md 7.4).
ASSESSMENT_FIELDS = frozenset(
    {
        "station_id",
        "fuel_type",
        "current_inventory",
        "expected_demand",
        "incoming_supply",
        "projected_inventory",
        "shortage_amount",
        "surplus_amount",
        "stockout_tick",
        "ticks_until_stockout",
        "risk",
        "basis",
    }
)


def _response(app=None, **params):
    return client(app or build_app()).get(ROUTE, params=params or None)


def _body(**params):
    response = _response(**params)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def test_envelope_carries_every_field_and_count_matches_the_rows() -> None:
    body = _body()
    assert set(body) >= {
        "horizon_ticks",
        "generated_at_tick",
        "count",
        "thresholds",
        "summary",
        "assessments",
        "simulated",
    }
    assert body["simulated"] is True
    assert body["horizon_ticks"] == 8
    assert body["generated_at_tick"] == TICK
    # 2 stations x 3 fuels in the shared fixture snapshot.
    assert body["count"] == len(body["assessments"]) == 6


def test_thresholds_are_the_bands_the_engine_actually_ran_with() -> None:
    thresholds = _body()["thresholds"]
    assert set(thresholds) >= {
        "critical_ticks",
        "high_ticks",
        "safety_stock_fraction",
        "horizon_ticks",
    }
    assert thresholds["horizon_ticks"] == 8
    assert thresholds["critical_ticks"] <= thresholds["high_ticks"]


# ---------------------------------------------------------------------------
# Summary completeness
# ---------------------------------------------------------------------------


def test_summary_carries_all_four_levels_even_when_a_count_is_zero() -> None:
    body = _body()
    # Every level is a *stated* count, including the zeros: the console renders
    # four tiles without a missing key defaulting to 0 behind its back.
    assert set(body["summary"]) == RISK_LEVELS
    assert body["summary"]["CRITICAL"] == 0  # the fixture projects none
    assert sum(body["summary"].values()) == body["count"] == len(body["assessments"])


def test_summary_counts_the_rows_in_the_station_scope() -> None:
    everything = _body()
    scoped = _body(station_id="ST-1")
    expected = sum(
        1 for row in everything["assessments"] if row["station_id"] == "ST-1"
    )
    assert scoped["count"] == expected == len(scoped["assessments"])
    assert sum(scoped["summary"].values()) == scoped["count"]


# ---------------------------------------------------------------------------
# Assessment fields
# ---------------------------------------------------------------------------


def test_each_assessment_round_trips_the_engine_fields() -> None:
    for row in _body()["assessments"]:
        assert ASSESSMENT_FIELDS <= set(row), sorted(set(row))
        assert row["basis"], "the basis sentence is the explanation, not decoration"
        # The arithmetic reconciles exactly: the projection is auditable by hand.
        assert row["projected_inventory"] == pytest.approx(
            row["current_inventory"] + row["incoming_supply"] - row["expected_demand"]
        )
        assert row["shortage_amount"] == pytest.approx(
            max(0.0, -row["projected_inventory"])
        )
        assert row["surplus_amount"] == pytest.approx(
            max(0.0, row["projected_inventory"])
        )


def test_risk_is_a_plain_string_and_never_an_enum_repr() -> None:
    rows = _body()["assessments"]
    assert rows
    for row in rows:
        assert isinstance(row["risk"], str)
        assert row["risk"] in RISK_LEVELS
        assert not row["risk"].startswith("RiskLevel.")


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_station_filter_narrows_to_that_station() -> None:
    body = _body(station_id="ST-1")
    assert {row["station_id"] for row in body["assessments"]} == {"ST-1"}
    assert body["count"] == len(body["assessments"])
    assert body["count"] == 3  # DIESEL, OCTANE, PETROL


def test_fuel_filter_is_case_insensitive() -> None:
    lower = _body(fuel_type="diesel")
    upper = _body(fuel_type="DIESEL")
    assert lower == upper
    assert {row["fuel_type"] for row in lower["assessments"]} == {"DIESEL"}
    assert lower["count"] == 2  # one row per station


def test_risk_filter_narrows_assessments_to_that_level() -> None:
    everything = _body()
    for level in RISK_LEVELS:
        filtered = _body(risk=level)
        assert all(row["risk"] == level for row in filtered["assessments"])
        expected = sum(
            1 for row in everything["assessments"] if row["risk"] == level
        )
        assert filtered["count"] == expected, level
    # A genuinely mixed picture, so the filter is exercised rather than vacuous.
    assert 0 < everything["summary"]["HIGH"] < everything["count"]


def test_risk_filter_is_case_insensitive() -> None:
    assert _body(risk="high") == _body(risk="HIGH")


def test_horizon_ticks_is_echoed_back() -> None:
    assert _body(horizon_ticks=3)["horizon_ticks"] == 3
    assert _body(horizon_ticks=8)["horizon_ticks"] == 8


# ---------------------------------------------------------------------------
# Summary vs risk filter semantics
# ---------------------------------------------------------------------------


def test_risk_filter_narrows_rows_while_summary_still_describes_the_scope() -> None:
    """The route's documented choice: tiles describe the station/fuel scope.

    `risk=HIGH` narrows `assessments`, but `summary` still bands the whole
    scope — a filtered page must not collapse to "1 HIGH, 0, 0, 0" and pretend
    that is the network.
    """
    everything = _body()
    target = "HIGH"
    scoped_count = everything["summary"][target]
    assert 0 < scoped_count < everything["count"]

    filtered = _body(risk=target)
    assert filtered["count"] == scoped_count
    assert all(row["risk"] == target for row in filtered["assessments"])
    # Unchanged by the risk filter: still the station/fuel scope, all six rows.
    assert filtered["summary"] == everything["summary"]
    assert sum(filtered["summary"].values()) == everything["count"]


def test_station_filter_narrows_both_rows_and_summary() -> None:
    """A station/fuel filter *is* part of the scope, so it does narrow the tiles."""
    everything = _body()
    scoped = _body(station_id="ST-2", fuel_type="diesel")
    assert scoped["count"] == len(scoped["assessments"]) == 1
    assert sum(scoped["summary"].values()) == scoped["count"]
    assert sum(scoped["summary"].values()) < everything["count"]


# ---------------------------------------------------------------------------
# Unknown station
# ---------------------------------------------------------------------------


def test_an_unknown_station_is_a_typed_404_not_a_500_or_an_empty_200() -> None:
    response = _response(station_id="NOPE")
    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "station_not_found"
    assert detail["details"]["station_id"] == "NOPE"


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


def test_a_dead_allocation_ledger_still_answers_with_zero_incoming_supply() -> None:
    simulator = FakeSimulatorClient()
    simulator.fail_on = {"get_allocations"}
    response = _response(build_app(client=simulator))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 6
    assert all(row["incoming_supply"] == 0.0 for row in body["assessments"])


def test_a_dead_demand_history_still_answers_from_the_repository() -> None:
    simulator = FakeSimulatorClient()
    simulator.fail_on = {"get_demand_history"}
    response = _response(build_app(client=simulator))
    assert response.status_code == 200, response.text
    assert response.json()["count"] == 6


def test_a_forecaster_that_raises_drops_rows_without_failing_the_page() -> None:
    class BoomForecaster(FakeForecaster):
        def forecast(self, history, *, horizon_ticks=8):
            raise ValueError("degenerate series")

    response = _response(build_app(forecaster=BoomForecaster()))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 0
    assert body["assessments"] == []
    assert set(body["summary"]) == RISK_LEVELS  # the four tiles survive an empty page
    assert sum(body["summary"].values()) == 0


# ---------------------------------------------------------------------------
# Incoming supply
# ---------------------------------------------------------------------------


def test_in_transit_allocations_reach_the_rows() -> None:
    class AllocatingClient(FakeSimulatorClient):
        async def get_allocations(self):
            return [
                SimpleNamespace(
                    id=7,
                    destination_station_id="ST-2",
                    status="PENDING",
                    expected_arrival_tick=TICK + 2,
                    departure_tick=None,
                    quantity=500.0,
                    fuel_type="diesel",  # case-insensitive against the row's DIESEL
                    source_depot_id="DP-1",
                )
            ]

    body = _response(build_app(client=AllocatingClient())).json()
    rows = {
        (row["station_id"], row["fuel_type"]): row for row in body["assessments"]
    }
    row = rows[("ST-2", "DIESEL")]
    assert row["incoming_supply"] == 500.0
    assert row["incoming_sources"][0]["quantity"] == 500.0
    # Every other row is untouched by an allocation addressed to one station.
    assert all(
        other["incoming_supply"] == 0.0
        for key, other in rows.items()
        if key != ("ST-2", "DIESEL")
    )


# ---------------------------------------------------------------------------
# Malformed peer payloads
# ---------------------------------------------------------------------------


def test_malformed_peer_allocations_do_not_500_the_route() -> None:
    class GarbageAllocations(FakeSimulatorClient):
        async def get_allocations(self):
            return [
                None,
                "junk",
                42,
                {"destination_station_id": None},
                # Non-numeric quantity/arrival and a status that is not inbound.
                {
                    "destination_station_id": "ST-1",
                    "status": "PENDING",
                    "quantity": "abc",
                    "expected_arrival_tick": "nope",
                },
                # ARRIVED is already in the inventory, so it must not be inbound.
                {
                    "destination_station_id": "ST-1",
                    "status": "ARRIVED",
                    "quantity": 10.0,
                    "expected_arrival_tick": TICK + 1,
                },
            ]

    response = _response(build_app(client=GarbageAllocations()))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 6
    assert all(row["incoming_supply"] == 0.0 for row in body["assessments"])


def test_malformed_peer_forecast_does_not_500_the_route() -> None:
    class GarbageForecast(FakeForecaster):
        def forecast(self, history, *, horizon_ticks=8):
            return SimpleNamespace(
                points=(), method=None, confidence="oops", fallback_used=False
            )

    response = _response(build_app(forecaster=GarbageForecast()))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 6
    for row in body["assessments"]:
        assert row["expected_demand"] == 0.0
        assert row["risk"] in RISK_LEVELS
