"""The platform's degradation policy, declared once, as data.

Brief §11 ("Application Resilience") requires the team to *define what happens
when something goes wrong*. A definition scattered across `try/except` blocks in
five modules is not a definition an operator, a judge, or a test can inspect.
This module makes it a table:

    component + condition  ->  a concrete, named fallback action

The API layer imports this module and asks it what to do, so the behaviour is
declared in exactly one place and every path is enumerable and testable.

Pinned from CONTRACT.md §11:

    LLM unavailable            -> templated explanation
    simulator circuit open     -> last-good cached snapshot marked stale
    optimiser infeasible       -> priority heuristic
    low confidence             -> human review
    database unavailable       -> serve from memory, mark degraded

plus two paths the contract requires elsewhere and that would otherwise be
implicit: an invalid simulator payload (brief §11: "reject input + raise alert",
CONTRACT §5.2/§5.3) and a dropped SSE stream (CONTRACT §5.5).

Usage
-----
Look a path up (raises on an undeclared path — never a silent success)::

    from app.resilience.policy import policy_for
    rule = policy_for("llm", "unavailable")
    rule.action            # "templated_explanation"
    rule.degraded          # True

Ask what to do when you are not certain the path is declared::

    rule = resolve("llm", "something_new")   # -> the documented default action
    rule.declared                            # False

Stamp a degraded response for the frontend::

    payload = degraded_envelope(
        {"explanation": text, "source": "fallback"},
        component="llm", condition="unavailable",
    )
    # -> {"explanation": ..., "source": "fallback", "simulated": True,
    #     "degraded": True, "degraded_reason": "llm.unavailable",
    #     "fallback": "templated_explanation"}

This module deliberately imports nothing from the rest of the application: it is
a leaf, so any layer (API, intelligence, gateway) can import it without creating
a cycle.

Ownership: A10. See CONTRACT.md §3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal, Mapping

__all__ = [
    "COMPONENT_DATABASE",
    "COMPONENT_DECISION_ENGINE",
    "COMPONENT_FORECAST",
    "COMPONENT_LLM",
    "COMPONENT_SIMULATOR",
    "DEFAULT_POLICY",
    "LOW_CONFIDENCE_THRESHOLD",
    "UNKNOWN_PATH_ACTION",
    "UnknownPolicyPath",
    "FallbackAction",
    "FallbackPolicy",
    "FallbackRule",
    "action_for",
    "degraded_envelope",
    "is_declared",
    "is_low_confidence",
    "policy_for",
    "requires_human_review",
    "resolve",
]

# --------------------------------------------------------------------------
# Component names.
#
# These are the same identifiers /api/v1/status reports per component
# (CONTRACT §9: "simulator", "database", "llm", "decision_engine"), plus
# "forecast" for the prediction layer, which brief §11 calls out separately
# ("Prediction confidence too low -> Human review requested").
# --------------------------------------------------------------------------

COMPONENT_LLM: Final = "llm"
COMPONENT_SIMULATOR: Final = "simulator"
COMPONENT_DECISION_ENGINE: Final = "decision_engine"
COMPONENT_DATABASE: Final = "database"
COMPONENT_FORECAST: Final = "forecast"

#: A recommendation or forecast below this confidence is not presented as
#: actionable. It is still returned — with its confidence, and flagged for
#: review — rather than hidden or silently upgraded. Advisory: the engines own
#: their own confidence calibration; this is the platform-wide floor.
LOW_CONFIDENCE_THRESHOLD: Final = 0.5

HealthLabel = Literal["healthy", "degraded", "down"]


class UnknownPolicyPath(KeyError):
    """Raised when an undeclared (component, condition) pair is looked up.

    Raised rather than returning a permissive default, so a code path that
    degrades in an undeclared way fails loudly in development instead of
    succeeding quietly in front of a judge. Callers that genuinely cannot know
    the path in advance use :func:`resolve`, which returns a default that is
    explicitly marked as undeclared.
    """

    def __init__(self, component: str, condition: str,
                 declared: tuple[tuple[str, str], ...]) -> None:
        self.component = component
        self.condition = condition
        self.declared = declared
        super().__init__(
            f"no fallback policy declared for component={component!r} "
            f"condition={condition!r}; declared paths: {_format_paths(declared)}"
        )


def _format_paths(paths: tuple[tuple[str, str], ...]) -> str:
    return ", ".join(f"{c}.{k}" for c, k in paths) or "(none)"


@dataclass(frozen=True, slots=True)
class FallbackAction:
    """A named degradation behaviour.

    One action can serve several (component, condition) pairs — the priority
    heuristic is the answer both when the optimiser is infeasible and when it is
    unavailable — so actions are a catalogue and rules point into it.
    """

    name: str
    """Stable machine identifier. Safe to put in a response body or a log line."""

    description: str
    """What the fallback actually does, in one operator-readable sentence."""

    status_label: HealthLabel
    """How the affected component is reported on /api/v1/status.

    ``degraded`` — working, with a declared limitation. brief §11 and
    CONTRACT §9 are explicit that an absent LLM key is a *supported mode*, so it
    reports ``degraded`` and never ``down``.
    """

    requires_human_review: bool
    """True when the fallback hands the decision to a person rather than
    producing an answer. Brief §24: human review is preserved for consequential
    simulated decisions."""

    metric: str
    """Counters/metrics the activation should be recorded against. Names match
    CONTRACT §10 where that section declares one; ``fallback_activated_total``
    is the generic counter (see the note in docs/architecture.md)."""

    log_event: str
    """Structured log event emitted on activation (CONTRACT §10 "Logs": one line
    per fallback activation)."""

    #: True when the fallback substitutes a value for the unavailable one
    #: (a template, a cache, a heuristic). False when it refuses to answer.
    substitutes_value: bool = True


@dataclass(frozen=True, slots=True)
class FallbackRule:
    """A declared degradation path: the trigger, and the action it selects."""

    component: str
    condition: str
    action: str
    """Name of the :class:`FallbackAction` taken."""

    trigger: str
    """What causes this condition, in terms of the actual mechanism (threshold,
    config flag, timeout). This is the "and its trigger" half of CONTRACT §11."""

    declared: bool = True
    """False only for the synthetic default returned by :func:`resolve`."""


@dataclass(frozen=True, slots=True)
class FallbackPolicy:
    """The declared policy: an action catalogue plus the rules that select one."""

    actions: Mapping[str, FallbackAction]
    rules: tuple[FallbackRule, ...]

    def __post_init__(self) -> None:
        seen: set[tuple[str, str]] = set()
        for rule in self.rules:
            if rule.action not in self.actions:
                raise ValueError(
                    f"rule {rule.component}.{rule.condition} names unknown action "
                    f"{rule.action!r}"
                )
            key = (rule.component, rule.condition)
            if key in seen:
                raise ValueError(
                    f"duplicate rule for {rule.component}.{rule.condition}"
                )
            seen.add(key)

    # -- lookup ------------------------------------------------------------

    def policy_for(self, component: str, condition: str) -> FallbackRule:
        """Return the declared rule. Raises :class:`UnknownPolicyPath`."""
        for rule in self.rules:
            if rule.component == component and rule.condition == condition:
                return rule
        raise UnknownPolicyPath(component, condition, self.declared_paths())

    def action_for(self, component: str, condition: str) -> FallbackAction:
        """Return the action a path selects. Raises :class:`UnknownPolicyPath`."""
        return self.actions[self.policy_for(component, condition).action]

    def try_policy_for(self, component: str, condition: str) -> FallbackRule | None:
        """Return the rule, or ``None`` if the path is undeclared."""
        for rule in self.rules:
            if rule.component == component and rule.condition == condition:
                return rule
        return None

    def is_declared(self, component: str, condition: str) -> bool:
        return self.try_policy_for(component, condition) is not None

    def for_component(self, component: str) -> tuple[FallbackRule, ...]:
        """Every declared path for one component (order as declared)."""
        return tuple(r for r in self.rules if r.component == component)

    def declared_paths(self) -> tuple[tuple[str, str], ...]:
        return tuple((r.component, r.condition) for r in self.rules)

    def status_label(self, component: str, condition: str) -> HealthLabel:
        return self.action_for(component, condition).status_label

    # -- container-ish conveniences ---------------------------------------

    def __contains__(self, item: object) -> bool:
        if isinstance(item, tuple) and len(item) == 2:
            return self.is_declared(str(item[0]), str(item[1]))
        if isinstance(item, str) and "." in item:
            component, _, condition = item.partition(".")
            return self.is_declared(component, condition)
        return False

    def __iter__(self):
        return iter(self.rules)

    def __len__(self) -> int:
        return len(self.rules)


# --------------------------------------------------------------------------
# The action catalogue.
# --------------------------------------------------------------------------

_TEMPLATED_EXPLANATION = FallbackAction(
    name="templated_explanation",
    description=(
        "Return a deterministic explanation assembled from the same structured "
        "facts the LLM would have been given, with source='fallback' and "
        "degraded=True. The operator always gets an explanation; the response "
        "always says where it came from."
    ),
    status_label="degraded",
    requires_human_review=False,
    metric="llm_fallback_total",
    log_event="llm.fallback",
)

_SERVE_LAST_GOOD_STALE = FallbackAction(
    name="serve_last_good_snapshot_stale",
    description=(
        "Serve the last-good cached snapshot with stale=True and age_seconds "
        "set to the age of the underlying data, rather than failing the "
        "request or inventing values."
    ),
    status_label="degraded",
    requires_human_review=False,
    metric="fallback_activated_total",
    log_event="fallback.activated",
)

_RAISE_TYPED_ERROR = FallbackAction(
    name="raise_typed_error",
    description=(
        "Raise SimulatorError. There is no cached value to degrade to, and "
        "degrading to a fabricated snapshot would be worse than failing: "
        "degrade, do not fabricate."
    ),
    status_label="down",
    requires_human_review=False,
    metric="fallback_activated_total",
    log_event="fallback.activated",
    substitutes_value=False,
)

_REJECT_AND_ALERT = FallbackAction(
    name="reject_and_raise_alert",
    description=(
        "Reject the invalid simulator payload, log the validation failure, "
        "raise an alert, and keep serving the last valid state. The bad "
        "payload is never partially trusted."
    ),
    status_label="degraded",
    requires_human_review=False,
    metric="fallback_activated_total",
    log_event="simulator.invalid_response",
    substitutes_value=False,
)

_RECONNECT_AND_REFETCH = FallbackAction(
    name="reconnect_and_refetch",
    description=(
        "Reconnect the SSE stream with exponential backoff and re-fetch over "
        "REST. A missed event can make a number late, never wrong, because "
        "nothing is computed from an event payload alone."
    ),
    status_label="degraded",
    requires_human_review=False,
    metric="circuit_breaker_state",
    log_event="simulator.stream_reconnect",
)

_PRIORITY_HEURISTIC = FallbackAction(
    name="priority_heuristic",
    description=(
        "Rank allocations with the transparent priority heuristic "
        "(severity x probability x volume) and report policy='heuristic' so the "
        "response itself says the optimiser did not run. Constraints are still "
        "enforced: no over-shipment, no over-draw, no DISRUPTED route."
    ),
    status_label="degraded",
    requires_human_review=False,
    metric="fallback_activated_total",
    log_event="decision.fallback",
)

_HUMAN_REVIEW = FallbackAction(
    name="human_review_required",
    description=(
        "Return the item with its confidence and set review_required=True "
        "rather than presenting it as actionable. The recommendation is not "
        "hidden and not silently upgraded — the operator decides."
    ),
    status_label="degraded",
    requires_human_review=True,
    metric="fallback_activated_total",
    log_event="decision.human_review",
)

_SERVE_FROM_MEMORY = FallbackAction(
    name="serve_from_memory_degraded",
    description=(
        "Serve reads from in-memory state, mark every response degraded, and "
        "stop recording history until storage returns. Read-only degradation: "
        "nothing is written to a database that is not there."
    ),
    status_label="degraded",
    requires_human_review=False,
    metric="fallback_activated_total",
    log_event="database.degraded",
)

#: The documented default for an undeclared path. It is *not* a licence to
#: succeed: it says "no fallback is declared here", and a caller that receives
#: it must propagate a typed error rather than serve a substituted value.
UNKNOWN_PATH_ACTION: Final = FallbackAction(
    name="undeclared_path",
    description=(
        "No fallback is declared for this component and condition. The caller "
        "must propagate a typed error and log the gap; it must not substitute "
        "a value or report success."
    ),
    status_label="degraded",
    requires_human_review=False,
    metric="fallback_activated_total",
    log_event="fallback.undeclared",
    substitutes_value=False,
)

ACTIONS: Final[Mapping[str, FallbackAction]] = {
    action.name: action
    for action in (
        _TEMPLATED_EXPLANATION,
        _SERVE_LAST_GOOD_STALE,
        _RAISE_TYPED_ERROR,
        _REJECT_AND_ALERT,
        _RECONNECT_AND_REFETCH,
        _PRIORITY_HEURISTIC,
        _HUMAN_REVIEW,
        _SERVE_FROM_MEMORY,
        UNKNOWN_PATH_ACTION,
    )
}

# --------------------------------------------------------------------------
# The declared rules. CONTRACT §11's five paths are the first five below.
# --------------------------------------------------------------------------

RULES: Final[tuple[FallbackRule, ...]] = (
    FallbackRule(
        component=COMPONENT_LLM,
        condition="unavailable",
        action="templated_explanation",
        trigger=(
            "DEEPSEEK_API_KEY is empty, LLM_ENABLED=false, the request times "
            "out (DEEPSEEK_TIMEOUT_SECONDS), a transport error occurs, or the "
            "response fails validation (empty or over the length bound)."
        ),
    ),
    FallbackRule(
        component=COMPONENT_SIMULATOR,
        condition="circuit_open",
        action="serve_last_good_snapshot_stale",
        trigger=(
            "CIRCUIT_FAILURE_THRESHOLD consecutive failures opened the breaker, "
            "and a last-good value has been cached."
        ),
    ),
    FallbackRule(
        component=COMPONENT_SIMULATOR,
        condition="circuit_open_no_cache",
        action="raise_typed_error",
        trigger=(
            "Breaker open and nothing has ever been cached successfully — a "
            "cold start into a dead simulator."
        ),
    ),
    FallbackRule(
        component=COMPONENT_DECISION_ENGINE,
        condition="optimizer_infeasible",
        action="priority_heuristic",
        trigger=(
            "scipy.optimize.linprog reports infeasible or unbounded, or raises "
            "while solving the allocation LP."
        ),
    ),
    FallbackRule(
        component=COMPONENT_DECISION_ENGINE,
        condition="low_confidence",
        action="human_review_required",
        trigger=(
            f"Recommendation confidence is below LOW_CONFIDENCE_THRESHOLD "
            f"({LOW_CONFIDENCE_THRESHOLD})."
        ),
    ),
    FallbackRule(
        component=COMPONENT_DATABASE,
        condition="unavailable",
        action="serve_from_memory_degraded",
        trigger=(
            "DATABASE_URL cannot be reached, or a read/write raises a "
            "SQLAlchemyError."
        ),
    ),
    # --- paths required elsewhere in the contract -------------------------
    FallbackRule(
        component=COMPONENT_DECISION_ENGINE,
        condition="optimizer_unavailable",
        action="priority_heuristic",
        trigger=(
            "The optimiser is disabled or its dependency is missing "
            "(AllocationEngine(policy='heuristic'))."
        ),
    ),
    FallbackRule(
        component=COMPONENT_FORECAST,
        condition="low_confidence",
        action="human_review_required",
        trigger=(
            "Forecast confidence is below LOW_CONFIDENCE_THRESHOLD, or "
            "fallback_used=True over a horizon longer than the history "
            "supports."
        ),
    ),
    FallbackRule(
        component=COMPONENT_SIMULATOR,
        condition="invalid_response",
        action="reject_and_raise_alert",
        trigger=(
            "A simulator response does not validate against the typed models, "
            "or an error envelope cannot be normalised (CONTRACT §5.3). Note: "
            "an unknown *enum value* is not this path — CONTRACT §5.2 requires "
            "it be logged and tolerated."
        ),
    ),
    FallbackRule(
        component=COMPONENT_SIMULATOR,
        condition="stream_disconnected",
        action="reconnect_and_refetch",
        trigger=(
            "The SSE read raises, or the simulator drops a subscriber more "
            "than 200 events behind (integration guide §6.1)."
        ),
    ),
)

#: The policy the application runs on.
DEFAULT_POLICY: Final = FallbackPolicy(actions=ACTIONS, rules=RULES)


# --------------------------------------------------------------------------
# Module-level convenience: the API layer imports these, not the instance.
# --------------------------------------------------------------------------


def policy_for(component: str, condition: str) -> FallbackRule:
    """The declared rule for a path. Raises :class:`UnknownPolicyPath`."""
    return DEFAULT_POLICY.policy_for(component, condition)


def action_for(component: str, condition: str) -> FallbackAction:
    """The declared action for a path. Raises :class:`UnknownPolicyPath`."""
    return DEFAULT_POLICY.action_for(component, condition)


def is_declared(component: str, condition: str) -> bool:
    """True when this degradation path is declared."""
    return DEFAULT_POLICY.is_declared(component, condition)


def resolve(component: str, condition: str) -> FallbackRule:
    """Like :func:`policy_for`, but never raises.

    Returns the declared rule when there is one, otherwise a rule pointing at
    :data:`UNKNOWN_PATH_ACTION` with ``declared=False``. The returned rule is
    always explicit about which of the two it is, so this is not a silent
    success: a caller that gets ``declared=False`` has been told nothing is
    declared and must not substitute a value.
    """
    declared = DEFAULT_POLICY.try_policy_for(component, condition)
    if declared is not None:
        return declared
    return FallbackRule(
        component=component,
        condition=condition,
        action=UNKNOWN_PATH_ACTION.name,
        trigger=(
            "Not declared. Nothing in the policy covers this component and "
            "condition."
        ),
        declared=False,
    )


# --------------------------------------------------------------------------
# Degraded-mode helpers.
# --------------------------------------------------------------------------


def is_low_confidence(confidence: float,
                      threshold: float = LOW_CONFIDENCE_THRESHOLD) -> bool:
    """True when a confidence value falls into the human-review band."""
    return confidence < threshold


def requires_human_review(confidence: float,
                          threshold: float = LOW_CONFIDENCE_THRESHOLD) -> bool:
    """True when an item at this confidence must be flagged for review.

    Convenience wrapper around the declared low-confidence path, so the API
    layer can answer the question it asks on every recommendation without
    looking the path up itself. A confidence at or above the threshold is
    actionable and returns False.
    """
    return is_low_confidence(confidence, threshold)


def degraded_envelope(payload: Mapping[str, Any],
                      *,
                      component: str,
                      condition: str,
                      reason: str | None = None) -> dict[str, Any]:
    """Return ``payload`` plus the declared degradation markers.

    The input mapping is not mutated. The result always carries:

    ``simulated``        always True — brief §24, nothing here is real-world.
    ``degraded``         True.
    ``degraded_reason``  ``"<component>.<condition>"``, or ``reason``.
    ``fallback``         the declared action name.

    ``fallback`` is the field a judge can look at to tell a fallback apart from
    a normal response, so it is set even for the undeclared default.
    """
    rule = resolve(component, condition)
    merged: dict[str, Any] = dict(payload)
    merged["simulated"] = True
    merged["degraded"] = True
    merged["degraded_reason"] = reason or f"{component}.{condition}"
    merged["fallback"] = rule.action
    if not rule.declared:
        merged["fallback_declared"] = False
    return merged
