"""`/api/v1/forecast`, `/api/v1/risk`, `/api/v1/recommendations` and the two
recommendation sub-routes."""

from __future__ import annotations

from types import SimpleNamespace

from app.api.intelligence import _derive_idempotency_key, build_history

import test_api_support  # noqa: F401
from test_api_support import (
    FakeAllocator,
    FakeDetector,
    FakeExplanationService,
    FakeForecaster,
    FakeRepository,
    FakeSimulatorClient,
    build_app,
    client,
)

SUBJECT = "rec-1"


# ---------------------------------------------------------------------------
# /api/v1/forecast
# ---------------------------------------------------------------------------


def test_forecast_covers_every_station_and_fuel_in_the_snapshot() -> None:
    body = client(build_app()).get("/api/v1/forecast").json()
    assert body["horizon_ticks"] == 8
    assert body["generated_at_tick"] == 42
    assert body["count"] == 6  # 2 stations x 3 fuels
    assert body["simulated"] is True
    assert {row["station_id"] for row in body["forecasts"]} == {"ST-1", "ST-2"}
    assert {row["fuel_type"] for row in body["forecasts"]} == {"DIESEL", "PETROL", "OCTANE"}


def test_forecast_points_carry_the_interval_and_the_horizon() -> None:
    row = client(build_app()).get("/api/v1/forecast").json()["forecasts"][0]
    assert len(row["points"]) == 8
    point = row["points"][0]
    assert set(point) >= {"tick", "liters", "lower", "upper"}
    assert point["lower"] <= point["liters"] <= point["upper"]
    assert row["method"] == "ewma"
    assert 0.0 <= row["confidence"] <= 1.0


def test_forecast_honours_a_short_horizon() -> None:
    body = client(build_app()).get("/api/v1/forecast", params={"horizon_ticks": 3}).json()
    assert body["horizon_ticks"] == 3
    assert all(len(row["points"]) == 3 for row in body["forecasts"])


def test_forecast_reports_stockout_risk_with_the_station_inventory() -> None:
    body = client(build_app()).get(
        "/api/v1/forecast", params={"station_id": "ST-2", "fuel_type": "DIESEL"}
    ).json()
    assert body["count"] == 1
    row = body["forecasts"][0]
    assert row["inventory_liters"] == 300.0
    assert row["stockout_risk"]["probability"] == 0.8
    assert row["stockout_risk"]["basis"]


def test_forecast_falls_back_when_there_is_no_history() -> None:
    """CONTRACT.md 7.1: few or zero points must still produce a forecast."""
    app = build_app(repository=FakeRepository(empty_history=True))
    body = client(app).get("/api/v1/forecast").json()
    assert body["count"] == 6
    assert all(row["fallback_used"] for row in body["forecasts"])
    assert all(row["method"] == "fallback" for row in body["forecasts"])


def test_forecast_survives_a_dead_simulator_history_endpoint() -> None:
    """The snapshot store is the fallback when the simulator cannot be reached."""
    broken = FakeSimulatorClient()
    broken.fail_on = {"get_demand_history"}
    body = client(build_app(client=broken)).get("/api/v1/forecast").json()
    assert body["count"] == 6


def test_forecast_survives_a_dead_repository() -> None:
    repository = FakeRepository()
    repository.fail_on = {"demand_series"}
    body = client(build_app(repository=repository)).get("/api/v1/forecast").json()
    assert body["count"] == 6
    assert all(row["fallback_used"] for row in body["forecasts"])


def test_one_broken_series_does_not_fail_the_page() -> None:
    class PickyForecaster(FakeForecaster):
        def forecast(self, history, *, horizon_ticks=8):
            points = list(history or [])
            if points and points[0].fuel_type == "OCTANE":
                raise ValueError("degenerate series")
            return super().forecast(history, horizon_ticks=horizon_ticks)

    body = client(build_app(forecaster=PickyForecaster())).get("/api/v1/forecast").json()
    assert body["count"] == 4  # the two OCTANE rows are dropped, not fatal


# ---------------------------------------------------------------------------
# /api/v1/risk
# ---------------------------------------------------------------------------


def test_risk_returns_signals_most_severe_first() -> None:
    body = client(build_app()).get("/api/v1/risk").json()
    assert body["count"] == 2
    assert [s["severity"] for s in body["signals"]] == ["critical", "warning"]
    assert body["signals"][0]["evidence"] == {"mad": 12.0, "z": 6.1}
    assert body["simulated"] is True


def test_risk_filters_by_severity_and_kind() -> None:
    http = client(build_app())
    assert http.get("/api/v1/risk", params={"severity": "critical"}).json()["count"] == 1
    assert http.get("/api/v1/risk", params={"severity": "CRITICAL"}).json()["count"] == 1
    assert http.get("/api/v1/risk", params={"kind": "supply_shortfall"}).json()["count"] == 1
    assert http.get("/api/v1/risk", params={"kind": "nonsense"}).json()["count"] == 0


def test_risk_is_empty_when_there_is_no_evidence() -> None:
    app = build_app(repository=FakeRepository(empty_history=True))
    body = client(app).get("/api/v1/risk").json()
    assert body["count"] == 0
    assert body["signals"] == []


# ---------------------------------------------------------------------------
# /api/v1/recommendations
# ---------------------------------------------------------------------------


def test_recommendations_are_ranked_and_inspectable() -> None:
    """Brief section 9: why, which signals, which constraints, what else."""
    body = client(build_app()).get("/api/v1/recommendations").json()
    assert body["count"] == 1
    assert body["policy"] == "optimizer"
    assert body["simulated"] is True
    rec = body["recommendations"][0]
    assert rec["id"] == SUBJECT
    assert rec["rationale"]
    assert rec["constraints"] == ["route max_shipment", "depot dispatch capacity"]
    assert rec["expected_impact"] == {"risk_before": 0.81, "risk_after": 0.12}
    assert rec["alternatives"][0]["quantity_liters"] == 1800.0
    assert 0.0 <= rec["confidence"] <= 1.0


def test_recommendations_pass_the_signals_and_forecasts_to_the_engine() -> None:
    allocator = FakeAllocator()
    client(build_app(allocator=allocator)).get("/api/v1/recommendations")
    call = allocator.calls[0]
    assert call["signals"] == 2
    assert call["forecasts"] == 6  # the full (station, fuel) grid


def test_recommendations_pass_the_budget_through() -> None:
    allocator = FakeAllocator()
    client(build_app(allocator=allocator)).get(
        "/api/v1/recommendations", params={"budget_liters": 5000}
    )
    assert allocator.calls[0]["budget_liters"] == 5000.0


def test_recommendations_report_the_heuristic_policy_when_the_optimizer_falls_back() -> None:
    body = client(build_app(allocator=FakeAllocator(policy="heuristic"))).get(
        "/api/v1/recommendations"
    ).json()
    assert body["policy"] == "heuristic"


def test_recommendations_never_execute_anything() -> None:
    """Brief section 24: the engine recommends; it never dispatches."""
    simulator = FakeSimulatorClient()
    client(build_app(client=simulator)).get("/api/v1/recommendations")
    assert simulator.allocations == []


def test_an_empty_recommendation_set_is_not_an_error() -> None:
    class QuietAllocator(FakeAllocator):
        def recommend(self, *, snapshot, signals, forecasts, budget_liters=None):
            return []

    body = client(build_app(allocator=QuietAllocator())).get("/api/v1/recommendations").json()
    assert body["count"] == 0
    assert body["recommendations"] == []


# ---------------------------------------------------------------------------
# /api/v1/recommendations/{id}/explanation
# ---------------------------------------------------------------------------


def test_explanation_says_where_it_came_from() -> None:
    body = client(build_app()).get(f"/api/v1/recommendations/{SUBJECT}/explanation").json()
    assert body["source"] == "fallback"
    assert body["degraded"] is True
    assert body["model"] is None
    assert body["text"]
    assert body["recommendation_id"] == SUBJECT
    assert body["signals_considered"] == 2
    assert body["simulated"] is True


def test_explanation_uses_the_llm_path_when_it_is_available() -> None:
    app = build_app(explainer=FakeExplanationService(source="llm"))
    body = client(app).get(f"/api/v1/recommendations/{SUBJECT}/explanation").json()
    assert body["source"] == "llm"
    assert body["degraded"] is False
    assert body["model"] == "deepseek-chat"


def test_explanation_records_the_decision_in_the_audit_history() -> None:
    repository = FakeRepository()
    client(build_app(repository=repository)).get(
        f"/api/v1/recommendations/{SUBJECT}/explanation"
    )
    assert len(repository.decisions) == 1
    assert repository.decisions[0]["recommendation_id"] == SUBJECT


def test_explanation_for_an_unknown_id_is_a_404() -> None:
    response = client(build_app()).get("/api/v1/recommendations/nope/explanation")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/recommendations/{id}/submit
# ---------------------------------------------------------------------------


def test_submit_creates_exactly_one_allocation_from_the_recommendation() -> None:
    simulator = FakeSimulatorClient()
    body = client(build_app(client=simulator)).post(
        f"/api/v1/recommendations/{SUBJECT}/submit",
        json={"confirm": True, "idempotency_key": "demo-1", "note": "approved by ops"},
    ).json()
    assert body["submitted"] is True
    assert body["allocation_id"] == 700
    assert body["allocation"]["status"] == "PENDING"
    assert body["allocation"]["destination_station_id"] == "ST-2"
    assert body["allocation"]["source_depot_id"] == "DP-1"
    assert body["allocation"]["route_id"] == "RT-1"
    assert body["allocation"]["quantity"] == 2500.0
    assert body["allocation"]["idempotency_key"] == "demo-1"
    assert body["simulated"] is True

    assert len(simulator.allocations) == 1
    sent = simulator.allocations[0]  # the payload A2's client would POST
    assert sent["destination_station_id"] == "ST-2"
    assert sent["source_depot_id"] == "DP-1"
    assert sent["route_id"] == "RT-1"
    assert sent["fuel_type"] == "DIESEL"
    assert sent["quantity"] == 2500.0


def test_submit_always_sends_an_idempotency_key() -> None:
    """CONTRACT.md 5.4: the simulator requires one, so a retry is de-duplicated."""
    simulator = FakeSimulatorClient()
    first = client(build_app(client=simulator)).post(
        f"/api/v1/recommendations/{SUBJECT}/submit", json={"confirm": True}
    ).json()
    second = client(build_app(client=simulator)).post(
        f"/api/v1/recommendations/{SUBJECT}/submit", json={"confirm": True}
    ).json()
    keys = [payload["idempotency_key"] for payload in simulator.allocations]
    assert all(keys), keys
    assert keys[0] == keys[1], "the same recommendation in the same tick retries the same key"
    assert first["allocation_id"] == 700
    assert second["allocation_id"] == 701


def test_submit_reports_a_recommendation_it_cannot_execute_as_a_typed_error() -> None:
    """A recommendation missing depot/route/fuel is a 409, never a 500."""

    class NamelessAllocator(FakeAllocator):
        def recommend(self, *, snapshot, signals, forecasts, budget_liters=None):
            out = []
            for rec in super().recommend(
                snapshot=snapshot, signals=signals, forecasts=forecasts,
                budget_liters=budget_liters,
            ):
                payload = rec.__dict__ if hasattr(rec, "__dict__") else dict(rec)
                out.append(SimpleNamespace(**{**payload, "depot_id": None, "route_id": None}))
            return out

    response = client(build_app(allocator=NamelessAllocator())).post(
        f"/api/v1/recommendations/{SUBJECT}/submit", json={"confirm": True}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "recommendation_incomplete"
    assert response.json()["detail"]["details"]["missing"]


def test_submit_can_override_the_quantity() -> None:
    simulator = FakeSimulatorClient()
    body = client(build_app(client=simulator)).post(
        f"/api/v1/recommendations/{SUBJECT}/submit",
        json={"confirm": True, "quantity_liters": 1234.5},
    ).json()
    assert body["allocation"]["quantity"] == 1234.5


def test_submit_records_the_decision_and_its_outcome() -> None:
    repository = FakeRepository()
    body = client(build_app(repository=repository)).post(
        f"/api/v1/recommendations/{SUBJECT}/submit",
        json={"confirm": True, "note": "approved"},
    ).json()
    assert body["decision_id"] == repository.decisions[0]["id"]
    assert repository.outcomes == [
        {
            "decision_id": body["decision_id"],
            "submitted": True,
            "allocation_id": 700,
            "note": "approved",
        }
    ]


def test_submit_still_succeeds_when_the_audit_write_fails() -> None:
    """The allocation is already with the simulator; the response must not lie
    about it. The failure is reported as a null decision_id."""
    repository = FakeRepository()
    repository.fail_on = {"record_decision"}
    body = client(build_app(repository=repository)).post(
        f"/api/v1/recommendations/{SUBJECT}/submit", json={"confirm": True}
    ).json()
    assert body["submitted"] is True
    assert body["allocation_id"] == 700
    assert body["decision_id"] is None


def test_submit_is_idempotent_when_the_simulator_replays_a_key() -> None:
    """CONTRACT.md 5.3: the simulator answers 201 on an idempotency replay."""
    simulator = FakeSimulatorClient()
    http = client(build_app(client=simulator))
    first = http.post(
        f"/api/v1/recommendations/{SUBJECT}/submit",
        json={"confirm": True, "idempotency_key": "same-key"},
    )
    second = http.post(
        f"/api/v1/recommendations/{SUBJECT}/submit",
        json={"confirm": True, "idempotency_key": "same-key"},
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["allocation_id"] == 700
    assert second.json()["allocation_id"] == 701


def test_the_detector_sees_the_history_the_repository_holds() -> None:
    detector = FakeDetector()
    client(build_app(detector=detector)).get("/api/v1/risk")
    assert detector.calls == 1


async def test_the_simulators_own_demand_history_reaches_the_engines() -> None:
    """``demand_liters`` -> ``liters``: the bridge that was missing.

    The simulator names the measurement ``demand_liters``; the engines' pinned
    ``DemandPoint`` names it ``liters``. Projecting a row by field name
    therefore failed on a missing required field on every single row,
    ``build_peer_model`` returned a plain dict, and the caller skipped it -- so
    the simulator's history, which CONTRACT.md 5.5 makes the source of truth,
    never reached the engines, and the persisted store quietly drove every
    forecast instead.

    Nothing else in this suite can catch that: the shared ``FakeSimulatorClient``
    returns no history at all, deliberately, to stay independent of the
    ``DemandPoint`` class.
    """

    class SimulatorWithHistory:
        async def get_demand_history(self, limit: int = 500):
            return [
                {
                    "id": 49212,
                    "station_id": "station-coxsbazar",
                    "fuel_type": "OCTANE",
                    "tick": 4100,
                    "sim_time": "2026-02-12T17:00:00",
                    "demand_liters": 53.255,
                    "served_liters": 0.0,
                    "unmet_liters": 53.255,
                }
            ]

    history = await build_history(None, SimulatorWithHistory(), None)

    points = history[("station-coxsbazar", "OCTANE")]
    assert len(points) == 1
    assert points[0].liters == 53.255
    assert points[0].tick == 4100


# ---------------------------------------------------------------------------
# The idempotency key a submission carries
# ---------------------------------------------------------------------------


def test_the_derived_key_follows_the_decision_content_not_the_clock() -> None:
    """Guide section 5.4 makes a used key permanently occupied on the simulator.

    That memory outlives this process, so a key derived from the tick was wrong
    twice over: a restart replayed a previous run's allocation at the same tick,
    and two different decisions inside one tick collided onto one key. Keying on
    the content keeps only the property worth having.
    """
    fields = {"depot_id": "DP-1", "station_id": "ST-2", "route_id": "RT-1", "fuel_type": "DIESEL"}
    same = _derive_idempotency_key("rec-1", quantity=2500.0, **fields)
    assert _derive_idempotency_key("rec-1", quantity=2500.0, **fields) == same

    assert _derive_idempotency_key("rec-1", quantity=1000.0, **fields) != same
    assert _derive_idempotency_key("rec-1", quantity=2500.0, **{**fields, "route_id": "RT-3"}) != same
    assert _derive_idempotency_key("rec-2", quantity=2500.0, **fields) != same


def test_the_derived_key_fits_the_documented_length_ceiling() -> None:
    """Guide section 5.1: 1-150 characters, so a long id must not overflow it."""
    key = _derive_idempotency_key(
        "r" * 500,
        depot_id="DP-1",
        station_id="ST-2",
        route_id="RT-1",
        fuel_type="DIESEL",
        quantity=2500.0,
    )
    assert 1 <= len(key) <= 150
    # The digest is the part that must survive: truncating the finished string
    # instead would let two different decisions share a key.
    assert key.endswith(key.rsplit("-", 1)[-1])
    assert len(key.rsplit("-", 1)[-1]) == 16


def test_overriding_the_quantity_is_a_new_decision_not_a_replay() -> None:
    """The operator asked for a different size; answering with the old
    allocation would be a silent lie about what was dispatched."""
    simulator = FakeSimulatorClient()
    http = client(build_app(client=simulator))
    http.post(f"/api/v1/recommendations/{SUBJECT}/submit", json={"confirm": True})
    http.post(
        f"/api/v1/recommendations/{SUBJECT}/submit",
        json={"confirm": True, "quantity_liters": 1000.0},
    )
    keys = [payload["idempotency_key"] for payload in simulator.allocations]
    assert keys[0] != keys[1], "a different size is a different decision"


def test_a_non_positive_quantity_is_refused_before_the_simulator_is_called() -> None:
    simulator = FakeSimulatorClient()
    http = client(build_app(client=simulator))
    for bad in (0, -5):
        response = http.post(
            f"/api/v1/recommendations/{SUBJECT}/submit",
            json={"confirm": True, "quantity_liters": bad},
        )
        assert response.status_code == 422, bad
    assert simulator.allocations == [], "a refused request must not reach the simulator"


def test_an_infinite_quantity_is_refused_at_the_boundary() -> None:
    """`gt=0` alone accepts `inf`, which serialises as the non-standard JSON
    literal `Infinity` and reaches the simulator as an unparseable body."""
    simulator = FakeSimulatorClient()
    response = client(build_app(client=simulator)).post(
        f"/api/v1/recommendations/{SUBJECT}/submit",
        json={"confirm": True, "quantity_liters": 1e999},
    )
    assert response.status_code == 422
    assert simulator.allocations == []


def test_a_recommendation_with_no_usable_size_is_a_typed_409() -> None:
    """The size can also come from the recommendation, which no request model
    ever validates. It must fail as a typed, explainable error."""

    class SizelessAllocator(FakeAllocator):
        def recommend(self, *, snapshot, signals, forecasts, budget_liters=None):
            return [
                SimpleNamespace(**{**rec.__dict__, "quantity_liters": None})
                for rec in super().recommend(
                    snapshot=snapshot, signals=signals, forecasts=forecasts,
                    budget_liters=budget_liters,
                )
            ]

    response = client(build_app(allocator=SizelessAllocator())).post(
        f"/api/v1/recommendations/{SUBJECT}/submit", json={"confirm": True}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "recommendation_incomplete"
    assert response.json()["detail"]["details"]["missing"] == ["quantity"]


def test_a_recommendation_spelling_its_id_differently_still_resolves() -> None:
    """`id` is pinned, but a drift to `recommendation_id` across this seam fails
    closed: every lookup would 404 and a live recommendation would be reported
    as non-existent."""

    class AliasedAllocator(FakeAllocator):
        def recommend(self, *, snapshot, signals, forecasts, budget_liters=None):
            out = []
            for rec in super().recommend(
                snapshot=snapshot, signals=signals, forecasts=forecasts,
                budget_liters=budget_liters,
            ):
                payload = {**rec.__dict__, "recommendation_id": rec.__dict__["id"]}
                del payload["id"]
                out.append(SimpleNamespace(**payload))
            return out

    response = client(build_app(allocator=AliasedAllocator())).get(
        f"/api/v1/recommendations/{SUBJECT}/explanation"
    )
    assert response.status_code == 200, response.text[:200]
    assert response.json()["recommendation_id"] == SUBJECT
