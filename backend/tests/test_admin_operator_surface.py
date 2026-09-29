"""The operator-facing self-test surface of `/api/v1/admin/*` (guide section 7).

Guide section 7 opens by saying the simulator's `/admin/*` API is "designed for
organizer use, but participants will find them invaluable for self-testing".
These tests are the proof that *this backend* exposes that whole surface, so
brief section 22's demonstration script — "a crisis event occurs", "application
or dependency failure is injected", "monitoring detects failure" — can be driven
by a judge through one API instead of two.

What is asserted here, and why each one is a real risk rather than a formality:

* all six event types (guide 7.8) and all five fault types (guide 7.10) are
  accepted, so no documented crisis is unreachable;
* `duration_ticks=0` and an out-of-range `duration_seconds` are 422s *and never
  reach the simulator* — the simulator's own 422 would be relayed as a 502 that
  names nothing the operator typed;
* an empty filter list is the documented "all entities of that type" case, not a
  missing scope (guide 7.8), and the singular `region_id` spelling the simulator
  would silently ignore is refused;
* `limit` on the audit log is clamped to guide 7.12's window rather than rejected;
* a simulator that is down produces a typed error, never a 500.

The fake client lives here rather than in `test_api_support.py` because it stands
in for capabilities (`admin_toggle`, `admin_get_events`, `get_audit`) that the
shared fixture does not carry; that module is owned by another workstream.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from test_api_support import (
    TICK,
    FakeSimulatorClient,
    build_app,
    client,
    error_code,
)


class OperatorFakeSimulator(FakeSimulatorClient):
    """`FakeSimulatorClient` plus guide section 7's inspection capabilities.

    It keeps A2's client contract in one respect that matters: `admin_inject_event`
    is handed A2's `EventRequest` and calls `to_payload()` on it, exactly as the
    live client does. If the API ever passed a plain dict, the live path would
    raise `AttributeError` and these tests would catch it.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.injected_events: list[dict[str, Any]] = []
        self.audit_limits: list[int] = []
        self.toggles = 0
        self.admin_event_rows: list[dict[str, Any]] = []
        self.audit_rows: list[dict[str, Any]] = [
            {
                "id": index,
                "wall_time": "2026-09-29T10:00:00+00:00",
                "sim_time": "2026-09-29T09:55:00+00:00",
                "tick": TICK,
                "action": f"action.{index}",
                "entity_type": "instance",
                "entity_id": "1",
                "result": "OK",
                "metadata_json": {},
            }
            # Deliberately *not* in id order, and with a duplicate tick: the
            # endpoint promises id-desc (guide 7.12), so an implementation that
            # trusted the simulator's ordering would fail this fixture.
            for index in (3, 1, 9, 7)
        ]

    async def admin_inject_event(self, req: Any) -> Any:
        self._maybe_fail("admin_inject_event")
        if not hasattr(req, "to_payload"):
            raise TypeError(
                f"admin_inject_event expects A2's EventRequest, got {type(req).__name__}"
            )
        payload = req.to_payload()
        self.injected_events.append(payload)
        return SimpleNamespace(
            id=len(self.injected_events),
            type=payload.get("type"),
            start_tick=payload.get("start_tick"),
            end_tick=(payload.get("start_tick") or 0) + (payload.get("duration_ticks") or 0),
            status="SCHEDULED",
            parameters=payload.get("parameters") or {},
        )

    async def admin_get_events(self) -> list[Any]:
        self._maybe_fail("admin_get_events")
        return list(self.admin_event_rows)

    async def admin_toggle(self) -> Any:
        self._maybe_fail("admin_toggle")
        self.toggles += 1
        return await self.get_instance()

    async def get_audit(self, limit: int = 200) -> list[Any]:
        self._maybe_fail("get_audit")
        self.audit_limits.append(limit)
        return list(self.audit_rows)


def _post_event(http: Any, body: dict[str, Any]) -> Any:
    return http.post("/api/v1/admin/events", json=body)


# ---------------------------------------------------------------------------
# Crisis events — all six types (guide sections 7.7, 7.8; brief section 10)
# ---------------------------------------------------------------------------

#: Every type in guide section 7.8, with the parameter shape that section
#: documents for it.
EVENT_CASES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("demand_spike", {"region_ids": ["region-dhaka"], "multiplier": 1.8}),
    ("shipment_delay", {"depot_ids": ["depot-gazipur"], "delay_ticks": 3}),
    ("route_disruption", {"route_ids": ["route-gazipur-mirpur"]}),
    ("station_outage", {"station_ids": ["station-mirpur"]}),
    ("depot_constraint", {"depot_ids": ["depot-patiya"]}),
    ("supply_shortfall", {"depot_ids": [], "fuel_types": ["DIESEL"], "factor": 0.4}),
)


def test_all_six_documented_event_types_are_accepted() -> None:
    """Brief section 10's crisis table is six rows; all six must be injectable."""
    simulator = OperatorFakeSimulator()
    http = client(build_app(client=simulator))

    for event_type, parameters in EVENT_CASES:
        response = _post_event(
            http,
            {
                "type": event_type,
                "start_tick": 8,
                "duration_ticks": 12,
                "parameters": parameters,
            },
        )
        assert response.status_code == 200, f"{event_type}: {response.text}"
        body = response.json()
        assert body["simulated"] is True
        assert body["event"]["type"] == event_type

    assert [e["type"] for e in simulator.injected_events] == [
        event_type for event_type, _ in EVENT_CASES
    ]


def test_the_event_reaches_the_simulator_with_its_documented_shape() -> None:
    """Guide section 7.7's body, verbatim — including `end_tick = start + duration`."""
    simulator = OperatorFakeSimulator()
    body = _post_event(
        client(build_app(client=simulator)),
        {
            "type": "demand_spike",
            "start_tick": 8,
            "duration_ticks": 12,
            "parameters": {"region_ids": ["region-dhaka"], "multiplier": 1.8},
        },
    ).json()

    payload = simulator.injected_events[0]  # the payload A2's client would POST
    assert payload["type"] == "demand_spike"
    assert payload["start_tick"] == 8
    assert payload["duration_ticks"] == 12
    assert payload["parameters"]["region_ids"] == ["region-dhaka"]
    assert payload["parameters"]["multiplier"] == 1.8
    assert body["event"]["end_tick"] == 20


def test_a_zero_duration_event_is_a_422_and_never_reaches_the_simulator() -> None:
    """Guide section 9 lists `duration_ticks=0` among the payloads that are a 422."""
    simulator = OperatorFakeSimulator()
    response = _post_event(
        client(build_app(client=simulator)),
        {"type": "demand_spike", "start_tick": 8, "duration_ticks": 0},
    )
    assert response.status_code == 422
    assert simulator.injected_events == []


def test_a_negative_start_tick_is_a_422() -> None:
    """Guide section 7.7: `start_tick >= 0`."""
    simulator = OperatorFakeSimulator()
    response = _post_event(
        client(build_app(client=simulator)),
        {"type": "demand_spike", "start_tick": -1, "duration_ticks": 5},
    )
    assert response.status_code == 422
    assert simulator.injected_events == []


def test_a_missing_duration_is_a_422() -> None:
    """`duration_ticks` is required (guide 7.7), not defaulted."""
    simulator = OperatorFakeSimulator()
    response = _post_event(
        client(build_app(client=simulator)), {"type": "demand_spike", "start_tick": 0}
    )
    assert response.status_code == 422
    assert simulator.injected_events == []


def test_an_unknown_event_type_is_a_422() -> None:
    """Guide section 7.8's enum is closed; a fabricated type is caught here."""
    simulator = OperatorFakeSimulator()
    response = _post_event(
        client(build_app(client=simulator)),
        {"type": "meteor_strike", "start_tick": 0, "duration_ticks": 5},
    )
    assert response.status_code == 422
    assert simulator.injected_events == []


# ---------------------------------------------------------------------------
# The empty-filter-list rule (guide section 7.8)
# ---------------------------------------------------------------------------


def test_an_empty_region_list_means_every_region_not_no_region() -> None:
    """Guide section 7.8: "if the list is empty, the event applies to all entities
    of that type" — so `[]` must be forwarded as `[]`, never dropped, never turned
    into "no target", and never rejected as a missing scope."""
    simulator = OperatorFakeSimulator()
    response = _post_event(
        client(build_app(client=simulator)),
        {
            "type": "demand_spike",
            "start_tick": 0,
            "duration_ticks": 10,
            "parameters": {"region_ids": [], "station_ids": []},
        },
    )
    assert response.status_code == 200
    parameters = simulator.injected_events[0]["parameters"]
    assert parameters["region_ids"] == []
    assert parameters["station_ids"] == []
    # Guide section 7.8's documented default, applied rather than assumed.
    assert parameters["multiplier"] == 1.5


def test_the_invented_singular_region_id_is_refused() -> None:
    """Guide section 7.8 spells the filter `region_ids`. A singular `region_id` is
    ignored by the simulator, so accepting it would let an operator believe a
    spike was scoped to one region while it hit the whole network."""
    simulator = OperatorFakeSimulator()
    response = _post_event(
        client(build_app(client=simulator)),
        {
            "type": "demand_spike",
            "start_tick": 0,
            "duration_ticks": 10,
            "parameters": {"region_id": "region-dhaka"},
        },
    )
    assert response.status_code == 422
    assert error_code(response) == "invalid_event_parameters"
    assert response.json()["detail"]["details"]["expected"] == "region_ids"
    assert simulator.injected_events == []


def test_a_scalar_parameter_for_the_wrong_event_type_is_refused() -> None:
    """`multiplier` belongs to `demand_spike` alone (guide section 7.8)."""
    simulator = OperatorFakeSimulator()
    response = _post_event(
        client(build_app(client=simulator)),
        {
            "type": "route_disruption",
            "start_tick": 0,
            "duration_ticks": 10,
            "parameters": {"multiplier": 2.0},
        },
    )
    assert response.status_code == 422
    assert simulator.injected_events == []


def test_a_combined_crisis_can_be_injected_in_one_session() -> None:
    """Brief section 10's "combined crisis": two or more disruptions together."""
    simulator = OperatorFakeSimulator()
    http = client(build_app(client=simulator))
    for event_type, parameters in (
        ("demand_spike", {"region_ids": ["region-dhaka"], "multiplier": 1.8}),
        ("shipment_delay", {"depot_ids": [], "delay_ticks": 3}),
        ("route_disruption", {"route_ids": []}),
    ):
        assert (
            _post_event(
                http,
                {
                    "type": event_type,
                    "start_tick": TICK,
                    "duration_ticks": 12,
                    "parameters": parameters,
                },
            ).status_code
            == 200
        )
    assert len(simulator.injected_events) == 3


# ---------------------------------------------------------------------------
# Faults — all five types and the duration window (guide sections 7.9, 7.10)
# ---------------------------------------------------------------------------


def test_all_five_documented_fault_types_are_accepted() -> None:
    """Brief section 22 step 11: "application or dependency failure is injected"."""
    simulator = OperatorFakeSimulator()
    http = client(build_app(client=simulator))

    for fault_type in ("latency", "unavailable", "error_rate", "stale_data", "stream_disconnect"):
        response = http.post(
            "/api/v1/admin/faults",
            json={"type": fault_type, "duration_seconds": 15},
        )
        assert response.status_code == 200, f"{fault_type}: {response.text}"
        assert response.json()["simulated"] is True

    assert [f["type"] for f in simulator.injected_faults] == [
        "latency",
        "unavailable",
        "error_rate",
        "stale_data",
        "stream_disconnect",
    ]


def test_the_documented_fault_parameter_defaults_are_applied() -> None:
    """Guide section 7.10: `delay_ms` defaults to 500, `rate` to 0.25."""
    simulator = OperatorFakeSimulator()
    http = client(build_app(client=simulator))
    http.post("/api/v1/admin/faults", json={"type": "latency", "duration_seconds": 15})
    http.post("/api/v1/admin/faults", json={"type": "error_rate", "duration_seconds": 15})

    assert simulator.injected_faults[0]["parameters"]["delay_ms"] == 500
    assert simulator.injected_faults[1]["parameters"]["rate"] == 0.25


def test_a_zero_second_fault_is_rejected() -> None:
    """Guide section 7.9: `0 < duration_seconds`."""
    simulator = OperatorFakeSimulator()
    response = client(build_app(client=simulator)).post(
        "/api/v1/admin/faults", json={"type": "stale_data", "duration_seconds": 0}
    )
    assert response.status_code == 422
    assert error_code(response) == "invalid_fault_duration"
    assert simulator.injected_faults == []


def test_a_fault_longer_than_an_hour_is_rejected() -> None:
    """Guide section 7.9: `duration_seconds <= 3600`."""
    simulator = OperatorFakeSimulator()
    response = client(build_app(client=simulator)).post(
        "/api/v1/admin/faults", json={"type": "stale_data", "duration_seconds": 3601}
    )
    assert response.status_code == 422
    assert error_code(response) == "invalid_fault_duration"
    assert simulator.injected_faults == []


def test_an_hour_long_fault_is_the_documented_maximum() -> None:
    """The boundary itself is valid — the bound is inclusive at 3600."""
    simulator = OperatorFakeSimulator()
    response = client(build_app(client=simulator)).post(
        "/api/v1/admin/faults", json={"type": "unavailable", "duration_seconds": 3600}
    )
    assert response.status_code == 200
    assert simulator.injected_faults[0]["duration_seconds"] == 3600


def test_a_negative_fault_duration_is_rejected() -> None:
    simulator = OperatorFakeSimulator()
    response = client(build_app(client=simulator)).post(
        "/api/v1/admin/faults", json={"type": "stale_data", "duration_seconds": -1}
    )
    assert response.status_code == 422
    assert simulator.injected_faults == []


def test_a_non_numeric_fault_duration_is_rejected() -> None:
    simulator = OperatorFakeSimulator()
    response = client(build_app(client=simulator)).post(
        "/api/v1/admin/faults", json={"type": "stale_data", "duration_seconds": "ages"}
    )
    assert response.status_code == 422
    assert simulator.injected_faults == []


def test_an_out_of_range_error_rate_is_rejected() -> None:
    """Guide section 7.10's `rate` is a probability."""
    simulator = OperatorFakeSimulator()
    response = client(build_app(client=simulator)).post(
        "/api/v1/admin/faults",
        json={"type": "error_rate", "duration_seconds": 15, "parameters": {"rate": 1.5}},
    )
    assert response.status_code == 422
    assert error_code(response) == "invalid_fault_parameters"
    assert simulator.injected_faults == []


# ---------------------------------------------------------------------------
# Inspection: the audit log (guide section 7.12; brief section 20)
# ---------------------------------------------------------------------------


def test_the_audit_log_is_exposed_for_the_decision_audit_history() -> None:
    simulator = OperatorFakeSimulator()
    body = client(build_app(client=simulator)).get("/api/v1/admin/audit").json()
    assert body["count"] == 4
    assert body["simulated"] is True
    assert body["entries"][0]["action"].startswith("action.")


def test_the_audit_default_limit_is_the_documented_200() -> None:
    """Guide section 7.12: default 200."""
    simulator = OperatorFakeSimulator()
    body = client(build_app(client=simulator)).get("/api/v1/admin/audit").json()
    assert simulator.audit_limits == [200]
    assert body["limit"] == 200


def test_the_audit_limit_is_clamped_to_the_documented_window() -> None:
    """Guide section 7.12: "limit clamped to [1, 1000]".

    Clamped, not rejected: an operator asking for 5000 rows is asking for "all of
    them", so the request must succeed with the ceiling, and `0` must not become
    a silent empty page.
    """
    simulator = OperatorFakeSimulator()
    http = client(build_app(client=simulator))

    assert http.get("/api/v1/admin/audit", params={"limit": 5000}).json()["limit"] == 1000
    assert http.get("/api/v1/admin/audit", params={"limit": 0}).json()["limit"] == 1
    assert http.get("/api/v1/admin/audit", params={"limit": -20}).json()["limit"] == 1
    assert http.get("/api/v1/admin/audit", params={"limit": 250}).json()["limit"] == 250

    assert simulator.audit_limits == [1000, 1, 1, 250], "the clamp must reach the simulator"


def test_audit_entries_are_returned_id_descending() -> None:
    """Guide section 7.12: sorted id-desc. Applied here, not assumed of the peer."""
    simulator = OperatorFakeSimulator()
    body = client(build_app(client=simulator)).get("/api/v1/admin/audit").json()
    ids = [row["id"] for row in body["entries"]]
    assert ids == sorted(ids, reverse=True)
    assert ids == [9, 7, 3, 1]


def test_an_audit_row_that_is_not_a_mapping_does_not_break_the_page() -> None:
    """CONTRACT.md 5.2: tolerate a surprise value, never fail the request."""
    simulator = OperatorFakeSimulator()
    simulator.audit_rows = [{"id": 2, "action": "simulation.tick"}, "not-a-row", None]
    body = client(build_app(client=simulator)).get("/api/v1/admin/audit").json()
    assert body["count"] == 1
    assert body["entries"][0]["id"] == 2


# ---------------------------------------------------------------------------
# Inspection: the fault and event timelines (guide section 7.13)
# ---------------------------------------------------------------------------


def test_the_fault_timeline_is_returned_id_descending() -> None:
    simulator = OperatorFakeSimulator()
    http = client(build_app(client=simulator))
    http.post("/api/v1/admin/faults", json={"type": "latency", "duration_seconds": 15})
    http.post("/api/v1/admin/faults", json={"type": "stale_data", "duration_seconds": 15})

    body = http.get("/api/v1/admin/faults").json()
    assert [row["id"] for row in body["faults"]] == [2, 1]


def test_the_event_timeline_is_exposed() -> None:
    """Guide section 7.13 pairs `GET /admin/events` with `GET /admin/faults`."""
    simulator = OperatorFakeSimulator()
    simulator.admin_event_rows = [
        {"id": 1, "type": "demand_spike", "status": "RESOLVED"},
        {"id": 4, "type": "route_disruption", "status": "ACTIVE"},
    ]
    body = client(build_app(client=simulator)).get("/api/v1/admin/events").json()
    assert body["count"] == 2
    assert body["simulated"] is True
    assert [row["id"] for row in body["events"]] == [4, 1]


def test_a_client_without_an_event_timeline_is_a_typed_503() -> None:
    """`admin_get_events` arrives concurrently; an older client must be named."""
    simulator = OperatorFakeSimulator()
    simulator.admin_get_events = None  # type: ignore[assignment]
    response = client(build_app(client=simulator)).get("/api/v1/admin/events")
    assert response.status_code == 503
    assert error_code(response) == "admin_action_unavailable"


# ---------------------------------------------------------------------------
# Simulation control (guide sections 7.4, 7.5)
# ---------------------------------------------------------------------------


def test_toggle_is_exposed_when_the_client_implements_it() -> None:
    """Guide section 7.4's RUNNING <-> PAUSED convenience for a UI."""
    simulator = OperatorFakeSimulator()
    response = client(build_app(client=simulator)).post("/api/v1/admin/toggle")
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "toggle"
    assert body["status"] == "RUNNING"
    assert body["simulated"] is True
    assert simulator.toggles == 1


def test_toggle_without_client_support_is_a_typed_503_not_a_500() -> None:
    """Called defensively with `getattr`: an older client is named, not crashed."""
    simulator = OperatorFakeSimulator()
    simulator.admin_toggle = None  # type: ignore[assignment]
    response = client(build_app(client=simulator)).post("/api/v1/admin/toggle")
    assert response.status_code == 503
    assert error_code(response) == "admin_action_unavailable"
    assert response.json()["detail"]["details"]["action"] == "toggle"


def test_pause_submit_step_is_reproducible_through_this_api() -> None:
    """Guide section 7.5 calls `/admin/step` "the recommended way to drive
    deterministic tests": pause, act, then step a known number of ticks."""
    simulator = OperatorFakeSimulator()
    http = client(build_app(client=simulator))

    assert http.post("/api/v1/admin/simulation/pause").json()["result"]["status"] == "PAUSED"
    for _ in range(3):
        assert http.post("/api/v1/admin/simulation/step").json()["result"]["tick"] == TICK + 1
    assert simulator.actions == ["pause", "step", "step", "step"]


# ---------------------------------------------------------------------------
# A failing simulator is a typed error, never a 500
# ---------------------------------------------------------------------------


def test_a_simulator_failure_on_event_injection_is_a_typed_error() -> None:
    """Brief section 11: "invalid simulator response -> reject input + raise alert",
    and CONTRACT.md 0.4: every failure path yields a typed error, not a traceback."""
    broken = OperatorFakeSimulator()
    broken.fail_on = {"admin_inject_event"}
    response = _post_event(
        client(build_app(client=broken)),
        {"type": "demand_spike", "start_tick": 0, "duration_ticks": 5},
    )
    assert response.status_code == 502
    assert error_code(response) == "simulator_error"
    assert "Traceback" not in response.text


def test_a_simulator_failure_on_the_audit_log_is_a_typed_error() -> None:
    broken = OperatorFakeSimulator()
    broken.fail_on = {"get_audit"}
    response = client(build_app(client=broken)).get("/api/v1/admin/audit")
    assert response.status_code == 502
    assert error_code(response) == "simulator_error"
    assert "Traceback" not in response.text


def test_a_simulator_failure_on_toggle_is_a_typed_error() -> None:
    broken = OperatorFakeSimulator()
    broken.fail_on = {"admin_toggle"}
    response = client(build_app(client=broken)).post("/api/v1/admin/toggle")
    assert response.status_code == 502
    assert error_code(response) == "simulator_error"


def test_a_simulator_failure_on_the_event_timeline_is_a_typed_error() -> None:
    broken = OperatorFakeSimulator()
    broken.fail_on = {"admin_get_events"}
    response = client(build_app(client=broken)).get("/api/v1/admin/events")
    assert response.status_code == 502
    assert error_code(response) == "simulator_error"


def test_every_domain_rejection_keeps_the_house_error_shape() -> None:
    """Rejections this layer makes carry `detail.{code,message,simulated}`.

    CONTRACT.md 5.3 and section 0.6 make that the one error format the console
    has to understand. Guide section 9 records the other half of the rule — a
    *Pydantic* rejection keeps FastAPI's default `detail` list — so the two are
    asserted separately rather than averaged into one weak assertion.
    """
    simulator = OperatorFakeSimulator()
    http = client(build_app(client=simulator))
    domain_rejections = (
        # An unknown fault type: only this layer can catch it before the
        # simulator's own 422 would be relayed as a 502.
        http.post("/api/v1/admin/faults", json={"type": "nope"}),
        # An out-of-range duration on a fault.
        http.post("/api/v1/admin/faults", json={"type": "stale_data", "duration_seconds": 0}),
        # The singular spelling guide section 7.8 does not have.
        _post_event(
            http,
            {
                "type": "demand_spike",
                "start_tick": 0,
                "duration_ticks": 5,
                "parameters": {"region_id": "region-dhaka"},
            },
        ),
    )
    for response in domain_rejections:
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert isinstance(detail, dict), response.text
        assert isinstance(detail["code"], str) and detail["code"]
        assert isinstance(detail["message"], str) and detail["message"]
        assert detail["simulated"] is True


def test_a_schema_rejection_uses_the_documented_pydantic_shape() -> None:
    """Guide section 9: "Pydantic validation errors use FastAPI's default
    {"detail":[...]}" — and the value never reaches the simulator."""
    simulator = OperatorFakeSimulator()
    response = _post_event(
        client(build_app(client=simulator)),
        {"type": "demand_spike", "start_tick": 0, "duration_ticks": 0},
    )
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    assert simulator.injected_events == []
