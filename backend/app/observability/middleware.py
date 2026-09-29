"""HTTP instrumentation — brief section 14, the *application* layer.

Pure ASGI middleware rather than ``BaseHTTPMiddleware``, for one concrete
reason: the router writes the matched route onto ``scope``, and this class needs
to read it back after the response starts. An ASGI middleware holds the very
same ``scope`` dict the router mutates, so the read is guaranteed; with
``BaseHTTPMiddleware`` it is an implementation detail we would be betting on.

What this buys, beyond counting requests:

* **The ``path`` label is a route template, not a URL.** ``/api/v1/decisions/17``
  and ``/api/v1/decisions/18`` are one series, not two. Labelling by raw path is
  the same unbounded-cardinality mistake as labelling by ``station_id`` — it just
  hides until someone scrapes with real ids in it.
* **A ``request_id`` on every line and in the ``X-Request-ID`` response header**,
  so one operator action can be followed across the gateway, the engines and the
  LLM.
* **No body, ever.** The access log records method, path, status, duration and
  the request id — never the request or response payload (brief section 18).
"""

from __future__ import annotations

import re
import time
from typing import Any, Final, Iterable
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .logging import (
    bind_request_context,
    clear_request_context,
    get_logger,
    log_request,
)
from .metrics import METRICS, Metrics

__all__ = [
    "DEFAULT_EXCLUDED_PATHS",
    "ObservabilityMiddleware",
    "normalise_path",
]

#: Paths that are never instrumented. Scrapes are infrastructure traffic; if
#: they counted, they would inflate the request rate and visibly depress the
#: error rate of a struggling service — exactly when the number matters most.
DEFAULT_EXCLUDED_PATHS: Final[frozenset[str]] = frozenset({"/metrics"})

_UUID_SEGMENT = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
#: Long hex blobs are ids/tokens, not routes.
_HEX_SEGMENT = re.compile(r"\A[0-9a-fA-F]{16,}\Z")
_DIGITS_SEGMENT = re.compile(r"\A\d+\Z")

#: Ceiling on how many *unmatched* (404) paths may become distinct series. A
#: client walking random URLs must not be able to grow the registry without
#: bound; past the ceiling everything unmatched shares one series.
_UNMATCHED_SERIES_LIMIT: Final[int] = 64
_MAX_PATH_LABEL_CHARS: Final[int] = 200

_unmatched_paths: set[str] = set()


def _collapse_identifiers(path: str) -> str:
    """Replace id-shaped path segments with ``{id}``.

    Used only when the router did not match a route (a 404): there is no
    template to consult, but ``/api/v1/decisions/17`` must still not become
    ``/api/v1/decisions/18``. ``v1`` and other version-like segments are
    deliberately *not* collapsed — only whole segments that are entirely digits,
    a UUID, or a long hex string.

    The result is run through the logging layer's ``scrub_text`` by the caller,
    so a credential pasted into a URL cannot become a label value either.
    """
    if not path:
        return "/"
    segments = path.split("/")
    collapsed = []
    for segment in segments:
        if not segment:
            collapsed.append(segment)
        elif (_DIGITS_SEGMENT.match(segment) or _UUID_SEGMENT.match(segment)
              or _HEX_SEGMENT.match(segment)):
            collapsed.append("{id}")
        else:
            collapsed.append(segment)
    return "/".join(collapsed)


def normalise_path(path: str, route_template: str | None = None) -> str:
    """Return the bounded ``path`` label for one request.

    Preference order:

    1. the FastAPI route template (``/api/v1/decisions/{decision_id}``) — fully
       bounded, one series per route;
    2. otherwise the id-collapsed path, capped so an unmatched-URL flood cannot
       grow the registry without limit.

    Every branch ends in :func:`~app.observability.logging.scrub_text` at the
    call site in :class:`ObservabilityMiddleware`.
    """
    if route_template:
        template = route_template if route_template.startswith("/") else "/" + route_template
        return template[:_MAX_PATH_LABEL_CHARS]

    candidate = _collapse_identifiers(path.split("?")[0])[:_MAX_PATH_LABEL_CHARS]
    if candidate in _unmatched_paths:
        return candidate
    if len(_unmatched_paths) < _UNMATCHED_SERIES_LIMIT:
        _unmatched_paths.add(candidate)
        return candidate
    return "__unmatched__"


def _route_template(scope: Scope) -> str | None:
    """The matched route's template, if the router ran and matched."""
    route = scope.get("route")
    if route is None:
        return None
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    return template if isinstance(template, str) and template else None


class ObservabilityMiddleware:
    """Record one line and one metric family per HTTP request.

    Added by :func:`app.observability.setup.install_observability`; it is not
    meant to be added twice (that would double-count every request).
    """

    def __init__(self, app: ASGIApp, *, metrics: Metrics | None = None,
                 excluded_paths: Iterable[str] | None = None) -> None:
        self.app = app
        self.metrics = metrics or METRICS
        self.excluded_paths = frozenset(
            DEFAULT_EXCLUDED_PATHS if excluded_paths is None else excluded_paths
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("path") in self.excluded_paths:
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "GET"))
        raw_path = str(scope.get("path", "/"))
        request_id = uuid4().hex[:16]
        started = time.perf_counter()

        status: int | None = None
        failure: BaseException | None = None

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message.get("status", 500)
                try:
                    headers = MutableHeaders(scope=message)
                    if "x-request-id" not in headers:
                        headers.append("x-request-id", request_id)
                except Exception:  # noqa: BLE001 - a header must not break a response
                    pass
            await send(message)

        bind_request_context(request_id=request_id, method=method, path=raw_path)
        self.metrics.request_started()
        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException as exc:  # noqa: BLE001 - re-raised immediately below
            # Starlette's ExceptionMiddleware has already turned HTTPException
            # into a response; anything reaching here is unhandled and will
            # become a 500 upstream of us.
            failure = exc
        finally:
            duration = time.perf_counter() - started
            self.metrics.request_finished()
            if failure is not None and status is None:
                status = 500
            self._record(method, scope, raw_path, status, duration, request_id, failure)
            clear_request_context()

        if failure is not None:
            raise failure

    def _record(self, method: str, scope: Scope, raw_path: str, status: int | None,
                duration: float, request_id: str,
                failure: BaseException | None) -> None:
        """Emit the metric and the access line. Must never raise into the app."""
        try:
            path_label = normalise_path(raw_path, _route_template(scope))
            status_code = status if status is not None else 500
            self.metrics.observe_request(method, path_label, status_code, duration)

            fields: dict[str, Any] = {
                "method": method,
                "path": path_label,
                "status": status_code,
                "duration_ms": round(duration * 1000.0, 3),
            }
            if failure is not None:
                # Only the type and a capped, scrubbed message: an exception
                # string can echo a URL, and a URL can carry a credential.
                fields["error_type"] = type(failure).__name__
                fields["error"] = str(failure)
                fields["exc_info"] = failure
                _log().error("http_request_failed", **fields)
            else:
                log_request(request_id=request_id, **fields)
        except Exception as exc:  # noqa: BLE001
            # Instrumentation is not allowed to break a request. Say so loudly
            # rather than swallowing it.
            try:
                _log().error(
                    "observability_record_failed",
                    error_type=type(exc).__name__,
                    request_path=raw_path,
                )
            except Exception:  # noqa: BLE001 - logging is down; nothing left to do
                pass


def _log() -> Any:
    return get_logger("app.http")
