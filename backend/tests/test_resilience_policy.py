"""Tests for app/resilience/policy.py (A10).

The policy is the platform's written answer to brief §11, so the tests assert
the two things that make it worth having:

* every path CONTRACT §11 names is retrievable and names a *concrete* action;
* an undeclared path is refused or explicitly flagged — it never silently
  succeeds, because a silent success is how a degradation path ends up
  undocumented in front of a judge.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Import the module under test the way the application does. backend/ is the
# sys.path root the app runs from, so put it there if pytest has not.
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.resilience.policy import (  # noqa: E402
    ACTIONS,
    COMPONENT_DATABASE,
    COMPONENT_DECISION_ENGINE,
    COMPONENT_FORECAST,
    COMPONENT_LLM,
    COMPONENT_SIMULATOR,
    DEFAULT_POLICY,
    LOW_CONFIDENCE_THRESHOLD,
    UNKNOWN_PATH_ACTION,
    FallbackPolicy,
    UnknownPolicyPath,
    action_for,
    degraded_envelope,
    is_declared,
    is_low_confidence,
    policy_for,
    requires_human_review,
    resolve,
)


# ---------------------------------------------------------------------------
# CONTRACT §11 — the five paths, word for word.
# ---------------------------------------------------------------------------

CONTRACT_PATHS = [
    # (component, condition, expected action) — CONTRACT.md §11
    ("llm unavailable -> templated explanation",
     COMPONENT_LLM, "unavailable", "templated_explanation"),
    ("simulator open-circuit -> last-good cached snapshot marked stale",
     COMPONENT_SIMULATOR, "circuit_open", "serve_last_good_snapshot_stale"),
    ("optimiser infeasible -> priority heuristic",
     COMPONENT_DECISION_ENGINE, "optimizer_infeasible", "priority_heuristic"),
    ("low confidence -> human review",
     COMPONENT_DECISION_ENGINE, "low_confidence", "human_review_required"),
    ("database unavailable -> serve from memory, mark degraded",
     COMPONENT_DATABASE, "unavailable", "serve_from_memory_degraded"),
]


@pytest.mark.parametrize(
    "component,condition,expected_action",
    [p[1:] for p in CONTRACT_PATHS],
    ids=[p[0] for p in CONTRACT_PATHS],
)
def test_contract_paths_are_declared_with_the_stated_action(
    component: str, condition: str, expected_action: str
) -> None:
    rule = policy_for(component, condition)
    assert rule.action == expected_action
    assert rule.declared is True
    assert rule.trigger.strip(), "a declared path must explain its trigger"


# ---------------------------------------------------------------------------
# Every declared path is retrievable and concrete.
# ---------------------------------------------------------------------------

def test_there_are_multiple_declared_paths() -> None:
    # A policy with fewer than the five contract paths has lost requirement
    # coverage; fail loudly rather than pass vacuously.
    assert len(DEFAULT_POLICY.declared_paths()) >= 5
    assert len(set(DEFAULT_POLICY.declared_paths())) == len(DEFAULT_POLICY)


@pytest.mark.parametrize(
    "component,condition", DEFAULT_POLICY.declared_paths()
)
def test_every_declared_path_resolves_to_a_concrete_action(
    component: str, condition: str
) -> None:
    rule = policy_for(component, condition)
    assert rule.component == component
    assert rule.condition == condition

    # The action must be a real entry in the catalogue, not a dangling string.
    assert rule.action in ACTIONS
    action = action_for(component, condition)
    assert action.name == rule.action

    # ...and it must say something concrete.
    assert action.description.strip()
    assert action.metric.strip()
    assert action.log_event.strip()
    assert action.status_label in {"healthy", "degraded", "down"}
    # Reaching a degradation path is never "healthy".
    assert action.status_label != "healthy"


def test_action_catalogue_is_consistent() -> None:
    for name, action in ACTIONS.items():
        assert name == action.name
    assert len(ACTIONS) == len({a.name for a in ACTIONS.values()})


def test_every_catalogue_action_except_the_default_is_reachable() -> None:
    # An action nothing selects is dead weight; the documented default is the
    # one deliberate exception, because it is returned by resolve(), not by a
    # rule.
    referenced = {rule.action for rule in DEFAULT_POLICY}
    assert UNKNOWN_PATH_ACTION.name not in referenced
    for action in ACTIONS.values():
        if action.name != UNKNOWN_PATH_ACTION.name:
            assert action.name in referenced, f"{action.name} is unreachable"


def test_for_component_lists_only_that_component() -> None:
    for component in (
        COMPONENT_LLM,
        COMPONENT_SIMULATOR,
        COMPONENT_DECISION_ENGINE,
        COMPONENT_DATABASE,
        COMPONENT_FORECAST,
    ):
        rules = DEFAULT_POLICY.for_component(component)
        assert rules, f"{component} has no declared path"
        assert all(r.component == component for r in rules)


def test_a_component_has_more_than_one_declared_path_where_it_matters() -> None:
    # The simulator alone can fail in more than one way (open circuit with and
    # without a cache, an invalid payload, a dropped stream), so the policy must
    # distinguish them rather than collapsing them into one vague "down".
    assert len(DEFAULT_POLICY.for_component(COMPONENT_SIMULATOR)) >= 2


# ---------------------------------------------------------------------------
# Unknown paths must not succeed silently.
# ---------------------------------------------------------------------------

def test_unknown_component_raises() -> None:
    with pytest.raises(UnknownPolicyPath) as exc:
        policy_for("quantum_flux_capacitor", "unavailable")
    assert exc.value.component == "quantum_flux_capacitor"
    # The error must be actionable: it lists what *is* declared.
    assert "llm.unavailable" in str(exc.value)


def test_unknown_condition_raises() -> None:
    with pytest.raises(UnknownPolicyPath):
        policy_for(COMPONENT_LLM, "mildly_confused")


def test_unknown_path_is_not_contained() -> None:
    assert is_declared(COMPONENT_LLM, "nonsense") is False
    assert (COMPONENT_LLM, "nonsense") not in DEFAULT_POLICY
    assert "llm.nonsense" not in DEFAULT_POLICY
    assert (COMPONENT_LLM, "unavailable") in DEFAULT_POLICY
    assert "llm.unavailable" in DEFAULT_POLICY


def test_unknown_path_raises_through_every_lookup() -> None:
    with pytest.raises(UnknownPolicyPath):
        action_for(COMPONENT_DATABASE, "covered_in_toast")
    with pytest.raises(UnknownPolicyPath):
        DEFAULT_POLICY.status_label(COMPONENT_DATABASE, "covered_in_toast")


def test_resolve_returns_a_documented_default_not_a_silent_success() -> None:
    rule = resolve(COMPONENT_LLM, "not_a_real_condition")

    # It must be impossible to mistake this for a declared path...
    assert rule.declared is False
    assert rule.action == UNKNOWN_PATH_ACTION.name
    assert rule.component == COMPONENT_LLM
    assert "Not declared" in rule.trigger

    # ...and the action itself must refuse to substitute a value.
    default = DEFAULT_POLICY.actions[rule.action]
    assert default.substitutes_value is False
    assert "must not substitute" in default.description


def test_resolve_still_returns_real_rules_for_declared_paths() -> None:
    rule = resolve(COMPONENT_SIMULATOR, "circuit_open")
    assert rule.declared is True
    assert rule.action == "serve_last_good_snapshot_stale"


def test_try_policy_for_returns_none_rather_than_raising() -> None:
    assert DEFAULT_POLICY.try_policy_for(COMPONENT_LLM, "nope") is None
    assert DEFAULT_POLICY.try_policy_for(COMPONENT_LLM, "unavailable") is not None


# ---------------------------------------------------------------------------
# Behaviour the other modules depend on.
# ---------------------------------------------------------------------------

def test_llm_absence_is_degraded_not_down() -> None:
    # CONTRACT §9: an absent DEEPSEEK_API_KEY is a supported mode, not a
    # failure. A policy that reported "down" here would contradict the status
    # endpoint.
    assert DEFAULT_POLICY.status_label(COMPONENT_LLM, "unavailable") == "degraded"


def test_breaker_open_without_a_cache_is_down_and_does_not_fabricate() -> None:
    action = action_for(COMPONENT_SIMULATOR, "circuit_open_no_cache")
    assert action.status_label == "down"
    assert action.substitutes_value is False


def test_breaker_open_with_a_cache_serves_stale_data() -> None:
    action = action_for(COMPONENT_SIMULATOR, "circuit_open")
    assert action.substitutes_value is True
    assert action.status_label == "degraded"


def test_optimizer_fallback_is_reachable_from_both_triggers() -> None:
    # brief §11 "ML model unavailable -> fallback allocation policy" and
    # CONTRACT §7.3 "used when the optimizer is unavailable, infeasible, or
    # errors" are the same fallback from two triggers.
    infeasible = action_for(COMPONENT_DECISION_ENGINE, "optimizer_infeasible")
    unavailable = action_for(COMPONENT_DECISION_ENGINE, "optimizer_unavailable")
    assert infeasible.name == unavailable.name == "priority_heuristic"


def test_low_confidence_flags_human_review() -> None:
    for component in (COMPONENT_DECISION_ENGINE, COMPONENT_FORECAST):
        action = action_for(component, "low_confidence")
        assert action.requires_human_review is True
    # The five non-review paths must not ask for review on every request.
    for _label, component, condition, _action in CONTRACT_PATHS:
        if condition != "low_confidence":
            assert action_for(component, condition).requires_human_review is False


def test_confidence_threshold_helpers() -> None:
    assert is_low_confidence(0.0) is True
    assert is_low_confidence(LOW_CONFIDENCE_THRESHOLD - 0.01) is True
    # The boundary is not "low": at the threshold the item is actionable.
    assert is_low_confidence(LOW_CONFIDENCE_THRESHOLD) is False
    assert is_low_confidence(0.99) is False

    assert requires_human_review(0.2) is True
    assert requires_human_review(0.8) is False
    # An explicit threshold must be honoured, so an engine with its own
    # calibration is not forced onto the platform default.
    assert requires_human_review(0.7, threshold=0.75) is True


# ---------------------------------------------------------------------------
# The degraded envelope handed to the API layer.
# ---------------------------------------------------------------------------

def test_degraded_envelope_marks_the_response() -> None:
    payload = {"explanation": "Depot 3 is constrained.", "source": "fallback"}
    out = degraded_envelope(payload, component=COMPONENT_LLM,
                            condition="unavailable")

    assert out["degraded"] is True
    assert out["degraded_reason"] == "llm.unavailable"
    assert out["fallback"] == "templated_explanation"
    # brief §24 / CONTRACT §0.6: a response carrying an explanation or a
    # prediction always says it is simulated.
    assert out["simulated"] is True
    # The payload survives.
    assert out["explanation"] == payload["explanation"]
    assert out["source"] == "fallback"


def test_degraded_envelope_does_not_mutate_its_input() -> None:
    payload = {"keep": "me"}
    degraded_envelope(payload, component=COMPONENT_DATABASE,
                      condition="unavailable")
    assert payload == {"keep": "me"}


def test_degraded_envelope_uses_an_explicit_reason_when_given() -> None:
    out = degraded_envelope(
        {}, component=COMPONENT_SIMULATOR, condition="circuit_open",
        reason="breaker open for 32s; serving 41s-old snapshot",
    )
    assert out["degraded_reason"] == "breaker open for 32s; serving 41s-old snapshot"
    assert out["fallback"] == "serve_last_good_snapshot_stale"


def test_degraded_envelope_flags_an_undeclared_path() -> None:
    out = degraded_envelope({}, component=COMPONENT_LLM, condition="melted")
    assert out["degraded"] is True
    assert out["fallback"] == UNKNOWN_PATH_ACTION.name
    assert out["fallback_declared"] is False
    # A declared path must not carry the "undeclared" marker.
    declared = degraded_envelope({}, component=COMPONENT_LLM,
                                 condition="unavailable")
    assert "fallback_declared" not in declared


# ---------------------------------------------------------------------------
# The policy object is data — a custom policy is constructible and validated.
# ---------------------------------------------------------------------------

def test_a_policy_rejects_a_rule_naming_an_unknown_action() -> None:
    from app.resilience.policy import FallbackRule

    with pytest.raises(ValueError, match="unknown action"):
        FallbackPolicy(
            actions=ACTIONS,
            rules=(FallbackRule(component="llm", condition="x",
                                action="does_not_exist", trigger="t"),),
        )


def test_a_policy_rejects_duplicate_rules() -> None:
    from app.resilience.policy import FallbackRule

    rule = FallbackRule(component="llm", condition="unavailable",
                        action="templated_explanation", trigger="t")
    with pytest.raises(ValueError, match="duplicate rule"):
        FallbackPolicy(actions=ACTIONS, rules=(rule, rule))


def test_declared_paths_are_addressable_in_all_three_shapes() -> None:
    # A caller may hold the pair, or a "component.condition" string.
    assert DEFAULT_POLICY.is_declared(COMPONENT_DATABASE, "unavailable")
    assert (COMPONENT_DATABASE, "unavailable") in DEFAULT_POLICY
    assert "database.unavailable" in DEFAULT_POLICY
    assert len(DEFAULT_POLICY) == len(DEFAULT_POLICY.declared_paths())
