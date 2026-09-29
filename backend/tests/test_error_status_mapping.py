"""Simulator error -> HTTP status mapping (guide section 9).

Guide section 9 is a *per-code* table, and section 10 bullet 11 pins
``POST /v1/allocations`` to ``201/200/404/409/503``. So the status the API
returns has to carry the simulator's own meaning:

* a 4xx is *our* request being rejected -- do not retry, fix the request;
* a 503 is the simulator being unavailable -- back off and retry;
* 502 is reserved for a failure we cannot classify at all.

Collapsing every simulator error to 502 (the previous behaviour) erased that
distinction and told the caller "upstream is broken, try again" for requests the
simulator had deliberately refused -- an invitation to re-POST an allocation it
had just rejected, which under ``IDEMPOTENCY_KEY_MISMATCH`` is exactly the wrong
move.

The mapping function matches ``SimulatorError`` by name/shape rather than
importing it (see ``app/api/schemas.py``), but these tests drive it with the
*real* class from ``app/sim/errors.py``, built from the envelopes observed on the
live simulator, so the shape under test is the shape that ships.
"""

from __future__ import annotations

import pytest

from app.api.schemas import simulator_error_to_http
from app.sim.errors import SimulatorError


def _from_domain(status_code: int, code: str, message: str = "rejected") -> SimulatorError:
    """A rejected request, exactly as the live simulator answers it.

    ``{"detail": {"code": "<UPPER_SNAKE>", "message": "..."}}`` -- verified
    against the running instance for both a 409 ROUTE_MISMATCH and a 404
    NOT_FOUND.
    """
    return SimulatorError.from_body(
        status_code,
        {"detail": {"code": code, "message": message}},
        method="POST",
        url="/v1/allocations",
    )


def _mapped(exc: BaseException):
    return simulator_error_to_http(exc)


# ---------------------------------------------------------------------------
# 4xx: our request was wrong -- preserve the simulator's own status
# ---------------------------------------------------------------------------


def test_a_404_domain_error_stays_404() -> None:
    """``NOT_FOUND`` (unknown depot/station/route) is the caller's id problem,
    not an upstream outage: guide section 9 says 404, so the API says 404."""
    http = _mapped(_from_domain(404, "NOT_FOUND", "Source depot not found"))
    assert http.status_code == 404
    assert http.detail["code"] == "NOT_FOUND"


def test_an_unknown_allocation_404_stays_404() -> None:
    http = _mapped(_from_domain(404, "ALLOCATION_NOT_FOUND"))
    assert http.status_code == 404
    assert http.detail["code"] == "ALLOCATION_NOT_FOUND"


@pytest.mark.parametrize(
    "code",
    [
        "IDEMPOTENCY_KEY_MISMATCH",
        "CANNOT_CANCEL",
        "INSUFFICIENT_INVENTORY",
        "ROUTE_MISMATCH",
        "DESTINATION_CAPACITY_EXCEEDED",
    ],
)
def test_every_409_domain_code_stays_409(code: str) -> None:
    """Each 409 has a different remedy (new key / give up / wait for inventory /
    fix the route / wait for demand). A 502 would erase all five and invite a
    retry that cannot succeed."""
    http = _mapped(_from_domain(409, code))
    assert http.status_code == 409
    assert http.detail["code"] == code


def test_a_validation_422_stays_422() -> None:
    """FastAPI's own ``{"detail": [...]}`` envelope from the simulator is still a
    "your payload is wrong" answer, so it keeps 422."""
    exc = SimulatorError.from_body(
        422,
        {"detail": [{"type": "greater_than", "loc": ["body", "quantity"], "msg": "Input should be greater than 0"}]},
        method="POST",
        url="/v1/allocations",
    )
    http = _mapped(exc)
    assert http.status_code == 422
    assert http.detail["code"] == "VALIDATION_ERROR"
    assert "quantity" in http.detail["message"]


# ---------------------------------------------------------------------------
# 5xx / transport: the simulator is unavailable -- 503, back off
# ---------------------------------------------------------------------------


def test_a_fault_injected_503_stays_503() -> None:
    """Guide section 9: ``503 FAULT_INJECTED`` is the backoff-and-retry case.
    Note the fault envelope is ``{"error": ...}``, not ``{"detail": ...}``."""
    exc = SimulatorError.from_body(
        503,
        {"error": {"code": "FAULT_INJECTED", "message": "Injected transient failure"}},
        method="POST",
        url="/v1/allocations",
    )
    http = _mapped(exc)
    assert http.status_code == 503
    assert http.detail["code"] == "FAULT_INJECTED"


def test_a_transport_failure_is_503() -> None:
    """No response ever arrived (connection refused, timeout): the simulator is
    unavailable, which is a 503 -- the same condition as FAULT_INJECTED."""
    http = _mapped(SimulatorError.transport("connection refused", url="http://sim"))
    assert http.status_code == 503
    assert http.detail["code"] == "simulator_error"


def test_an_open_breaker_is_503() -> None:
    http = _mapped(SimulatorError.circuit_open("breaker open", url="http://sim"))
    assert http.status_code == 503


# ---------------------------------------------------------------------------
# Genuinely unknown: 502
# ---------------------------------------------------------------------------


def test_an_unclassifiable_failure_is_502() -> None:
    """A bare exception carries no status code and no envelope: we cannot say
    whose fault it is, so it stays 502 -- *not* a 4xx, because we cannot promise
    the caller that changing the request would help."""
    http = _mapped(RuntimeError("simulator get_stations exploded"))
    assert http.status_code == 502
    assert http.detail["code"] == "simulator_error"
    assert "exploded" in http.detail["message"]


def test_a_502_body_names_the_simulator_as_the_source() -> None:
    assert _mapped(RuntimeError("boom")).detail["details"] == {"source": "simulator"}


def test_a_502_is_never_produced_for_a_domain_4xx() -> None:
    """The regression this file exists for: no 4xx may surface as 502."""
    for status_code, code in [
        (404, "NOT_FOUND"),
        (409, "ROUTE_MISMATCH"),
        (409, "INSUFFICIENT_INVENTORY"),
        (422, "VALIDATION_ERROR"),
    ]:
        http = _mapped(_from_domain(status_code, code))
        assert http.status_code == status_code, f"{code} was flattened to {http.status_code}"


# ---------------------------------------------------------------------------
# The body stays machine-readable in every case
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        _from_domain(404, "NOT_FOUND"),
        _from_domain(409, "ROUTE_MISMATCH"),
        SimulatorError.from_body(503, {"error": {"code": "FAULT_INJECTED", "message": "fault"}}),
        SimulatorError.transport("connection refused"),
        RuntimeError("boom"),
    ],
)
def test_the_code_and_simulated_flag_survive_in_the_body(exc: BaseException) -> None:
    """Callers branch on ``code``, the operator needs ``simulated``, and the body
    must never leak the source exception's traceback."""
    http = _mapped(exc)
    assert isinstance(http.detail, dict)
    assert http.detail["code"], "every error carries a machine-readable code"
    assert http.detail["simulated"] is True
    assert http.detail["message"]
    assert "Traceback" not in str(http.detail)


# ---------------------------------------------------------------------------
# Through the API: the guard hands the mapping's status to the client
# ---------------------------------------------------------------------------


def test_a_rejected_submission_reaches_the_operator_as_409_not_502() -> None:
    """End to end through `simulator_guard`: the guide's 409 must survive the
    route, because a 502 here is what tempts a client to re-POST."""
    from test_api_support import FakeSimulatorClient, build_app, client, error_code

    class RejectingClient(FakeSimulatorClient):
        async def create_allocation(self, req):  # noqa: ANN001 - fake
            raise _from_domain(
                409,
                "INSUFFICIENT_INVENTORY",
                "Depot does not have enough DIESEL",
            )

    simulator = RejectingClient()
    response = client(build_app(client=simulator)).post(
        "/api/v1/recommendations/rec-1/submit", json={"confirm": True}
    )
    assert response.status_code == 409
    assert error_code(response) == "INSUFFICIENT_INVENTORY"
    assert response.json()["detail"]["simulated"] is True
    assert simulator.allocations == [], "the rejected allocation must not be recorded"
