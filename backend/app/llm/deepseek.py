"""DeepSeek chat client -- plain ``httpx`` against the OpenAI-compatible API.

CONTRACT.md section 8.1. Three things matter here and the rest is plumbing:

**The API key never leaves the Authorization header.** It is read once from
settings, used for one header, and never placed in a log line, a metric label, a
prompt, a request body or an exception message. Every failure message this
module builds is passed through :func:`redact`, and every failure is turned into
a message *after* leaving the ``except`` block, so even the implicit
``__context__`` chain carries no request headers or bodies. A test plants the
key in both the error body and the base URL and asserts it appears nowhere.

**Timeouts and failures never reach the operator.** One retry, then
:class:`LLMUnavailable`. The service above turns that into a deterministic
fallback explanation rather than a 500.

**The response is validated.** Empty, too short or too long is a failure, not an
explanation -- CONTRACT 8.2.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from app.llm.prompts import (
    MAX_RESPONSE_CHARS,
    MIN_RESPONSE_CHARS,
    REDACTED,
    scrub_secrets,
)

logger = structlog.get_logger(__name__)

#: How many times one ``complete()`` call talks to the network. One retry.
MAX_ATTEMPTS = 2

#: Characters of an error body kept for diagnostics, after redaction.
MAX_ERROR_DETAIL = 200


class LLMUnavailable(RuntimeError):
    """The LLM could not produce a usable completion.

    Raised for a missing key, a disabled feature flag, a timeout, a transport
    error, a rejected request, and for a response that failed validation. The
    caller (:class:`~app.llm.explain.ExplanationService`) always catches it and
    degrades to the deterministic template; it must never surface as a 500.

    ``reason`` is one of the bounded keys in
    :data:`app.llm.prompts.FALLBACK_REASON_TEXT`. It is carried as an attribute
    rather than parsed back out of the message, so it can be used directly as a
    metric label (CONTRACT 10: ``llm_fallback_total{reason}``) without unbounded
    cardinality and without the message ever being inspected.
    """

    def __init__(self, message: str, *, reason: str = "unknown") -> None:
        super().__init__(message)
        self.reason = reason


class InvalidLLMResponse(LLMUnavailable):
    """The model answered, but the answer failed validation."""

    def __init__(self, message: str, *, reason: str = "invalid_content") -> None:
        super().__init__(message, reason=reason)


# --------------------------------------------------------------------------
# Credential safety
# --------------------------------------------------------------------------


def redact(text: str | None, *secrets: str | None) -> str:
    """Strip known secrets and credential-shaped substrings from ``text``.

    Explicit ``secrets`` are removed first, so the configured key is always
    caught even if it does not match a vendor prefix pattern. Then
    :func:`app.llm.prompts.scrub_secrets` removes Bearer headers, vendor-shaped
    tokens and credential-bearing connection strings, so a secret we were not
    told about -- echoed by a proxy, or lying in an error body -- is still
    caught.

    Sharing that one helper with the prompt sanitiser matters: the rules for
    "what must never reach a log" and "what must never reach a prompt" cannot
    drift apart if there is only one of them.
    """

    out = "" if text is None else str(text)
    for secret in secrets:
        if secret:
            out = out.replace(secret, REDACTED)
    return scrub_secrets(out)


def validate_completion(
    text: Any,
    *,
    min_chars: int = MIN_RESPONSE_CHARS,
    max_chars: int = MAX_RESPONSE_CHARS,
) -> str:
    """Return the trimmed completion, or raise :class:`InvalidLLMResponse`.

    CONTRACT 8.2: a response failing validation is treated as an LLM failure and
    falls back. Bounds are parameters so a caller can tighten them, and so the
    behaviour is testable at both ends without a giant fixture.
    """

    if text is None:
        raise InvalidLLMResponse("model returned no content")
    if not isinstance(text, str):
        raise InvalidLLMResponse(f"model returned {type(text).__name__}, expected str")
    stripped = text.strip()
    if not stripped:
        raise InvalidLLMResponse("model returned an empty response")
    if len(stripped) < min_chars:
        raise InvalidLLMResponse(
            f"model response too short: {len(stripped)} chars, minimum {min_chars}"
        )
    if len(stripped) > max_chars:
        raise InvalidLLMResponse(
            f"model response too long: {len(stripped)} chars, maximum {max_chars}"
        )
    return stripped


def _extract_content(body: Any) -> str | None:
    """Pull the assistant message out of an OpenAI-compatible response body."""

    if not isinstance(body, dict):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
    # Some OpenAI-compatible deployments use the legacy `text` field.
    legacy = first.get("text")
    if isinstance(legacy, str):
        return legacy
    return None


@dataclass(frozen=True)
class _Attempt:
    """The result of one HTTP round trip. Never an exception."""

    text: str | None = None
    reason: str | None = None
    status: int | None = None
    error_type: str | None = None
    detail: str | None = None
    #: False for a permanent failure where a second identical request cannot
    #: help -- a 4xx other than 429. Retrying a rejected request wastes the
    #: operator's latency budget and, on a 401, risks tripping a lockout.
    retryable: bool = True

    @property
    def ok(self) -> bool:
        return self.text is not None


class DeepSeekClient:
    """Async DeepSeek chat client (CONTRACT 8.1).

    ``settings`` and ``metrics`` are duck-typed against ``app.config.Settings``
    and ``app.observability.metrics.Metrics`` so this module imports and tests
    without those peers present; all access is via ``getattr`` with defaults.
    """

    def __init__(
        self,
        settings: Any,
        metrics: Any = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # `transport` is an additive, keyword-only extension to the contract
        # signature: it exists so tests can inject httpx.MockTransport and run
        # with no network. Positional callers are unaffected.
        self._settings = settings
        self._metrics = metrics
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

        self._api_key = str(getattr(settings, "deepseek_api_key", "") or "").strip()
        self._base_url = str(getattr(settings, "deepseek_base_url", "") or "").rstrip("/")
        self._model = str(getattr(settings, "deepseek_model", "deepseek-chat") or "deepseek-chat")
        try:
            self._timeout = float(getattr(settings, "deepseek_timeout_seconds", 30) or 30)
        except (TypeError, ValueError):
            self._timeout = 30.0
        self._enabled = bool(getattr(settings, "llm_enabled", True))

    # -- introspection ----------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover - defensive, no secret
        # Deliberately excludes settings and the key: this object may be logged.
        return (
            f"DeepSeekClient(model={self._model!r}, available={self.available}, "
            f"timeout={self._timeout})"
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def available(self) -> bool:
        """False when the key is empty or ``LLM_ENABLED`` is false (CONTRACT 8.1).

        False is a supported mode, not an error: every caller degrades to the
        deterministic fallback.
        """

        return self._enabled and bool(self._api_key)

    @property
    def unavailable_reason(self) -> str | None:
        """A bounded, secret-free reason string, or None when available."""

        if not self._enabled:
            return "llm_disabled"
        if not self._api_key:
            return "api_key_missing"
        return None

    # -- lifecycle --------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                transport=self._transport,
            )
        return self._client

    async def aclose(self) -> None:
        """Release the underlying connection pool."""

        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def __aenter__(self) -> "DeepSeekClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- metrics ----------------------------------------------------------

    def _record(self, *, purpose: str, ok: bool, latency: float, fallback_reason: str | None) -> None:
        """Best-effort metrics (CONTRACT 10).

        Guarded so a metrics double in tests, or a partially built A9 registry,
        cannot turn a successful explanation into a failure.
        """

        record = getattr(self._metrics, "record_llm_call", None)
        if record is None:
            return
        try:
            record(purpose, ok, latency, fallback_reason)
        except Exception as exc:  # noqa: BLE001 - metrics must never break a call
            logger.warning("llm_metric_failed", purpose=purpose, error_type=type(exc).__name__)

    # -- the one public call ----------------------------------------------

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 700,
        temperature: float = 0.2,
        purpose: str = "explain",
    ) -> str:
        """Return a validated completion, or raise :class:`LLMUnavailable`.

        ``temperature`` defaults low: this is operational explanation, not
        creative writing (CONTRACT 8.1). ``purpose`` is an additive, keyword-only
        extension used for the bounded metric label on ``llm_calls_total``.
        """

        if not self.available:
            # No call is attempted; the reason is a bounded label, never a key.
            self._record(purpose=purpose, ok=False, latency=0.0, fallback_reason=self.unavailable_reason)
            raise LLMUnavailable(
                "DeepSeek is not available: " + str(self.unavailable_reason),
                reason=str(self.unavailable_reason),
            )

        endpoint = f"{self._base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        last = _Attempt(reason="unknown")
        attempts_made = 0
        started = time.perf_counter()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            attempts_made = attempt
            last = await self._attempt(endpoint, headers, payload)
            if last.ok:
                latency = time.perf_counter() - started
                logger.info(
                    "llm_call",
                    purpose=purpose,
                    model=self._model,
                    attempt=attempt,
                    ok=True,
                    latency_ms=round(latency * 1000, 1),
                    response_chars=len(last.text or ""),
                )
                self._record(purpose=purpose, ok=True, latency=latency, fallback_reason=None)
                return last.text or ""
            logger.warning(
                "llm_call_failed",
                purpose=purpose,
                model=self._model,
                attempt=attempt,
                reason=last.reason,
                status=last.status,
                error_type=last.error_type,
                # Redacted: a misconfigured base URL must not leak a key here.
                endpoint=redact(endpoint, self._api_key),
            )
            if not last.retryable:
                break

        latency = time.perf_counter() - started
        self._record(purpose=purpose, ok=False, latency=latency, fallback_reason=last.reason)

        # Built outside the `except` blocks in _attempt, so no httpx exception
        # carrying a URL or body becomes this exception's __context__.
        message = (
            f"DeepSeek request failed after {attempts_made} attempt(s): "
            f"reason={last.reason}"
        )
        if last.status is not None:
            message += f", status={last.status}"
        if last.error_type is not None:
            message += f", error={last.error_type}"
        message += f", endpoint={redact(endpoint, self._api_key)}"
        if last.detail:
            message += f", detail={redact(last.detail, self._api_key)}"
        raise LLMUnavailable(message, reason=str(last.reason or "unknown"))

    # -- one round trip ---------------------------------------------------

    async def _attempt(
        self, endpoint: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> _Attempt:
        """One POST. Returns a failure record instead of raising.

        Returning rather than raising keeps the credential out of any exception
        chain: nothing here holds a reference to a response or request object.
        """

        try:
            response = await self._http().post(endpoint, json=payload, headers=headers)
        except httpx.TimeoutException:
            return _Attempt(reason="timeout", error_type="TimeoutException")
        except httpx.HTTPError as exc:
            # Only the exception's class name is carried forward: its message
            # can contain the URL.
            return _Attempt(reason="transport_error", error_type=type(exc).__name__)

        if response.status_code == 429:
            return _Attempt(
                reason="rate_limited",
                status=429,
                detail=self._detail(response),
            )
        if response.status_code >= 500:
            return _Attempt(
                reason="server_error",
                status=response.status_code,
                detail=self._detail(response),
            )
        if response.status_code >= 400:
            return _Attempt(
                reason="http_error",
                status=response.status_code,
                detail=self._detail(response),
                # 4xx other than 429 is our fault and will not fix itself.
                retryable=False,
            )

        try:
            body = response.json()
        except ValueError:
            return _Attempt(reason="malformed_json", status=response.status_code)

        content = _extract_content(body)
        if content is None:
            return _Attempt(reason="malformed_response", status=response.status_code)
        try:
            text = validate_completion(content)
        except InvalidLLMResponse:
            return _Attempt(reason="invalid_content", status=response.status_code)
        return _Attempt(text=text, status=response.status_code)

    @staticmethod
    def _detail(response: httpx.Response) -> str | None:
        """A short, later-redacted snippet of an error body."""

        try:
            raw = response.text
        except Exception:  # noqa: BLE001 - a body we cannot read is simply omitted
            return None
        raw = (raw or "").strip()
        if not raw:
            return None
        return raw[:MAX_ERROR_DETAIL]
