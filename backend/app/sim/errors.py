"""Normalisation of the simulator's three error envelopes into one exception.

CONTRACT.md 5.3. The simulator answers a failed request in one of three ways::

    {"detail": {"code": "...", "message": "..."}}   # domain error
    {"error":  {"code": "...", "message": "..."}}   # injected fault
    {"detail": [{"loc": [...], "msg": "..."}]}      # FastAPI 422

...and, as the live instance also showed, ``{"detail": "Method Not Allowed"}``
for a 405. All of them become a single :class:`SimulatorError`.

The trap
--------
The natural way to write the discriminator is ``isinstance(body, dict)``, and
that is correct *in Python* -- but only if the ``dict`` test is made **before**
any ``list`` test. The same check written in TypeScript
(``typeof x === 'object'``) accepts arrays, so the 422 envelope silently takes
the object branch and reports ``code=None`` with a nonsensical message. In
Python the failure mode is different but just as real: a discriminator that
tests ``list`` first will classify every ``{"detail": {...}}`` domain error as a
validation error, because a dict is not a list but the *branch order* is what
decides. Both orders are pinned by tests in
``tests/test_sim_errors.py::test_array_envelope_does_not_take_the_dict_branch``.

The same discipline applies one level down: inside a body that has ``detail``,
we test ``isinstance(detail, dict)`` before ``isinstance(detail, list)``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

__all__ = [
    "SimulatorError",
    "EnvelopeKind",
    "DOMAIN",
    "FAULT",
    "VALIDATION",
    "TEXT",
    "UNKNOWN",
    "parse_envelope",
    "normalise_error",
    "is_retryable_status",
    "body_from_response",
]

logger = logging.getLogger(__name__)

#: Which of the simulator's three envelopes (or which fallback) a body matched.
EnvelopeKind = Literal["domain", "fault", "validation", "text", "unknown"]

DOMAIN: EnvelopeKind = "domain"
FAULT: EnvelopeKind = "fault"
VALIDATION: EnvelopeKind = "validation"
TEXT: EnvelopeKind = "text"
UNKNOWN: EnvelopeKind = "unknown"

#: Cap on how much of an unrecognised body we keep in the error. Bodies are
#: external input: a hostile or broken simulator must not be able to inflate
#: our log lines or our error objects.
_MAX_MESSAGE = 500


def is_retryable_status(status_code: int | None) -> bool:
    """Retry on 5xx only. Never on 4xx -- CONTRACT.md 5.5.

    A 4xx means *we* sent something the simulator rejected; repeating the
    request would produce the same rejection and waste the timeout budget.
    """
    return status_code is not None and status_code >= 500


def _render_validation(detail: list[Any]) -> str:
    """Turn FastAPI's ``[{"loc": [...], "msg": "..."}]`` into one readable line."""
    parts: list[str] = []
    for item in detail:
        if isinstance(item, dict):
            loc = item.get("loc")
            where = ".".join(str(p) for p in loc) if isinstance(loc, (list, tuple)) else ""
            msg = item.get("msg") or item.get("message") or ""
            parts.append(f"{where}: {msg}" if where else str(msg))
        else:
            parts.append(str(item))
    joined = "; ".join(p for p in parts if p)
    return joined[:_MAX_MESSAGE] if joined else "request failed validation"


def _render_unknown(body: Any) -> str:
    try:
        rendered = json.dumps(body, default=str)
    except (TypeError, ValueError):
        rendered = repr(body)
    return f"unrecognised error body: {rendered[:_MAX_MESSAGE]}"


def parse_envelope(body: Any) -> tuple[EnvelopeKind, str | None, str]:
    """Classify an error body.

    Returns ``(kind, code, message)``. Never raises: an unrecognised body yields
    ``(UNKNOWN, None, <rendered body>)`` rather than blowing up inside an error
    path, which is the one place a second exception does the most damage.
    """
    # --- object first: a JSON array is never a dict ----------------------- #
    if isinstance(body, dict):
        detail = body.get("detail")

        # `detail` as an object is the *domain* envelope. This test must precede
        # the list test below; see the module docstring.
        if isinstance(detail, dict):
            code = detail.get("code")
            message = detail.get("message") or detail.get("msg")
            if message is None and len(detail) > 1:
                # NOT_FOUND came back as {"detail": {"code": "NOT_FOUND"}} with
                # no message at all -- fall back to the whole object.
                message = json.dumps(detail, default=str)
            return DOMAIN, _as_code(code), str(message or code or "simulator error")

        if isinstance(detail, list):
            return VALIDATION, "VALIDATION_ERROR", _render_validation(detail)

        if isinstance(detail, str):
            # Observed live: 405 -> {"detail": "Method Not Allowed"}.
            return TEXT, None, detail

        # `detail` absent or some other type -- try the fault envelope.
        error = body.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message") or error.get("msg")
            return FAULT, _as_code(code), str(message or code or "injected fault")

        if isinstance(error, str):
            return FAULT, None, error

        return UNKNOWN, None, _render_unknown(body)

    # --- bare array: the 422 shape when it is not nested under `detail` --- #
    if isinstance(body, list):
        return VALIDATION, "VALIDATION_ERROR", _render_validation(body)

    if isinstance(body, str):
        return TEXT, None, body[:_MAX_MESSAGE] if body else "simulator request failed"

    return UNKNOWN, None, _render_unknown(body)


def _as_code(code: Any) -> str | None:
    if code is None:
        return None
    return code if isinstance(code, str) else str(code)


class SimulatorError(RuntimeError):
    """Every failed simulator interaction, in one type.

    ``kind`` says which envelope (or which local failure) produced it, so a
    caller can branch without string-matching a message:

    ``domain`` / ``fault`` / ``validation`` / ``text`` / ``unknown``
        One of the simulator's own error envelopes.
    ``transport``
        Connection refused, DNS failure, read or connect timeout. Retryable.
    ``decode``
        A 2xx whose body was not JSON. Retryable.
    ``invalid_payload``
        A 2xx whose body did not match the model. Counts as a simulator fault.
    ``circuit_open``
        The breaker is open and there is no last-good value to serve. The
        platform degrades; it does not fabricate a number.
    ``invalid_request``
        The caller passed something the simulator would reject. Raised locally,
        no request is made.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        kind: EnvelopeKind | str = UNKNOWN,
        url: str | None = None,
        method: str | None = None,
        payload: Any = None,
        retryable: bool | None = None,
    ) -> None:
        self.message = message
        self.status_code = status_code
        self.code = code
        self.kind = kind
        self.url = url
        self.method = method
        self.payload = payload
        # A transport failure has no status code but is always retryable.
        if retryable is None:
            retryable = True if status_code is None else is_retryable_status(status_code)
        self.retryable = retryable
        super().__init__(self.render())

    # -- presentation ---------------------------------------------------- #

    def render(self) -> str:
        bits: list[str] = []
        if self.method or self.url:
            bits.append(f"{self.method or 'GET'} {self.url or ''}".strip())
        if self.status_code is not None:
            bits.append(f"HTTP {self.status_code}")
        bits.append(f"[{self.kind}{':' + self.code if self.code else ''}]")
        bits.append(self.message)
        return " ".join(bits)

    def __str__(self) -> str:  # pragma: no cover - delegates to render()
        return self.render()

    def __repr__(self) -> str:
        return (
            f"<SimulatorError kind={self.kind!r} status={self.status_code!r} "
            f"code={self.code!r} retryable={self.retryable!r}>"
        )

    # -- construction ---------------------------------------------------- #

    @classmethod
    def from_body(
        cls,
        status_code: int,
        body: Any,
        *,
        method: str | None = None,
        url: str | None = None,
    ) -> "SimulatorError":
        """Normalise one of the three envelopes plus its status code."""
        kind, code, message = parse_envelope(body)
        return cls(
            message,
            status_code=status_code,
            code=code,
            kind=kind,
            url=url,
            method=method,
            payload=body,
        )

    @classmethod
    def transport(
        cls, message: str, *, method: str | None = None, url: str | None = None
    ) -> "SimulatorError":
        return cls(
            message, kind="transport", url=url, method=method, retryable=True
        )

    @classmethod
    def circuit_open(
        cls, message: str, *, url: str | None = None
    ) -> "SimulatorError":
        return cls(message, kind="circuit_open", url=url, retryable=True)


def body_from_response(response: Any) -> Any:
    """Best-effort body extraction from an ``httpx.Response``.

    Only ``ValueError`` (which ``json.JSONDecodeError`` subclasses) is caught; a
    body that cannot be parsed falls back to raw text so the message an operator
    sees is the simulator's own, not ours.
    """
    try:
        return response.json()
    except ValueError:
        return getattr(response, "text", "")
