"""Envelope normalisation tests (CONTRACT.md 5.3).

The centrepiece is the branch-order trap: the three envelopes have to be told
apart by inspecting the *body*, and the way that inspection is ordered is what
decides whether a 422 is read correctly.
"""

from __future__ import annotations

import httpx
import pytest

from app.sim.errors import (
    DOMAIN,
    FAULT,
    TEXT,
    UNKNOWN,
    VALIDATION,
    SimulatorError,
    body_from_response,
    is_retryable_status,
    parse_envelope,
)

# The three bodies below are the simulator's real responses, copied from the
# live instance at :8001 (or, for the fault envelope, from the integration
# guide's specification of an injected fault).

DOMAIN_BODY = {"detail": {"code": "INSUFFICIENT_INVENTORY", "message": "depot has too little diesel"}}
FAULT_BODY = {"error": {"code": "FAULT_INJECTED", "message": "error_rate fault active"}}
VALIDATION_BODY = {
    "detail": [
        {
            "type": "int_parsing",
            "loc": ["query", "limit"],
            "msg": "Input should be a valid integer, unable to parse string as an integer",
            "input": "notanint",
        }
    ]
}


# --------------------------------------------------------------------------- #
# The three envelopes
# --------------------------------------------------------------------------- #


def test_domain_envelope_detail_object():
    kind, code, message = parse_envelope(DOMAIN_BODY)
    assert kind == DOMAIN
    assert code == "INSUFFICIENT_INVENTORY"
    assert message == "depot has too little diesel"


def test_fault_envelope_error_object():
    kind, code, message = parse_envelope(FAULT_BODY)
    assert kind == FAULT
    assert code == "FAULT_INJECTED"
    assert message == "error_rate fault active"


def test_validation_envelope_detail_list():
    kind, code, message = parse_envelope(VALIDATION_BODY)
    assert kind == VALIDATION
    assert code == "VALIDATION_ERROR"
    # The readable form names the offending parameter, not just "validation error".
    assert "query.limit" in message
    assert "valid integer" in message


def test_all_three_envelopes_become_one_error_type():
    for status, body in ((409, DOMAIN_BODY), (503, FAULT_BODY), (422, VALIDATION_BODY)):
        err = SimulatorError.from_body(status, body, method="GET", url="/v1/x")
        assert isinstance(err, SimulatorError)
        assert err.status_code == status
        assert err.message


# --------------------------------------------------------------------------- #
# The trap
# --------------------------------------------------------------------------- #


def test_array_envelope_does_not_take_the_dict_branch():
    """The 422 array must be classified as validation, never as an object.

    This is the Python mirror of the TypeScript bug the contract warns about.
    In TS, ``typeof [] === 'object'`` is true, so a discriminator written as
    ``typeof body === 'object' ? detail : error`` sends the FastAPI 422 down the
    domain branch and reports ``code=None``. In Python a list is genuinely not a
    dict, so the guard holds -- but only because the dict test is made first and
    ``detail`` is tested for ``dict`` before ``list``. Both halves of that
    ordering are asserted here.
    """
    body = VALIDATION_BODY["detail"]

    # The premise of the trap, asserted directly rather than assumed.
    assert isinstance(body, list)
    assert not isinstance(body, dict)

    kind, code, _ = parse_envelope(body)
    assert kind == VALIDATION, "the array took the wrong branch"
    assert kind != DOMAIN
    assert kind != FAULT
    assert code == "VALIDATION_ERROR"


def test_nested_array_detail_does_not_take_the_domain_branch():
    """The same trap one level down: ``detail`` as a list is not a domain error."""
    kind, code, message = parse_envelope(VALIDATION_BODY)
    assert kind == VALIDATION
    assert code == "VALIDATION_ERROR"
    # A list-first ordering inside the dict branch would have produced an
    # AttributeError or a nonsense message here.
    assert "Input should be a valid integer" in message


def test_dict_branch_wins_for_a_dict_body_carrying_a_list_value():
    """A dict is a dict; the list inside it must not reclassify the body."""
    kind, _, _ = parse_envelope({"detail": [{"msg": "x"}], "other": ["y"]})
    assert kind == VALIDATION


def test_array_body_is_not_reported_as_unknown():
    """Bare array (the 422 shape without the ``detail`` wrapper) still validates."""
    kind, code, _ = parse_envelope([{"loc": ["body", "quantity"], "msg": "must be positive"}])
    assert kind == VALIDATION
    assert kind != UNKNOWN
    assert code == "VALIDATION_ERROR"


def test_object_with_neither_detail_nor_error_is_unknown_not_a_crash():
    kind, code, message = parse_envelope({"unexpected": True})
    assert kind == UNKNOWN
    assert code is None
    assert "unrecognised error body" in message


@pytest.mark.parametrize("body", [None, 42, 3.5, True, b"bytes"])
def test_non_string_scalars_never_raise(body):
    """An error path must not raise a second exception of its own."""
    kind, _, message = parse_envelope(body)
    assert kind == UNKNOWN
    assert message


# --------------------------------------------------------------------------- #
# Additional shapes the live simulator actually returns
# --------------------------------------------------------------------------- #


def test_text_envelope_405_method_not_allowed():
    """Verified live: ``POST /v1/health`` -> 405 ``{"detail": "Method Not Allowed"}``."""
    kind, code, message = parse_envelope({"detail": "Method Not Allowed"})
    assert kind == TEXT
    assert code is None
    assert message == "Method Not Allowed"


def test_domain_error_without_a_message_does_not_render_none():
    """Verified live: ``GET /v1/depots/nope`` -> ``{"detail": {"code": "NOT_FOUND"}}``.

    Only ``code`` is present. The message must never be the string "None".
    """
    kind, code, message = parse_envelope({"detail": {"code": "NOT_FOUND"}})
    assert kind == DOMAIN
    assert code == "NOT_FOUND"
    assert message and message != "None"


def test_unrecognised_body_is_truncated():
    kind, _, message = parse_envelope({"junk": "x" * 5000})
    assert kind == UNKNOWN
    assert len(message) < 1000


# --------------------------------------------------------------------------- #
# Retry classification (CONTRACT.md 5.5: 5xx and transport only, never 4xx)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [500, 501, 502, 503, 504, 599])
def test_5xx_is_retryable(status):
    assert is_retryable_status(status) is True
    assert SimulatorError.from_body(status, FAULT_BODY).retryable is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 409, 422, 429])
def test_4xx_is_never_retryable(status):
    assert is_retryable_status(status) is False
    assert SimulatorError.from_body(status, DOMAIN_BODY).retryable is False


def test_transport_error_is_retryable_and_has_no_status():
    err = SimulatorError.transport("ConnectError: refused", method="GET", url="/v1/health")
    assert err.retryable is True
    assert err.status_code is None
    assert err.kind == "transport"


def test_circuit_open_error_is_typed():
    err = SimulatorError.circuit_open("no cached value", url="/v1/depots")
    assert err.kind == "circuit_open"
    assert "no cached value" in str(err)


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #


def test_error_str_carries_the_diagnostic_facts():
    err = SimulatorError.from_body(503, FAULT_BODY, method="GET", url="/v1/metrics")
    rendered = str(err)
    assert "GET" in rendered
    assert "/v1/metrics" in rendered
    assert "503" in rendered
    assert "fault" in rendered
    assert "FAULT_INJECTED" in rendered


def test_body_from_response_falls_back_to_text_for_non_json():
    response = httpx.Response(502, text="<html>bad gateway</html>")
    assert body_from_response(response) == "<html>bad gateway</html>"
    # ...and that fallback still normalises without raising.
    err = SimulatorError.from_body(502, body_from_response(response))
    assert err.kind == TEXT
    assert "bad gateway" in err.message


def test_body_from_response_parses_json():
    response = httpx.Response(422, json=VALIDATION_BODY)
    assert body_from_response(response) == VALIDATION_BODY
