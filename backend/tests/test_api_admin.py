"""`/api/v1/admin/*` — simulator scenario control.

This is the demo surface for the resilience story: an operator injects a fault
and watches the console degrade honestly (CONTRACT.md 0.1 keeps every one of
these actions inside the published simulation).

The three CONTRACT.md section 9 rows are exercised here. The rest of guide
section 7's operator surface — event injection, audit, toggle — has its own
home in `test_admin_operator_surface.py`.
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
    http.post("/api/v1/admin/faults", json={"type": "stale_data"})
    body = http.get("/api/v1/admin/faults").json()
    assert body["count"] == 1
    assert body["faults"][0]["type"] == "stale_data"
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
        "/api/v1/admin/faults", json={"type": "unavailable", "route_id": "RT-1"}
    ).json()
    assert body["fault"]["type"] == "unavailable"
    assert body["fault"]["active"] is True
    assert body["simulated"] is True
    assert len(simulator.injected_faults) == 1


def test_the_injected_fault_reaches_the_simulator_with_the_operator_fields() -> None:
    """A2's `FaultRequest` is {type, duration_seconds, parameters}: a `delay_ms`
    sent at the top level has to travel inside `parameters` to arrive at all."""
    simulator = FakeSimulatorClient()
    client(build_app(client=simulator)).post(
        "/api/v1/admin/faults", json={"type": "latency", "delay_ms": 250}
    )
    payload = simulator.injected_faults[0]  # the payload A2's client would POST
    assert payload["type"] == "latency"
    assert payload["parameters"]["delay_ms"] == 250
    assert payload["duration_seconds"] == 300


def test_an_explicit_fault_duration_override_is_honoured() -> None:
    simulator = FakeSimulatorClient()
    client(build_app(client=simulator)).post(
        "/api/v1/admin/faults", json={"type": "stale_data", "duration_seconds": 60}
    )
    assert simulator.injected_faults[0]["duration_seconds"] == 60


def test_a_fault_type_outside_the_documented_five_is_a_422() -> None:
    """Guide section 7.9 pins the enum; forwarding anything else would come back
    from the simulator as a 422 and be relayed to the operator as a 502 that
    names nothing they typed."""
    simulator = FakeSimulatorClient()
    response = client(build_app(client=simulator)).post(
        "/api/v1/admin/faults", json={"type": "depot_offline"}
    )
    assert response.status_code == 422
    assert error_code(response) == "invalid_fault_type"
    assert simulator.injected_faults == [], "a rejected type must not reach the simulator"


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
        "/api/v1/admin/faults", json={"type": "stale_data"}
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


def test_clear_faults_closes_the_resilience_loop() -> None:
    """`admin_clear_faults` is in guide section 7.11, so it now has a route.

    Previously recorded here as a visible contract gap (the capability existed on
    the client with no path to it). Guide section 7.11 names the path, so the gap
    is closed: inject a fault, watch the console degrade, clear it, recover.
    """
    simulator = FakeSimulatorClient()
    http = client(build_app(client=simulator))
    http.post("/api/v1/admin/faults", json={"type": "stale_data"})
    assert http.get("/api/v1/admin/faults").json()["count"] == 1

    response = http.post("/api/v1/admin/faults/clear")
    assert response.status_code == 200
    body = response.json()
    assert body["cleared"] is True
    assert body["simulated"] is True
    assert http.get("/api/v1/admin/faults").json()["count"] == 0


def test_clear_faults_is_idempotent() -> None:
    """Guide section 7.11: idempotent. Clearing nothing is a success, not a 404."""
    response = client(build_app()).post("/api/v1/admin/faults/clear")
    assert response.status_code == 200
    assert response.json()["cleared"] is True


def test_a_client_without_clear_faults_is_a_typed_503() -> None:
    simulator = FakeSimulatorClient()
    simulator.admin_clear_faults = None  # type: ignore[assignment]
    response = client(build_app(client=simulator)).post("/api/v1/admin/faults/clear")
    assert response.status_code == 503
    assert error_code(response) == "admin_action_unavailable"
    assert response.json()["detail"]["details"]["action"] == "clear_faults"


def test_a_dead_simulator_is_a_typed_502_on_clear() -> None:
    broken = FakeSimulatorClient()
    broken.fail_on = {"admin_clear_faults"}
    response = client(build_app(client=broken)).post("/api/v1/admin/faults/clear")
    assert response.status_code == 502
    assert error_code(response) == "simulator_error"
