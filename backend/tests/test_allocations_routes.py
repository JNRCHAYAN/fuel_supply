"""The allocation ledger and cancellation routes.

Guide sections 4.10 (`GET /v1/allocations`, "your shipment ledger") and 5.5
(`POST /v1/allocations/{id}/cancel`, refunds a PENDING allocation and marks it
CANCELLED), plus guide section 5.4's rule that a used idempotency key is never
freed. Brief section 24 requires the consequential cancellation to keep its
human review, so the explicit `{"confirm": true}` gate is a tested invariant,
not a convention.
"""

from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace
from typing import Any

import test_api_support  # noqa: F401  (puts backend/ on sys.path)

from test_api_support import (
    FakeSimulatorClient,
    build_app,
    client,
    error_code,
)

from app.api import allocations
from app.sim.errors import SimulatorError

ALLOCATION_ID = 700


def _ledger_row(**overrides: Any) -> dict[str, Any]:
    """One documented row from guide section 4.10."""
    row = {
        "id": ALLOCATION_ID,
        "idempotency_key": "demo-001",
        "source_depot_id": "depot-gazipur",
        "destination_station_id": "station-mirpur",
        "route_id": "route-gazipur-mirpur",
        "fuel_type": "DIESEL",
        "quantity": 3000.0,
        "created_tick": 5,
        "departure_tick": 6,
        "expected_arrival_tick": 8,
        "actual_arrival_tick": 8,
        "status": "ARRIVED",
        "failure_reason": None,
    }
    row.update(overrides)
    return row


class LedgerSimulatorClient(FakeSimulatorClient):
    """A fake that models the simulator's *permanent* idempotency ledger.

    Guide section 5.4: "Once used, an idempotency_key is permanently occupied."
    So this fake records every key it has ever seen and keeps it forever, which
    is what lets a test prove the API never puts a key back into circulation.
    """

    def __init__(self, ledger: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.ledger = list(ledger or [])
        self.keys_in_use: dict[str, int] = {}
        self.freed_keys: list[str] = []
        self._key_by_allocation: dict[int, str] = {}

    def free_key(self, key: str) -> None:
        """The simulator has no such operation; this only exists to be asserted
        empty. If the API ever asked for a key back, it would land here."""
        self.freed_keys.append(key)

    async def get_allocations(self) -> list[Any]:
        self._maybe_fail("get_allocations")
        return [SimpleNamespace(**row) for row in self.ledger]

    async def create_allocation(self, req: Any) -> Any:
        key = req.to_payload()["idempotency_key"]
        # setdefault, not overwrite: a replay keeps the same row.
        self.keys_in_use.setdefault(key, len(self.keys_in_use) + 1)
        allocation = await super().create_allocation(req)
        self._key_by_allocation[allocation.id] = key
        return allocation

    async def cancel_allocation(self, allocation_id: int) -> Any:
        self._maybe_fail("cancel_allocation")
        return await super().cancel_allocation(allocation_id)


class RaisingSimulatorClient(FakeSimulatorClient):
    """`cancel_allocation` fails exactly as the live simulator does."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__()
        self._error = SimulatorError(
            message, status_code=status_code, code=code, kind="domain"
        )
        self.cancelled: list[int] = []

    async def cancel_allocation(self, allocation_id: int) -> Any:
        self.cancelled.append(allocation_id)
        raise self._error


# ---------------------------------------------------------------------------
# GET /api/v1/allocations — the ledger
# ---------------------------------------------------------------------------


def test_the_ledger_returns_the_documented_fields() -> None:
    """Guide section 4.10, field for field."""
    simulator = LedgerSimulatorClient([_ledger_row()])
    response = client(build_app(client=simulator)).get("/api/v1/allocations")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["simulated"] is True
    assert body["count"] == 1
    row = body["allocations"][0]
    for field in (
        "id",
        "idempotency_key",
        "source_depot_id",
        "destination_station_id",
        "route_id",
        "fuel_type",
        "quantity",
        "created_tick",
        "departure_tick",
        "expected_arrival_tick",
        "actual_arrival_tick",
        "status",
        "failure_reason",
    ):
        assert field in row, f"{field} is missing from the ledger row"
    assert row["idempotency_key"] == "demo-001"
    assert row["status"] == "ARRIVED"
    assert row["created_tick"] == 5


def test_the_ledger_keeps_the_simulators_id_desc_order() -> None:
    rows = [_ledger_row(id=3), _ledger_row(id=2), _ledger_row(id=1)]
    response = client(build_app(client=LedgerSimulatorClient(rows))).get(
        "/api/v1/allocations"
    )
    assert [row["id"] for row in response.json()["allocations"]] == [3, 2, 1]


def test_the_ledger_status_filter_narrows_without_reordering() -> None:
    rows = [
        _ledger_row(id=5, status="PENDING"),
        _ledger_row(id=4, status="CANCELLED"),
        _ledger_row(id=3, status="PENDING"),
    ]
    response = client(build_app(client=LedgerSimulatorClient(rows))).get(
        "/api/v1/allocations", params={"status": "PENDING"}
    )
    body = response.json()
    assert body["status"] == "PENDING"
    assert [row["id"] for row in body["allocations"]] == [5, 3]
    assert body["count"] == 2


def test_the_ledger_limit_pages_the_sorted_rows() -> None:
    rows = [_ledger_row(id=i) for i in (9, 8, 7, 6)]
    response = client(build_app(client=LedgerSimulatorClient(rows))).get(
        "/api/v1/allocations", params={"limit": 2}
    )
    body = response.json()
    assert body["limit"] == 2
    assert [row["id"] for row in body["allocations"]] == [9, 8]
    assert body["count"] == 2


def test_an_unknown_status_filter_is_a_422() -> None:
    response = client(build_app()).get(
        "/api/v1/allocations", params={"status": "TELEPORTED"}
    )
    assert response.status_code == 422
    assert "PENDING" in response.text


def test_an_out_of_range_limit_is_a_422() -> None:
    http = client(build_app())
    assert http.get("/api/v1/allocations", params={"limit": 0}).status_code == 422
    assert http.get("/api/v1/allocations", params={"limit": 99999}).status_code == 422


def test_a_simulator_failure_on_the_ledger_is_typed() -> None:
    broken = FakeSimulatorClient()
    broken.fail_on = {"get_allocations"}
    response = client(build_app(client=broken)).get("/api/v1/allocations")
    assert response.status_code in (502, 503)
    assert "Traceback" not in response.text


# ---------------------------------------------------------------------------
# POST /api/v1/allocations/{id}/cancel
# ---------------------------------------------------------------------------


def test_cancel_succeeds_for_a_pending_allocation() -> None:
    simulator = LedgerSimulatorClient()
    response = client(build_app(client=simulator)).post(
        f"/api/v1/allocations/{ALLOCATION_ID}/cancel", json={"confirm": True}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["simulated"] is True
    assert body["allocation_id"] == ALLOCATION_ID
    assert body["allocation"]["status"] == "CANCELLED"
    assert simulator.cancelled == [ALLOCATION_ID]


def test_cancel_without_a_body_is_rejected_with_400_and_writes_nothing() -> None:
    simulator = LedgerSimulatorClient()
    response = client(build_app(client=simulator)).post(
        f"/api/v1/allocations/{ALLOCATION_ID}/cancel"
    )
    assert response.status_code == 400
    assert error_code(response) == "confirmation_required"
    assert simulator.cancelled == [], "nothing may reach the simulator without confirm"


def test_cancel_with_confirm_false_is_rejected_with_400_and_writes_nothing() -> None:
    simulator = LedgerSimulatorClient()
    response = client(build_app(client=simulator)).post(
        f"/api/v1/allocations/{ALLOCATION_ID}/cancel", json={"confirm": False}
    )
    assert response.status_code == 400
    assert error_code(response) == "confirmation_required"
    assert simulator.cancelled == []


def test_the_400_comes_before_the_simulator_is_touched() -> None:
    """An unknown id yields 400 without a confirm, not a simulator 404: the guard
    runs first, so it cannot be probed or bypassed by a crafted id."""
    simulator = RaisingSimulatorClient(404, "ALLOCATION_NOT_FOUND", "no such allocation")
    response = client(build_app(client=simulator)).post(
        "/api/v1/allocations/999999/cancel", json={"confirm": False}
    )
    assert response.status_code == 400
    assert error_code(response) == "confirmation_required"
    assert simulator.cancelled == []


def test_a_non_boolean_confirm_is_a_422() -> None:
    response = client(build_app()).post(
        f"/api/v1/allocations/{ALLOCATION_ID}/cancel", json={"confirm": "yes"}
    )
    assert response.status_code == 422


def test_a_non_integer_allocation_id_is_a_422() -> None:
    response = client(build_app()).post(
        "/api/v1/allocations/not-a-number/cancel", json={"confirm": True}
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# The simulator's own 404 / 409 must survive (guide section 9)
# ---------------------------------------------------------------------------


def test_an_unknown_allocation_surfaces_as_a_404() -> None:
    simulator = RaisingSimulatorClient(
        404, "ALLOCATION_NOT_FOUND", "no allocation with id 4321"
    )
    response = client(build_app(client=simulator)).post(
        "/api/v1/allocations/4321/cancel", json={"confirm": True}
    )
    assert response.status_code == 404, response.text
    assert error_code(response) == "ALLOCATION_NOT_FOUND"
    assert response.json()["detail"]["simulated"] is True
    assert "Traceback" not in response.text


def test_cancelling_a_non_pending_allocation_surfaces_as_a_409() -> None:
    simulator = RaisingSimulatorClient(
        409, "CANNOT_CANCEL", "allocation 4321 is IN_TRANSIT, not PENDING"
    )
    response = client(build_app(client=simulator)).post(
        "/api/v1/allocations/4321/cancel", json={"confirm": True}
    )
    assert response.status_code == 409, response.text
    assert error_code(response) == "CANNOT_CANCEL"
    assert response.json()["detail"]["simulated"] is True


# ---------------------------------------------------------------------------
# Guide section 5.4: a used idempotency key is never freed or reused
# ---------------------------------------------------------------------------


def _create_then_cancel(simulator: LedgerSimulatorClient, key: str) -> None:
    http = client(build_app(client=simulator))
    created = http.post(
        "/api/v1/recommendations/rec-1/submit",
        json={"confirm": True, "idempotency_key": key},
    )
    assert created.status_code == 200, created.text
    allocation_id = created.json()["allocation_id"]
    cancelled = http.post(
        f"/api/v1/allocations/{allocation_id}/cancel", json={"confirm": True}
    )
    assert cancelled.status_code == 200, cancelled.text


def test_cancelling_does_not_free_the_idempotency_key() -> None:
    """Guide 5.4: "Cancellation does NOT free the key." """
    simulator = LedgerSimulatorClient()
    _create_then_cancel(simulator, "probe-permanent-key-1")
    assert "probe-permanent-key-1" in simulator.keys_in_use, "the key vanished"
    assert simulator.freed_keys == [], "a key was put back into circulation"


def test_a_key_stays_occupied_after_its_allocation_is_cancelled() -> None:
    """The key is still the *same* occupancy after the cancel — not a new one."""
    simulator = LedgerSimulatorClient()
    _create_then_cancel(simulator, "probe-permanent-key-2")
    holders = [k for k, v in simulator.keys_in_use.items() if k == "probe-permanent-key-2"]
    assert holders == ["probe-permanent-key-2"]
    assert list(simulator.keys_in_use.values()) == [1]


def test_the_cancel_route_never_creates_an_allocation_or_a_key() -> None:
    """The cancel route is addressed by id alone: no create path, no key.

    A structural guard alongside the behavioural one above. Reusing or freeing a
    key means either calling `create_allocation` (the only way to *use* a key) or
    mutating a key field; this fails if a future edit introduces either, even
    without a fake in the way.
    """
    tree = ast.parse(inspect.getsource(allocations))
    referenced = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert "create_allocation" not in referenced, "only the simulator may create an allocation"
    assert "AllocationRequest" not in referenced

    for node in ast.walk(tree):
        assert not isinstance(node, (ast.Delete, ast.AugAssign)), ast.dump(node)
        if isinstance(node, ast.Call):
            called = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            assert called not in {"pop", "clear", "setdefault", "update", "free_key"}, (
                f"{called}() could mutate an idempotency key"
            )


def test_the_ledger_route_is_a_read() -> None:
    """A GET must not be able to occupy, free or reuse a key on any path."""
    simulator = LedgerSimulatorClient([_ledger_row()])
    response = client(build_app(client=simulator)).get("/api/v1/allocations")
    assert response.status_code == 200
    assert simulator.keys_in_use == {}
    assert simulator.freed_keys == []
    assert simulator.allocations == []
