"""`/api/v1/events`, `/api/v1/events/summary`, `/api/v1/investigate`.

Brief section 11: when the LLM is unavailable the deterministic fallback answers
and says so. Neither route may 500 because of a missing key or a dead provider.
"""

from __future__ import annotations

from test_api_support import (
    EVENT_TYPES,
    SECRET_SENTINEL,
    FakeExplanationService,
    FakeRepository,
    FakeSimulatorClient,
    build_app,
    client,
    error_code,
    fake_settings,
)


# ---------------------------------------------------------------------------
# GET /api/v1/events
# ---------------------------------------------------------------------------


def test_the_fixture_only_uses_the_simulators_real_event_types() -> None:
    """A fabricated type would make the filter tests below prove nothing.

    The vocabulary comes from the simulator's own OpenAPI schema; this asserts
    the fixture stays inside it.
    """
    assert set(FakeSimulatorClient.EVENT_TYPES_FIXTURE) <= set(EVENT_TYPES)


def test_events_are_listed_with_their_windows() -> None:
    body = client(build_app()).get("/api/v1/events").json()
    assert body["count"] == 2
    assert body["simulated"] is True
    first = body["events"][0]
    assert first["type"] == "route_disruption"
    assert first["type"] in EVENT_TYPES
    assert first["start_tick"] is not None
    assert first["end_tick"] > first["start_tick"]
    assert first["parameters"] == {"region_id": "R-1"}


def test_events_filter_by_status_and_type() -> None:
    http = client(build_app())
    # statuses come back UPPERCASE from the simulator, so the filter is
    # case-insensitive on both sides.
    assert http.get("/api/v1/events", params={"status": "active"}).json()["count"] == 1
    assert http.get("/api/v1/events", params={"status": "ACTIVE"}).json()["count"] == 1
    assert http.get("/api/v1/events", params={"status": "resolved"}).json()["count"] == 1
    assert http.get("/api/v1/events", params={"type": "demand_spike"}).json()["count"] == 1
    assert http.get("/api/v1/events", params={"type": "DEMAND_SPIKE"}).json()["count"] == 1
    assert http.get("/api/v1/events", params={"type": "route_disruption"}).json()["count"] == 1
    # a real type with no current event is an empty page, not an error
    assert http.get("/api/v1/events", params={"type": "supply_shortfall"}).json()["count"] == 0
    assert http.get("/api/v1/events", params={"type": "road_closure"}).json()["count"] == 0


def test_events_accept_a_bare_json_array() -> None:
    """The simulator returns `[{...}]`, not an object envelope (verified live)."""
    body = client(build_app()).get("/api/v1/events").json()
    assert isinstance(body["events"], list)
    assert body["count"] == 2

    class EnvelopeClient(FakeSimulatorClient):
        async def get_events(self) -> object:
            return {"events": [{"id": 1, "type": "demand_spike", "status": "ACTIVE"}]}

    # A wrapped payload degrades to one row rather than raising.
    wrapped = client(build_app(client=EnvelopeClient())).get("/api/v1/events").json()
    assert wrapped["count"] == 1


def test_events_honour_the_limit() -> None:
    body = client(build_app()).get("/api/v1/events", params={"limit": 1}).json()
    assert body["count"] == 1
    assert len(body["events"]) == 1


def test_events_reject_a_nonsense_limit() -> None:
    http = client(build_app())
    assert http.get("/api/v1/events", params={"limit": 0}).status_code == 422
    assert http.get("/api/v1/events", params={"limit": 501}).status_code == 422


def test_a_dead_simulator_is_a_typed_502_not_a_stack_trace() -> None:
    broken = FakeSimulatorClient()
    broken.fail_on = {"get_events"}
    response = client(build_app(client=broken)).get("/api/v1/events")
    assert response.status_code == 502
    assert error_code(response) == "simulator_error"
    assert "Traceback" not in response.text


# ---------------------------------------------------------------------------
# POST /api/v1/events/summary
# ---------------------------------------------------------------------------


def test_the_summary_uses_the_deterministic_fallback_when_there_is_no_llm() -> None:
    explainer = FakeExplanationService(source="fallback")
    body = client(build_app(explainer=explainer)).post(
        "/api/v1/events/summary", json={"focus": "R-1"}
    ).json()
    assert body["source"] == "fallback"
    assert body["degraded"] is True
    assert body["model"] is None
    assert body["text"]
    assert body["focus"] == "R-1"
    assert body["signals_considered"] == 2
    assert body["simulated"] is True
    assert explainer.calls == ["summarize_network"]


def test_the_summary_uses_the_llm_when_it_is_available() -> None:
    body = client(build_app(explainer=FakeExplanationService(source="llm"))).post(
        "/api/v1/events/summary", json={}
    ).json()
    assert body["source"] == "llm"
    assert body["degraded"] is False
    assert body["model"] == "deepseek-chat"


def test_the_summary_body_is_entirely_optional() -> None:
    """A POST with no body at all is a valid "summarise the network"."""
    response = client(build_app()).post("/api/v1/events/summary")
    assert response.status_code == 200
    assert response.json()["focus"] is None


def test_the_summary_survives_a_dead_explanation_service() -> None:
    """Brief section 11: the page must degrade, not break."""
    response = client(build_app(explainer=FakeExplanationService(boom=True))).post(
        "/api/v1/events/summary"
    )
    assert response.status_code == 503
    assert error_code(response) == "dependency_unavailable"
    assert "exploded" in response.text or "ExplanationService" in response.text


def test_the_summary_still_works_when_one_broken_component_is_ahead_of_it() -> None:
    """The snapshot store is the fallback for history; the LLM path is unaffected."""
    repository = FakeRepository(empty_history=True)
    repository.fail_on = {"demand_series"}
    body = client(build_app(repository=repository)).post("/api/v1/events/summary").json()
    assert body["text"]


# ---------------------------------------------------------------------------
# POST /api/v1/investigate
# ---------------------------------------------------------------------------


def test_investigate_answers_the_operator_question() -> None:
    explainer = FakeExplanationService(source="llm")
    body = client(build_app(explainer=explainer)).post(
        "/api/v1/investigate",
        json={"question": "why is ST-2 short of diesel?", "context": {"station": "ST-2"}},
    ).json()
    assert body["question"] == "why is ST-2 short of diesel?"
    assert body["text"]
    assert body["source"] == "llm"
    assert body["degraded"] is False
    assert body["simulated"] is True
    assert explainer.calls == ["investigate"]


def test_investigate_sends_only_structured_facts_and_no_credentials() -> None:
    """CONTRACT.md 0.2: a secret must never leave the process, even in a prompt."""
    seen: dict[str, object] = {}

    class RecordingService(FakeExplanationService):
        async def investigate(self, question: str, *, context: dict) -> object:
            seen.update(context)
            return super()._result("ok")

    settings = fake_settings()  # carries SECRET_SENTINEL as the API key
    client(build_app(explainer=RecordingService(), settings=settings)).post(
        "/api/v1/investigate", json={"question": "what happened?"}
    )
    assert seen["tick"] == 42
    assert seen["instance_status"] == "RUNNING"
    assert len(seen["signals"]) == 2
    blob = str(seen)
    assert SECRET_SENTINEL not in blob
    assert "deepseek_api_key" not in blob.lower()
    assert "DEEPSEEK_API_KEY" not in blob


def test_investigate_requires_a_question() -> None:
    http = client(build_app())
    assert http.post("/api/v1/investigate", json={}).status_code == 422
    assert http.post("/api/v1/investigate", json={"question": ""}).status_code == 422
    assert http.post("/api/v1/investigate", json={"question": "ab"}).status_code == 422


def test_investigate_rejects_a_question_that_is_too_long() -> None:
    http = client(build_app())
    response = http.post("/api/v1/investigate", json={"question": "x" * 1001})
    assert response.status_code == 422


def test_investigate_falls_back_rather_than_failing_when_the_llm_is_missing() -> None:
    body = client(build_app(explainer=FakeExplanationService(source="fallback"))).post(
        "/api/v1/investigate", json={"question": "what should we watch?"}
    ).json()
    assert body["source"] == "fallback"
    assert body["degraded"] is True


def test_investigate_survives_a_dead_explanation_service() -> None:
    response = client(build_app(explainer=FakeExplanationService(boom=True))).post(
        "/api/v1/investigate", json={"question": "why?"}
    )
    assert response.status_code == 503
    assert error_code(response) == "dependency_unavailable"


def test_neither_post_echoes_the_api_key_in_an_error() -> None:
    http = client(build_app(explainer=FakeExplanationService(boom=True)))
    for response in (
        http.post("/api/v1/events/summary"),
        http.post("/api/v1/investigate", json={"question": "why?"}),
    ):
        assert SECRET_SENTINEL not in response.text
