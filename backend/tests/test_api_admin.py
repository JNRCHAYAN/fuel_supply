"""`/api/v1/admin/*` — simulator scenario control.

This is the demo surface for the resilience story: an operator injects a fault
and watches the console degrade honestly (CONTRACT.md 0.1 keeps every one of
these actions inside the published simulation).
"""

from __future__ import annotations

from test_api_support import (
    TICK,
    FakeSimulatorClient,
    build_app,
    client,
    error_code,
)


# ---------------------------------------------------------------------------
# GET /api/v1/admin/faults
# ---------------------------------------------------------------------------


def test_there_are_no_faults_before_any_are_injected() -> None:
    body = client(build_app()).get("/api/v1/admin/faults").json()
    assert body["count"] == 0
    assert body["faults"] == []
    assert body["simulated"] is True


def test_injected_faults_are_listed() -> None:
    simulator = FakeSimulatorClient()
    http = client(build_app(client=simulator))
    http.post("/api/v1/admin/faults", json={"type": "depot_offline"})
    body = http.get("/api/v1/admin/faults").json()
    assert body["count"] == 1
    assert body["faults"][0]["type"] == "depot_offline"
    assert body["faults"][0]["active"] is True


def test_a_dead_simulator_is_a_typed_502() -> None:
    broken = FakeSimulatorClient()
    broken.fail_on = {"admin_get_faults"}
    response = client(build_app(client=broken)).get("/api/v1/admin/faults")
    assert response.status_code == 502
    assert error_code(response) == "simulator_error"
    assert "Traceback" not in response.text


# ---------------------------------------------------------------------------
# POST /api/v1/admin/faults
# ---------------------------------------------------------------------------


def test_a_fault_can_be_injected_with_a_type() -> None:
    simulator = FakeSimulatorClient()
    body = client(build_app(client=simulator)).post(
        "/api/v1/admin/faults", json={"type": "route_blocked", "route_id": "RT-1"}
    ).json()
    assert body["fault"]["type"] == "route_blocked"
    assert body["fault"]["active"] is True
    assert body["simulated"] is True
    assert len(simulator.injected_faults) == 1


def test_the_injected_fault_reaches_the_simulator_with_the_operator_fields() -> None:
    """A2's `FaultRequest` is {type, duration_seconds, parameters}: a `depot_id`
    sent at the top level has to travel inside `parameters` to arrive at all."""
    simulator = FakeSimulatorClient()
    client(build_app(client=simulator)).post(
        "/api/v1/admin/faults", json={"type": "depot_offline", "depot_id": "DP-1"}
    )
    payload = simulator.injected_faults[0]  # the payload A2's client would POST
    assert payload["type"] == "depot_offline"
    assert payload["parameters"]["depot_id"] == "DP-1"
    assert payload["duration_seconds"] == 300


def test_an_explicit_fault_duration_override_is_honoured() -> None:
    simulator = FakeSimulatorClient()
    client(build_app(client=simulator)).post(
        "/api/v1/admin/faults", json={"type": "depot_offline", "duration_seconds": 60}
    )
    assert simulator.injected_faults[0]["duration_seconds"] == 60


def test_a_fault_without_a_type_is_a_422() -> None:
    simulator = FakeSimulatorClient()
    response = client(build_app(client=simulator)).post("/api/v1/admin/faults", json={})
    assert response.status_code == 422
    assert simulator.injected_faults == [], "nothing may reach the simulator unvalidated"


def test_an_empty_fault_type_is_a_422() -> None:
    response = client(build_app()).post("/api/v1/admin/faults", json={"type": ""})
    assert response.status_code == 422


def test_a_rejected_fault_never_reaches_the_simulator() -> None:
    simulator = FakeSimulatorClient()
    client(build_app(client=simulator)).post(
        "/api/v1/admin/faults", json={"type": "x" * 101}
    )
    assert simulator.injected_faults == []


def test_a_dead_simulator_is_a_typed_502_on_injection() -> None:
    broken = FakeSimulatorClient()
    broken.fail_on = {"admin_inject_fault"}
    response = client(build_app(client=broken)).post(
        "/api/v1/admin/faults", json={"type": "depot_offline"}
    )
    assert response.status_code == 502
    assert error_code(response) == "simulator_error"


# ---------------------------------------------------------------------------
# POST /api/v1/admin/simulation/{action}
# ---------------------------------------------------------------------------


def test_every_contract_action_is_reachable() -> None:
    simulator = FakeSimulatorClient()
    http = client(build_app(client=simulator))
    for action in ("run", "pause", "step", "reset"):
        response = http.post(f"/api/v1/admin/simulation/{action}")
        assert response.status_code == 200, action
        body = response.json()
        assert body["action"] == action
        assert body["simulated"] is True
    assert simulator.actions == ["run", "pause", "step", "reset"]


def test_step_returns_the_new_tick() -> None:
    body = client(build_app()).post("/api/v1/admin/simulation/step").json()
    assert body["result"]["tick"] == TICK + 1


def test_pause_reports_the_paused_status() -> None:
    body = client(build_app()).post("/api/v1/admin/simulation/pause").json()
    assert body["result"]["status"] == "PAUSED"


def test_an_unknown_action_is_a_422_from_the_path_validator() -> None:
    simulator = FakeSimulatorClient()
    response = client(build_app(client=simulator)).post("/api/v1/admin/simulation/explode")
    assert response.status_code == 422
    assert simulator.actions == [], "an unknown action must not reach the simulator"


def test_a_client_missing_the_capability_is_a_typed_503() -> None:
    """A capability the gateway does not implement is named, not guessed at."""
    simulator = FakeSimulatorClient()
    simulator.admin_reset = None  # type: ignore[assignment]
    response = client(build_app(client=simulator)).post("/api/v1/admin/simulation/reset")
    assert response.status_code == 503
    assert error_code(response) == "admin_action_unavailable"
    assert response.json()["detail"]["details"]["action"] == "reset"


def test_a_dead_simulator_is_a_typed_502_on_an_action() -> None:
    broken = FakeSimulatorClient()
    broken.fail_on = {"admin_pause"}
    response = client(build_app(client=broken)).post("/api/v1/admin/simulation/pause")
    assert response.status_code == 502
    assert error_code(response) == "simulator_error"


def test_the_contract_gap_is_visible_clear_faults_is_not_exposed() -> None:
    """`admin_clear_faults` is in CONTRACT.md 5.4 but has no section 9 route.

    The route table is asserted in test_api_routes.py; this records that the
    capability exists on the client and the API deliberately does not expose it.
    """
    simulator = FakeSimulatorClient()
    assert callable(simulator.admin_clear_faults)
    assert client(build_app()).post("/api/v1/admin/faults/clear").status_code == 404
