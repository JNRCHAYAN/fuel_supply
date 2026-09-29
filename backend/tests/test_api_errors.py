"""Failure paths: the 400, the 404, the 422 and the typed simulator error.

CONTRACT.md section 9 and the workstream brief's requirement 6. These are the
tests that matter most: an operator must never see a stack trace, and no request
without an explicit confirmation may ever reach the simulator.
"""

from __future__ import annotations

from test_api_support import (
    FakeRepository,
    FakeSimulatorClient,
    build_app,
    client,
    error_code,
)

from app.api.schemas import ErrorResponse

SUBJECT = "rec-1"


def _submit_app(**kwargs):
    return client(build_app(**kwargs))


# ---------------------------------------------------------------------------
# The 400: explicit confirmation is structural, not advisory
# ---------------------------------------------------------------------------


def test_submit_without_a_body_is_rejected_with_400() -> None:
    simulator = FakeSimulatorClient()
    response = client(build_app(client=simulator)).post(
        f"/api/v1/recommendations/{SUBJECT}/submit"
    )
    assert response.status_code == 400
    assert error_code(response) == "confirmation_required"
    assert simulator.allocations == [], "nothing may reach the simulator without confirm"


def test_submit_without_the_confirm_field_is_rejected_with_400() -> None:
    simulator = FakeSimulatorClient()
    response = client(build_app(client=simulator)).post(
        f"/api/v1/recommendations/{SUBJECT}/submit", json={"idempotency_key": "k-1"}
    )
    assert response.status_code == 400
    assert error_code(response) == "confirmation_required"
    assert simulator.allocations == []


def test_submit_with_confirm_false_is_rejected_with_400() -> None:
    simulator = FakeSimulatorClient()
    response = client(build_app(client=simulator)).post(
        f"/api/v1/recommendations/{SUBJECT}/submit",
        json={"confirm": False, "idempotency_key": "k-1"},
    )
    assert response.status_code == 400
    assert error_code(response) == "confirmation_required"
    assert simulator.allocations == []


def test_the_400_is_returned_before_the_recommendation_is_looked_up() -> None:
    """Even an unknown id yields 400 without a confirm, not 404: the guard runs
    first, so it cannot be probed or bypassed by a crafted id."""
    response = client(build_app()).post(
        "/api/v1/recommendations/does-not-exist/submit", json={"confirm": False}
    )
    assert response.status_code == 400
    assert error_code(response) == "confirmation_required"


def test_a_non_boolean_confirm_is_a_422() -> None:
    response = client(build_app()).post(
        f"/api/v1/recommendations/{SUBJECT}/submit", json={"confirm": "yes"}
    )
    assert response.status_code == 422
    assert "detail" in response.json()


def test_the_400_body_is_typed_and_marked_simulated() -> None:
    response = client(build_app()).post(f"/api/v1/recommendations/{SUBJECT}/submit")
    body = ErrorResponse.model_validate(response.json())
    assert body.detail.code == "confirmation_required"
    assert body.detail.simulated is True
    assert "never auto-executes" in body.detail.message


# ---------------------------------------------------------------------------
# The 404: typed, never a 500
# ---------------------------------------------------------------------------


def test_unknown_recommendation_id_is_a_typed_404() -> None:
    response = client(build_app()).get("/api/v1/recommendations/nope/explanation")
    assert response.status_code == 404
    assert error_code(response) == "recommendation_not_found"
    assert "Traceback" not in response.text
    assert response.json()["detail"]["simulated"] is True


def test_unknown_recommendation_id_on_submit_is_a_typed_404() -> None:
    simulator = FakeSimulatorClient()
    response = client(build_app(client=simulator)).post(
        "/api/v1/recommendations/nope/submit", json={"confirm": True}
    )
    assert response.status_code == 404
    assert error_code(response) == "recommendation_not_found"
    assert simulator.allocations == []


def test_unknown_station_forecast_is_a_404() -> None:
    response = client(build_app()).get("/api/v1/forecast", params={"station_id": "ST-404"})
    assert response.status_code == 404
    assert error_code(response) == "station_not_found"


def test_unknown_decision_id_is_a_typed_404() -> None:
    response = client(build_app()).get("/api/v1/decisions/9999")
    assert response.status_code == 404
    assert error_code(response) == "decision_not_found"


def test_a_non_integer_decision_id_is_a_422() -> None:
    response = client(build_app()).get("/api/v1/decisions/not-a-number")
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# The 422: validated input
# ---------------------------------------------------------------------------


def test_out_of_range_horizon_is_a_422() -> None:
    response = client(build_app()).get("/api/v1/forecast", params={"horizon_ticks": 0})
    assert response.status_code == 422


def test_out_of_range_demand_history_limit_is_a_422() -> None:
    response = client(build_app()).get("/api/v1/network/demand-history", params={"limit": 0})
    assert response.status_code == 422


def test_a_short_investigation_question_is_a_422() -> None:
    response = client(build_app()).post("/api/v1/investigate", json={"question": "hi"})
    assert response.status_code == 422
    assert "at least 3 characters" in response.text


def test_an_unknown_simulation_action_is_a_422() -> None:
    response = client(build_app()).post("/api/v1/admin/simulation/explode")
    assert response.status_code == 422
    assert "run" in response.text


def test_a_fault_without_a_type_is_a_422() -> None:
    response = client(build_app()).post("/api/v1/admin/faults", json={"probability": 1.0})
    assert response.status_code == 422


def test_a_non_positive_budget_is_a_422() -> None:
    response = client(build_app()).get("/api/v1/recommendations", params={"budget_liters": 0})
    assert response.status_code == 422


def test_a_non_positive_submission_quantity_is_a_422() -> None:
    response = client(build_app()).post(
        f"/api/v1/recommendations/{SUBJECT}/submit",
        json={"confirm": True, "quantity_liters": -5},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# The simulator's failures
# ---------------------------------------------------------------------------


def test_a_simulator_failure_is_a_typed_502_not_a_500() -> None:
    broken = FakeSimulatorClient()
    broken.fail_on = {"get_stations"}
    response = client(build_app(client=broken)).get("/api/v1/network/snapshot")
    assert response.status_code == 502
    assert error_code(response) == "simulator_error"
    assert "exploded" in response.json()["detail"]["message"]
    assert "Traceback" not in response.text


def test_a_simulator_failure_on_events_is_typed() -> None:
    broken = FakeSimulatorClient()
    broken.fail_on = {"get_events"}
    response = client(build_app(client=broken)).get("/api/v1/events")
    assert response.status_code == 502
    assert error_code(response) == "simulator_error"


def test_a_rejected_submission_keeps_the_simulators_own_status() -> None:
    """A 4xx from the simulator is *our* request being refused, not an upstream
    outage. It must reach the operator as its own status: flattening it to 502
    said "retry" about the very allocation the simulator had just rejected
    (guide section 9; POST /v1/allocations is 201/200/404/409/503).

    The exception is the simulator's real shape, built from the live 409
    ROUTE_MISMATCH envelope::

        {"detail": {"code": "ROUTE_MISMATCH", "message": "Route does not connect ..."}}
    """
    from app.sim.errors import SimulatorError

    class RejectingClient(FakeSimulatorClient):
        async def create_allocation(self, req):
            raise SimulatorError.from_body(
                409,
                {
                    "detail": {
                        "code": "ROUTE_MISMATCH",
                        "message": "Route does not connect selected depot and station",
                    }
                },
                method="POST",
                url="/v1/allocations",
            )

    broken = RejectingClient()
    response = client(build_app(client=broken)).post(
        f"/api/v1/recommendations/{SUBJECT}/submit", json={"confirm": True}
    )
    assert response.status_code == 409
    assert error_code(response) == "ROUTE_MISMATCH"
    assert broken.allocations == []


def test_an_unclassifiable_simulator_failure_during_submission_is_typed() -> None:
    """A failure with no status code and no envelope cannot be blamed on the
    request, so it stays a typed 502 -- never a 500 and never a 4xx."""
    broken = FakeSimulatorClient()
    broken.fail_on = {"create_allocation"}
    response = client(build_app(client=broken)).post(
        f"/api/v1/recommendations/{SUBJECT}/submit", json={"confirm": True}
    )
    assert response.status_code == 502
    assert error_code(response) == "simulator_error"


def test_a_simulator_failure_during_fault_injection_is_typed() -> None:
    broken = FakeSimulatorClient()
    broken.fail_on = {"admin_inject_fault"}
    response = client(build_app(client=broken)).post(
        "/api/v1/admin/faults", json={"type": "unavailable"}
    )
    assert response.status_code == 502
    assert error_code(response) == "simulator_error"


def test_a_repository_failure_is_a_typed_503() -> None:
    repository = FakeRepository()
    repository.fail_on = {"list_decisions"}
    response = client(build_app(repository=repository)).get("/api/v1/decisions")
    assert response.status_code == 503
    assert error_code(response) == "dependency_unavailable"
    assert "Traceback" not in response.text


def test_a_missing_decision_row_when_recording_the_outcome_is_a_404() -> None:
    """A3 raises LookupError for an unknown decision id; that is a 404, not a 500.

    The allocation has already reached the simulator by then, so its id travels
    in the error body — the operator can still reconcile what happened.
    """

    class LookupErrorRepository(FakeRepository):
        async def record_decision_outcome(
            self, decision_id, *, submitted, allocation_id, note
        ) -> None:
            raise LookupError(f"no decision with id {decision_id}")

    simulator = FakeSimulatorClient()
    response = client(build_app(client=simulator, repository=LookupErrorRepository())).post(
        f"/api/v1/recommendations/{SUBJECT}/submit", json={"confirm": True}
    )
    assert response.status_code == 404
    assert error_code(response) == "decision_not_found"
    assert len(simulator.allocations) == 1, "the allocation was created before the audit write"
    assert response.json()["detail"]["details"]["allocation_id"] == 700


def test_an_explanation_service_failure_is_a_typed_503() -> None:
    from test_api_support import FakeExplanationService

    response = client(build_app(explainer=FakeExplanationService(boom=True))).get(
        f"/api/v1/recommendations/{SUBJECT}/explanation"
    )
    assert response.status_code == 503
    assert error_code(response) == "dependency_unavailable"


def test_the_simulator_failure_is_recorded_in_the_metrics() -> None:
    """CONTRACT.md 0.4: every failure path yields a typed error, a log line *and
    a metric*."""
    from test_api_support import FakeMetrics

    metrics = FakeMetrics()
    broken = FakeSimulatorClient()
    broken.fail_on = {"get_stations"}
    client(build_app(client=broken, metrics=metrics)).get("/api/v1/network/snapshot")
    assert metrics.api_errors, "the failure path must emit a metric"


# ---------------------------------------------------------------------------
# Conventions across the surface
# ---------------------------------------------------------------------------


def _get_paths() -> list[str]:
    return [
        "/api/v1/health",
        "/api/v1/status",
        "/api/v1/network/snapshot",
        "/api/v1/network/demand-history",
        "/api/v1/forecast",
        "/api/v1/risk",
        "/api/v1/recommendations",
        "/api/v1/decisions",
        "/api/v1/events",
        "/api/v1/admin/faults",
    ]


def test_no_read_route_returns_a_500() -> None:
    http = client(build_app())
    for path in _get_paths():
        response = http.get(path)
        assert response.status_code != 500, f"{path} returned 500: {response.text[:200]}"


def test_every_predictive_or_advisory_response_is_marked_simulated() -> None:
    """CONTRACT.md section 0.6 — a judge must never mistake this for a real tool."""
    http = client(build_app())
    for path in _get_paths():
        body = http.get(path).json()
        assert body.get("simulated") is True, f"{path} is missing simulated: true"

    posts = [
        ("/api/v1/events/summary", None),
        ("/api/v1/investigate", {"question": "why is ST-2 short?"}),
        ("/api/v1/admin/faults", {"type": "unavailable"}),
        ("/api/v1/admin/simulation/step", None),
        (f"/api/v1/recommendations/{SUBJECT}/submit", {"confirm": True}),
    ]
    for path, payload in posts:
        response = http.post(path, json=payload) if payload is not None else http.post(path)
        assert response.status_code == 200, f"{path} -> {response.status_code} {response.text[:200]}"
        assert response.json().get("simulated") is True, f"{path} is missing simulated: true"

    explanation = http.get(f"/api/v1/recommendations/{SUBJECT}/explanation")
    assert explanation.status_code == 200, explanation.text[:200]
    assert explanation.json().get("simulated") is True


def test_errors_never_echo_the_api_key() -> None:
    from test_api_support import SECRET_SENTINEL

    broken = FakeSimulatorClient()
    broken.fail_on = {"get_stations"}
    response = client(build_app(client=broken)).get("/api/v1/network/snapshot")
    assert SECRET_SENTINEL not in response.text
