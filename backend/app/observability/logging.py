"""Structured logging for the platform — brief section 14, the *logs* layer.

Two jobs, and they are both security-relevant:

1. **JSON, one line per event.** structlog renders every record as a single JSON
   object so a log shipper (Loki/ELK) can index the fields rather than regex the
   prose.

2. **Redaction that actually holds.** CONTRACT.md sections 0.2 and 10 forbid a
   secret, an API key, a request body or a raw entity payload from ever reaching
   a log line. That cannot be a rule people remember to follow, so it is enforced
   by a structlog processor that runs on *every* record, immediately before the
   renderer:

   * any key that looks like a credential (``api_key``, ``token``, ``password``,
     ``authorization`` …) at any depth has its value replaced with
     ``[redacted]``;
   * every registered secret *literal* is scrubbed out of every string value, so
     a key that leaks into an innocuous field name -- or into an exception
     message -- is still removed;
   * every string is length-capped and containers are depth- and item-capped,
     which is what stops "just log the response body" from being possible.

   Secrets are registered with :func:`register_secret` /
   :func:`register_settings_secrets`. ``install_observability`` does the latter
   from the application ``Settings``, so the DeepSeek key is known to the
   redactor from the moment the app is built, without this module importing the
   configuration at module scope.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Mapping, MutableMapping, TextIO

import structlog

__all__ = [
    "REDACTED",
    "configure_logging",
    "get_logger",
    "log_decision",
    "log_fallback",
    "log_integration_failure",
    "log_recovery",
    "log_request",
    "log_shortage_alert",
    "redact_secrets",
    "register_secret",
    "register_settings_secrets",
    "scrub_text",
    "bind_request_context",
    "clear_request_context",
    "registered_secret_count",
]

# --------------------------------------------------------------------------- #
# Bounds. These are the difference between "we log the important thing" and
# "we logged a customer's payload into a shared index".
# --------------------------------------------------------------------------- #

#: Longest string value kept verbatim; anything longer is truncated.
MAX_STRING_CHARS = 500
#: Longest sequence rendered; the remainder is replaced by a count.
MAX_SEQUENCE_ITEMS = 20
#: Deepest nesting walked; below this the value is replaced by a placeholder.
MAX_DEPTH = 6
#: Longest exception message kept.
MAX_ERROR_CHARS = 300

REDACTED = "[redacted]"
_TRUNCATED_SUFFIX = "...[truncated]"
_DEPTH_SUFFIX = "[depth limit: nested payload omitted]"

#: Key names that are credentials by construction, wherever they appear.
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|apikey|secret|token|password|passwd|credential|"
    r"authorization|bearer|cookie|private[_-]?key|access[_-]?key)",
    re.IGNORECASE,
)

#: Shortest registered secret worth scrubbing. Anything shorter is too likely to
#: appear by accident inside ordinary prose and would mangle the logs.
_MIN_SECRET_CHARS = 6

_secrets: set[str] = set()
_secrets_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Secret registry
# --------------------------------------------------------------------------- #

def register_secret(value: str | None) -> None:
    """Teach the redactor one secret literal.

    Safe to call with ``None`` or a short/empty value; those are ignored rather
    than stored. Values never leave this module except as a replacement.
    """
    if not isinstance(value, str):
        return
    candidate = value.strip()
    if len(candidate) < _MIN_SECRET_CHARS:
        return
    with _secrets_lock:
        _secrets.add(candidate)


def register_settings_secrets(settings: Any | None) -> int:
    """Register every credential-looking field of a settings object.

    Accepts anything: a pydantic ``Settings`` (``model_dump``), a dataclass, or
    a plain object (``vars``). Returns the number of secrets registered, which
    is what lets a test assert the wiring actually happened.
    """
    if settings is None:
        return 0

    data: Mapping[str, Any] = {}
    dump = getattr(settings, "model_dump", None)
    if callable(dump):
        try:
            data = dump()
        except Exception:  # noqa: BLE001 - a broken dump must not break startup
            data = {}
    if not isinstance(data, Mapping) or not data:
        raw = getattr(settings, "__dict__", None)
        if isinstance(raw, dict):
            data = raw
    if not isinstance(data, Mapping):
        return 0

    before = registered_secret_count()
    for key, value in data.items():
        if isinstance(value, str) and _SECRET_KEY_RE.search(str(key)):
            register_secret(value)
    return registered_secret_count() - before


def registered_secret_count() -> int:
    """Number of secret literals currently known to the redactor."""
    with _secrets_lock:
        return len(_secrets)


def scrub_text(value: str, *, limit: int = MAX_STRING_CHARS) -> str:
    """Scrub registered secret literals out of one string and cap its length.

    Public because the metrics layer needs it too: a secret must never reach a
    *metric label* either, and a label is built from a URL path we do not fully
    control.
    """
    if not isinstance(value, str):  # defensive: callers pass anything
        return value
    with _secrets_lock:
        secrets = tuple(_secrets)
    for secret in secrets:
        if secret in value:
            value = value.replace(secret, REDACTED)
    if len(value) > limit:
        value = value[:limit] + _TRUNCATED_SUFFIX
    return value


# --------------------------------------------------------------------------- #
# The processor
# --------------------------------------------------------------------------- #

def _redact_value(value: Any, secrets: tuple[str, ...], depth: int = 0) -> Any:
    if depth > MAX_DEPTH:
        return _DEPTH_SUFFIX

    if isinstance(value, str):
        for secret in secrets:
            if secret in value:
                value = value.replace(secret, REDACTED)
        if len(value) > MAX_STRING_CHARS:
            value = value[:MAX_STRING_CHARS] + _TRUNCATED_SUFFIX
        return value

    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"

    if isinstance(value, Mapping):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if _SECRET_KEY_RE.search(str(key)):
                # Credential-shaped key: the value is discarded, not inspected.
                redacted[key] = REDACTED
            else:
                redacted[key] = _redact_value(item, secrets, depth + 1)
        return redacted

    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        redacted_items = [_redact_value(i, secrets, depth + 1)
                          for i in items[:MAX_SEQUENCE_ITEMS]]
        if len(items) > MAX_SEQUENCE_ITEMS:
            redacted_items.append(f"...[+{len(items) - MAX_SEQUENCE_ITEMS} more]")
        return redacted_items

    if isinstance(value, BaseException):
        # Never let an exception's args reach the log unfiltered.
        return _redact_value(f"{type(value).__name__}: {value}", secrets, depth)

    return value


def redact_secrets(logger: Any, method_name: str, event_dict: MutableMapping) -> MutableMapping:
    """structlog processor: strip credentials, cap payload size.

    Registered last-but-one in the chain so it inspects the *final* event dict --
    including the formatted traceback -- on every record, for every logger, with
    no cooperation required from the caller.
    """
    with _secrets_lock:
        secrets = tuple(_secrets)
    try:
        return _redact_value(event_dict, secrets, 0)
    except Exception:  # noqa: BLE001 - logging must never break the request
        return {"event": "log_redaction_failed", "level": "error"}


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def _level_to_int(level: str | int) -> int:
    if isinstance(level, int):
        return level
    return structlog.processors.NAME_TO_LEVEL.get(str(level).strip().lower(), logging.INFO)


def configure_logging(level: str | int = "INFO", *,
                      stream: TextIO | None = None) -> None:
    """Configure structlog for JSON output on stdout.

    ``stream`` exists so tests can capture the rendered output (and assert a
    secret is absent from it) rather than having to mock structlog; production
    callers pass nothing and get ``sys.stdout``.

    ``cache_logger_on_first_use`` is deliberately False: it is what makes a
    later reconfigure -- in tests, or by a peer -- take effect.
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # Must stay immediately before the renderer: this is the single
            # chokepoint every record passes through.
            redact_secrets,
            structlog.processors.JSONRenderer(sort_keys=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_level_to_int(level)),
        logger_factory=structlog.PrintLoggerFactory(file=stream),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str = "app") -> Any:
    """Return a structlog logger bound to ``name``.

    A fresh lookup each call is intentional: it keeps a reconfigure (e.g. in a
    test) effective, and structlog loggers are cheap.
    """
    return structlog.get_logger(name)


# --------------------------------------------------------------------------- #
# Request context
# --------------------------------------------------------------------------- #

def bind_request_context(**fields: Any) -> None:
    """Bind fields (request_id, path, …) onto every subsequent log record."""
    structlog.contextvars.bind_contextvars(**fields)


def clear_request_context() -> None:
    """Drop bound request fields. Called by the middleware in its ``finally``."""
    structlog.contextvars.clear_contextvars()


# --------------------------------------------------------------------------- #
# Event helpers -- the four log events brief section 14 asks for
# --------------------------------------------------------------------------- #

def _emit(event: str, level: str, args: tuple[Any, ...], fields: dict[str, Any]) -> None:
    log = get_logger("app")
    payload: dict[str, Any] = dict(fields)
    if args:
        # Tolerate positional style: log_decision("submitted", decision_id=7).
        payload.setdefault("detail", " ".join(str(a) for a in args))
    if event == "integration_failure" and "error" in payload:
        error = payload["error"]
        payload["error"] = scrub_text(str(error), limit=MAX_ERROR_CHARS)
        payload["error_type"] = type(error).__name__
    # The positional argument becomes event_dict["event"]; no need to also pass
    # it as a keyword.
    getattr(log, level)(event, **payload)


def log_request(*args: Any, **fields: Any) -> None:
    """One line per HTTP request. Emitted by the middleware."""
    _emit("http_request", "info", args, fields)


def log_decision(*args: Any, **fields: Any) -> None:
    """A recommendation or allocation decision was produced.

    Callers log identifiers and *numbers* (station_id, fuel_type, liters,
    confidence, policy) -- never the raw entity payload. A station id is fine in
    a log line; it is only forbidden as a metric *label* (see ``metrics.py``).
    """
    _emit("decision", "info", args, fields)


def log_fallback(*args: Any, **fields: Any) -> None:
    """A degradation path activated: LLM unavailable, breaker open, optimizer
    infeasible, database down. Warning level -- the brief grades this event."""
    _emit("fallback_activated", "warning", args, fields)


def log_recovery(*args: Any, **fields: Any) -> None:
    """The system returned to its healthy path after a degradation."""
    _emit("recovery", "info", args, fields)


def log_integration_failure(*args: Any, **fields: Any) -> None:
    """A call to the simulator or the LLM failed. Error level."""
    _emit("integration_failure", "error", args, fields)


def log_shortage_alert(*args: Any, **fields: Any) -> None:
    """A shortage/risk signal was detected (brief section 6)."""
    _emit("shortage_alert", "warning", args, fields)
