"""`/api/v1/decisions` and `/api/v1/decisions/{id}` — the decision audit history.

Brief section 20: the operator must be able to see what the system recommended,
what was done about it, and when. CONTRACT.md section 6 makes the `decisions`
table that record; these tests cover the two read routes over it.
"""

from __future__ import annotations

from types import SimpleNamespace

from test_api_support import FakeRepository, build_app, client, error_code

SUBJECT = "rec-1"


def _seed(repository: FakeRepository) -> FakeRepository:
    """One recorded decision with its outcome, the way a submit leaves it."""
    repository.decisions.append(
        {
            "id": 1,
            "recommendation_id": SUBJECT,
            "station_id": "ST-2",
            "fuel_type": "DIESEL",
            "quantity_liters": 2500.0,
            "policy": "optimizer",
            "tick": 42,
            "sim_time": "2026-09-29T10:00:00Z",
        }
    )
    repository.outcomes.append(
        {
            "decision_id": 1,
            "submitted": True,
            "allocation_id": 700,
            "note": "approved by ops",
        }
    )
    return repository


# ---------------------------------------------------------------------------
# GET /api/v1/decisions
# ---------------------------------------------------------------------------


def test_the_history_is_empty_before_anything_is_decided() -> None:
    body = client(build_app()).get("/api/v1/decisions").json()
    assert body["count"] == 0
    assert body["decisions"] == []
    assert body["simulated"] is True


def test_the_history_lists_what_was_decided() -> None:
    body = client(build_app(repository=_seed(FakeRepository()))).get(
        "/api/v1/decisions"
    ).json()
    assert body["count"] == 1
    row = body["decisions"][0]
    assert row["id"] == 1
    assert row["recommendation_id"] == SUBJECT
    assert row["station_id"] == "ST-2"
    assert row["quantity_liters"] == 2500.0
    assert row["policy"] == "optimizer"


def test_the_history_pages_with_limit_and_offset() -> None:
    repository = _seed(FakeRepository())
    for decision_id in (2, 3):
        repository.decisions.append({"id": decision_id, "recommendation_id": f"rec-{decision_id}"})

    http = client(build_app(repository=repository))
    first = http.get("/api/v1/decisions", params={"limit": 2}).json()
    second = http.get("/api/v1/decisions", params={"limit": 2, "offset": 2}).json()
    assert [row["id"] for row in first["decisions"]] == [1, 2]
    assert [row["id"] for row in second["decisions"]] == [3]
    assert second["offset"] == 2


def test_the_history_rejects_a_nonsense_limit() -> None:
    http = client(build_app())
    assert http.get("/api/v1/decisions", params={"limit": 0}).status_code == 422
    assert http.get("/api/v1/decisions", params={"limit": 5000}).status_code == 422
    assert http.get("/api/v1/decisions", params={"offset": -1}).status_code == 422


def test_a_broken_repository_is_a_typed_503_not_a_stack_trace() -> None:
    repository = FakeRepository()
    repository.fail_on = {"list_decisions"}
    response = client(build_app(repository=repository)).get("/api/v1/decisions")
    assert response.status_code == 503
    assert error_code(response) == "dependency_unavailable"
    assert response.json()["detail"]["details"]["dependency"] == "Repository.list_decisions"
    assert "Traceback" not in response.text


def test_a_broken_repository_is_recorded_against_the_metrics() -> None:
    from test_api_support import FakeMetrics

    metrics = FakeMetrics()
    repository = FakeRepository()
    repository.fail_on = {"list_decisions"}
    client(build_app(repository=repository, metrics=metrics)).get("/api/v1/decisions")
    assert metrics.api_errors, "a dependency failure must be visible in the metrics"
    assert "repository_error" in metrics.api_errors


# ---------------------------------------------------------------------------
# GET /api/v1/decisions/{id}
# ---------------------------------------------------------------------------


def test_a_decision_is_returned_with_its_outcome_split_out() -> None:
    """The console shows the recommendation and what was done about it."""
    body = client(build_app(repository=_seed(FakeRepository()))).get(
        "/api/v1/decisions/1"
    ).json()
    assert body["decision"]["id"] == 1
    assert body["decision"]["recommendation_id"] == SUBJECT
    assert body["outcome"] == {
        "submitted": True,
        "allocation_id": 700,
        "note": "approved by ops",
    }
    # the outcome fields belong to the outcome block, not to the decision itself
    assert "allocation_id" not in body["decision"]
    assert body["simulated"] is True


def test_a_decision_without_an_outcome_reports_none() -> None:
    repository = FakeRepository()
    repository.decisions.append({"id": 9, "recommendation_id": SUBJECT})
    body = client(build_app(repository=repository)).get("/api/v1/decisions/9").json()
    assert body["decision"]["id"] == 9
    assert body["outcome"] is None


def test_an_unknown_decision_is_a_typed_404() -> None:
    response = client(build_app()).get("/api/v1/decisions/424242")
    assert response.status_code == 404
    assert error_code(response) == "decision_not_found"
    assert response.json()["detail"]["details"]["decision_id"] == 424242


def test_a_non_integer_decision_id_is_a_422() -> None:
    """A path that can never resolve is rejected as input, not looked up."""
    response = client(build_app()).get("/api/v1/decisions/not-a-number")
    assert response.status_code == 422


def test_a_broken_repository_is_a_typed_503_on_the_detail_route() -> None:
    repository = _seed(FakeRepository())
    repository.fail_on = {"get_decision"}
    response = client(build_app(repository=repository)).get("/api/v1/decisions/1")
    assert response.status_code == 503
    assert error_code(response) == "dependency_unavailable"
    assert response.json()["detail"]["details"]["dependency"] == "Repository.get_decision"


def test_a_decision_with_an_unexpected_shape_still_renders() -> None:
    """A3 may rename a field; that degrades the display, it is not a 500."""

    async def weird_get_decision(decision_id: int) -> object:
        return SimpleNamespace(id=decision_id, something_new="value", nested={"a": 1})

    repository = FakeRepository()
    repository.get_decision = weird_get_decision  # type: ignore[method-assign]
    body = client(build_app(repository=repository)).get("/api/v1/decisions/5").json()
    assert body["decision"]["something_new"] == "value"
    assert body["decision"]["nested"] == {"a": 1}
