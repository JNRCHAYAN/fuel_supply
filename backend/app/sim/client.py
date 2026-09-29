"""``SimulatorClient`` -- the single gateway to the BUP Fuel Supply Simulator.

CONTRACT.md 5.4 (surface) and 5.5 (resilience). The gateway owns four things the
rest of the backend must never have to think about:

* **Timeouts and bounded retries.** Every request is bounded by
  ``SIMULATOR_TIMEOUT_SECONDS``; 5xx and transport failures are retried with
  exponential backoff, 4xx never is.
* **A circuit breaker.** After ``CIRCUIT_FAILURE_THRESHOLD`` consecutive
  *unhealthy* failures the breaker opens for ``CIRCUIT_RESET_SECONDS`` and then
  admits exactly one trial request.
* **A last-good cache.** Every successful read is remembered as
  ``(value, monotonic_ts)``. While the breaker is open, reads serve that value
  instead of failing. When there is nothing cached the read **raises**: the
  platform degrades, it never invents a number (brief section 11).
* **Envelope normalisation.** Three error shapes in, one ``SimulatorError`` out.

REST is the source of truth; an SSE event is only a hint
-------------------------------------------------------
This is the load-bearing design decision in this module, so it is stated once,
here, and honoured everywhere below. Nothing in this client -- and nothing in
any consumer of ``StreamEvent`` -- may compute a figure from an event payload.
``simulation.tick`` carries a tick number on the wire, and it is still not a
value we are allowed to use: the events are fire-and-forget, a subscriber more
than 200 events behind is dropped by the simulator (integration guide section
6.1), and frames can be missed entirely. So the SSE consumer here does exactly
one thing with an event -- hands it to the subscribers as a hint that *something
changed*, and lets them re-fetch REST. A dropped or reordered event can
therefore make a number **late**, but never **wrong**.

Settings and metrics are read defensively
-----------------------------------------
``Settings`` (A1) and ``Metrics`` (A9) are other workstreams' classes. This
module reads the handful of attributes it needs with ``getattr`` and a sane
default, and calls metric hooks only when they exist. That keeps the gateway
importable and testable on its own, and means a missing metric hook degrades
observability rather than breaking a request.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import time
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Literal,
)

import httpx

from app.sim.circuit import CircuitBreaker
from app.sim.errors import SimulatorError, body_from_response
from app.sim.models import (
    STREAM_EVENT_NAMES,
    Allocation,
    AllocationRequest,
    AuditEntry,
    CircuitState,
    DemandObservation,
    Depot,
    DomainEvent,
    EventRequest,
    Fault,
    FaultRequest,
    Health,
    Metrics,
    Region,
    Route,
    SimInstance,
    Snapshot,
    Station,
    StreamEvent,
    SupplyArrival,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from app.config import Settings
    from app.observability.metrics import Metrics as ObservabilityMetrics

__all__ = ["SimulatorClient", "iter_stream_events", "SSEFrameDecoder"]

logger = logging.getLogger(__name__)

#: Failure kinds that say "the simulator is unhealthy" and so count towards the
#: breaker. A 4xx is deliberately absent -- see ``_counts_against_breaker``.
_UNHEALTHY_KINDS = frozenset({"transport", "decode", "invalid_payload"})

_RETRY_BASE_DELAY = 0.25
_RETRY_MAX_DELAY = 5.0
_RETRY_JITTER = 0.1

_STREAM_BASE_DELAY = 0.5
_STREAM_MAX_DELAY = 30.0

_BREAKER_COMPONENT = "simulator"

#: The simulator's stale-data signal. Guide sections 3, 6.4 and 7.10: while a
#: ``stale_data`` fault is active every non-stream ``/v1/*`` GET carries this
#: response header with the value ``true``. The SSE stream deliberately does
#: not, so only the REST read path below has to honour it.
_STALE_HEADER = "X-Simulator-Stale"


def _setting(settings: Any, name: str, default: Any) -> Any:
    value = getattr(settings, name, default)
    return default if value is None else value


def _is_simulator_stale(response: httpx.Response) -> bool:
    """Did the simulator flag this response as stale?

    Lookup goes through ``httpx.Headers``, which folds header names
    case-insensitively, so the wire casing (HTTP/1.1 servers commonly send
    ``x-simulator-stale``) cannot hide the flag. An absent header and any value
    that is not true-looking both mean "not stale" -- the conservative reading,
    because the normal path is only ever overridden by an explicit ``true``.
    """
    raw = response.headers.get(_STALE_HEADER)
    if raw is None:
        return False
    return raw.strip().lower() in {"true", "1"}


def _as_model_list(model: type, payload: Any, *, endpoint: str) -> list[Any]:
    """Parse a list endpoint.

    Shape is validated (a 200 carrying an object where an array belongs is a
    real contract violation and raises), values are tolerated. The
    ``isinstance(payload, list)`` test is what keeps an error body's dict from
    being iterated as if it were a collection of records.
    """
    if not isinstance(payload, list):
        raise ValueError(
            f"{endpoint}: expected a JSON array, got {type(payload).__name__}"
        )
    return [model.from_api(item) for item in payload]


def _as_free_mapping(payload: Any, *, endpoint: str) -> dict[str, Any]:
    """For the admin endpoints whose response schema the simulator leaves open.

    ``/admin/step`` and ``/admin/reset`` declare ``{}`` as their response schema
    in the simulator's own ``/openapi.json``, so the honest thing is to carry
    whatever came back and not pretend to know its shape.
    """
    if isinstance(payload, dict):
        return payload
    return {"value": payload}


# --------------------------------------------------------------------------- #
# Server-Sent Events decoding
# --------------------------------------------------------------------------- #


class SSEFrameDecoder:
    """Incremental ``text/event-stream`` frame decoder.

    Kept as a plain synchronous object so the framing rules -- the part with all
    the edge cases -- can be unit-tested without a socket. Rules implemented:
    a blank line dispatches; a line starting with ``:`` is a comment (the live
    simulator opens every stream with ``: connected``); a field is
    ``name:value`` with a single optional leading space stripped from the value;
    multiple ``data`` lines join with newlines; a frame with no ``data`` never
    dispatches. ``id`` and ``retry`` are accepted and ignored -- we never resume
    by id, because REST is the source of truth and a missed event only makes a
    number late.
    """

    def __init__(self) -> None:
        self._name: str | None = None
        self._data: list[str] = []

    def feed(self, line: str) -> list[tuple[str, str]]:
        line = line.rstrip("\r")
        if line == "":
            return self._flush()
        if line.startswith(":"):
            return []
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            self._name = value
        elif field == "data":
            self._data.append(value)
        return []

    def close(self) -> list[tuple[str, str]]:
        """Flush a final frame that arrived without a trailing blank line."""
        return self._flush()

    def _flush(self) -> list[tuple[str, str]]:
        if not self._data:
            self._name = None
            self._data = []
            return []
        frame = (self._name or "message", "\n".join(self._data))
        self._name = None
        self._data = []
        return [frame]


def _build_stream_event(name: str, raw: str) -> StreamEvent | None:
    """Turn one frame into a ``StreamEvent``, or drop it.

    A frame we cannot parse is dropped rather than raised: events are hints, so
    losing one costs at most a *late* number, and a stream that died on a single
    bad frame would be strictly worse. Note the same dict-before-anything-else
    discipline as :mod:`app.sim.errors` -- a JSON array payload is not a dict.
    """
    try:
        parsed: Any = json.loads(raw) if raw.strip() else {}
    except ValueError:
        logger.warning(
            "dropping malformed SSE frame",
            extra={"event": "sim.sse_bad_frame", "sse_event": name},
        )
        return None

    if isinstance(parsed, dict):
        data = parsed
    else:
        data = {"value": parsed}

    if name not in STREAM_EVENT_NAMES:
        logger.debug("unrecognised SSE event name %r; carrying it as a hint", name)

    return StreamEvent(name=name, data=data, received_at=time.monotonic())


async def iter_stream_events(response: httpx.Response) -> AsyncIterator[StreamEvent]:
    """Yield ``StreamEvent``s from a live ``httpx`` streaming response."""
    decoder = SSEFrameDecoder()
    async for line in response.aiter_lines():
        for name, raw in decoder.feed(line):
            event = _build_stream_event(name, raw)
            if event is not None:
                yield event
    for name, raw in decoder.close():
        event = _build_stream_event(name, raw)
        if event is not None:
            yield event


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #


class SimulatorClient:
    """Typed, resilient async client for the simulator's REST and SSE surface."""

    def __init__(
        self,
        settings: "Settings | Any",
        metrics: "ObservabilityMetrics | Any | None" = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._base_url = str(
            _setting(settings, "simulator_base_url", "http://simulator-api:8000")
        ).rstrip("/")
        self._timeout = float(_setting(settings, "simulator_timeout_seconds", 10))
        self._max_attempts = max(
            1, int(_setting(settings, "simulator_max_retries", 3))
        )
        #: How long a successful read is reused, and the window within which
        #: concurrent callers share one in-flight fetch. See :attr:`memo_seconds`.
        self._memo_seconds = max(
            0.0, float(_setting(settings, "simulator_cache_ttl_seconds", 1.0))
        )
        self._metrics = metrics

        self._clock = clock
        self._sleep = sleep
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        # The published simulator has a small database pool. Coalescing only
        # merges identical reads; snapshot fan-out and distinct history queries
        # still need a shared bound. SSE must not hold one of these REST slots.
        self._request_slots = asyncio.Semaphore(2)

        self._breaker = CircuitBreaker(
            failure_threshold=int(_setting(settings, "circuit_failure_threshold", 5)),
            reset_seconds=float(_setting(settings, "circuit_reset_seconds", 30)),
            clock=clock,
            on_state_change=self._on_breaker_state_change,
            name=_BREAKER_COMPONENT,
        )

        #: key -> (value, monotonic_ts). The last-good cache. Keys are per
        #: endpoint *and per query*, so a cached page of demand history can
        #: never be served as the answer to a different query.
        self._last_good: dict[str, tuple[Any, float]] = {}

        #: key -> the read currently in flight for that key. Callers arriving
        #: while it runs await it instead of starting a second one; see
        #: :meth:`_coalesced_read`.
        self._inflight: dict[str, asyncio.Task[tuple[Any, bool, float]]] = {}

        self._subscribers: list[Callable[[StreamEvent], Any]] = []
        self._stream_task: asyncio.Task[None] | None = None
        self._closed = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Start the SSE consumer task. Idempotent."""
        if self._closed:
            raise SimulatorError(
                "simulator client is closed", kind="invalid_request"
            )
        if self._stream_task is None or self._stream_task.done():
            self._stream_task = asyncio.create_task(
                self._consume_stream(), name="simulator-sse"
            )
            logger.info(
                "simulator SSE consumer started",
                extra={"event": "sim.sse_started", "base_url": self._base_url},
            )

    async def aclose(self) -> None:
        """Stop the consumer and release the connection pool."""
        self._closed = True
        task, self._stream_task = self._stream_task, None
        if task is not None:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.warning(
                    "SSE consumer ended with an error during shutdown",
                    exc_info=True,
                    extra={"event": "sim.sse_shutdown_error"},
                )

        client, self._client = self._client, None
        if client is not None and not client.is_closed:
            await client.aclose()

    async def __aenter__(self) -> "SimulatorClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ #
    # Resilience surface (consumed by /api/v1/status)
    # ------------------------------------------------------------------ #

    @property
    def breaker_state(self) -> Literal["closed", "open", "half_open"]:
        return self._breaker.state.value  # type: ignore[return-value]

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    def last_good(self, key: str) -> tuple[Any, float] | None:
        """``(value, monotonic_ts)`` of the last successful read for ``key``."""
        return self._last_good.get(key)

    def last_good_age(self, key: str) -> float | None:
        cached = self._last_good.get(key)
        if cached is None:
            return None
        return max(0.0, self._clock() - cached[1])

    @property
    def memo_seconds(self) -> float:
        """How long a successful read is reused before the simulator is asked again.

        Zero disables the memo entirely, which restores a network read per
        call -- useful in a test that wants to count requests.
        """
        return self._memo_seconds

    def cached_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._last_good))

    # ------------------------------------------------------------------ #
    # Reads -- every one raises SimulatorError, never returns None
    # ------------------------------------------------------------------ #

    async def get_health(self) -> Health:
        value, _, _ = await self._read("health", "/v1/health", Health.from_api)
        return value

    async def get_instance(self) -> SimInstance:
        value, _, _ = await self._read("instance", "/v1/instance", SimInstance.from_api)
        return value

    async def get_regions(self) -> list[Region]:
        value, _, _ = await self._read(
            "regions", "/v1/regions", self._list_parser(Region)
        )
        return value

    async def get_depots(self) -> list[Depot]:
        value, _, _ = await self._read("depots", "/v1/depots", self._list_parser(Depot))
        return value

    async def get_stations(self) -> list[Station]:
        value, _, _ = await self._read(
            "stations", "/v1/stations", self._list_parser(Station)
        )
        return value

    async def get_routes(self) -> list[Route]:
        value, _, _ = await self._read("routes", "/v1/routes", self._list_parser(Route))
        return value

    async def get_supply_arrivals(self) -> list[SupplyArrival]:
        value, _, _ = await self._read(
            "supply-arrivals", "/v1/supply-arrivals", self._list_parser(SupplyArrival)
        )
        return value

    async def get_events(self) -> list[DomainEvent]:
        value, _, _ = await self._read(
            "events", "/v1/events", self._list_parser(DomainEvent)
        )
        return value

    async def get_allocations(self) -> list[Allocation]:
        value, _, _ = await self._read(
            "allocations", "/v1/allocations", self._list_parser(Allocation)
        )
        return value

    async def get_demand_history(
        self, station_id: str | None = None, limit: int = 200
    ) -> list[DemandObservation]:
        """``GET /v1/demand-history``.

        Guide 4.11: ``limit`` is clamped to ``[1, 2000]`` and defaults to 200.
        The clamp is applied here as well as server-side, so a caller passing a
        nonsense limit gets a defined page instead of the simulator's 422 -- and
        because the cache key carries the *clamped* value, two spellings of the
        same query share one entry rather than caching the same page twice.
        Callers that pass an explicit in-range limit (the ingestor and the
        intelligence layer pass 1500) are forwarded unchanged.
        """
        limit = max(1, min(2000, int(limit)))
        params: dict[str, Any] = {"limit": limit}
        if station_id is not None:
            params["station_id"] = station_id
        # The cache key carries the query: a cached page must never answer a
        # different question.
        key = f"demand-history:{station_id or '*'}:{limit}"
        value, _, _ = await self._read(
            key,
            "/v1/demand-history",
            self._list_parser(DemandObservation),
            params=params,
        )
        return value

    async def get_metrics(self) -> Metrics:
        value, _, _ = await self._read("metrics", "/v1/metrics", Metrics.from_api)
        return value

    async def get_audit(self, limit: int = 50) -> list[AuditEntry]:
        """``GET /admin/audit``. Not in the contract's client surface.

        Added because the simulator has it, ``AuditEntry`` is already a pinned
        model, and the decision audit story (brief section 20) is stronger when
        the simulator's own trail can be shown next to ours.
        """
        value, _, _ = await self._read(
            f"audit:{int(limit)}",
            "/admin/audit",
            self._list_parser(AuditEntry),
            params={"limit": int(limit)},
        )
        return value

    # ------------------------------------------------------------------ #
    # Writes -- operator-initiated only, never cached, never fabricated
    # ------------------------------------------------------------------ #

    async def create_allocation(self, req: AllocationRequest) -> Allocation:
        """``POST /v1/allocations``.

        The simulator requires an ``idempotency_key``, which is what makes a
        retry after a timeout safe: a replay is de-duplicated (only one
        allocation is created) and comes back as **201**, not the 200 the
        integration guide documents. We accept any 2xx, so the guide's claimed
        200 and the observed 201 both succeed.
        """
        return await self._write(
            "create-allocation",
            "POST",
            "/v1/allocations",
            Allocation.from_api,
            json_body=req.to_payload(),
        )

    async def cancel_allocation(self, allocation_id: int) -> Allocation:
        """``POST /v1/allocations/{id}/cancel``."""
        return await self._write(
            f"cancel-allocation:{int(allocation_id)}",
            "POST",
            f"/v1/allocations/{int(allocation_id)}/cancel",
            Allocation.from_api,
        )

    # ------------------------------------------------------------------ #
    # Admin / scenario control
    # ------------------------------------------------------------------ #

    async def admin_run(self) -> SimInstance:
        return await self._write(
            "admin-run", "POST", "/admin/run", SimInstance.from_api
        )

    async def admin_pause(self) -> SimInstance:
        return await self._write(
            "admin-pause", "POST", "/admin/pause", SimInstance.from_api
        )

    async def admin_toggle(self) -> SimInstance:
        """``POST /admin/toggle`` (guide 7.4) -- flip RUNNING <-> PAUSED.

        A convenience for UIs, so it takes no request body and answers with the
        instance in its new state, exactly like :meth:`admin_run` and
        :meth:`admin_pause`. ``/admin/*`` paths stay unprefixed -- they bypass
        fault injection and are not under ``/v1``.
        """
        return await self._write(
            "admin-toggle", "POST", "/admin/toggle", SimInstance.from_api
        )

    async def admin_step(self) -> dict[str, Any]:
        return await self._write(
            "admin-step",
            "POST",
            "/admin/step",
            lambda payload: _as_free_mapping(payload, endpoint="/admin/step"),
        )

    async def admin_reset(self) -> dict[str, Any]:
        return await self._write(
            "admin-reset",
            "POST",
            "/admin/reset",
            lambda payload: _as_free_mapping(payload, endpoint="/admin/reset"),
        )

    async def admin_inject_event(self, req: EventRequest) -> DomainEvent:
        return await self._write(
            "admin-inject-event",
            "POST",
            "/admin/events",
            DomainEvent.from_api,
            json_body=req.to_payload(),
        )

    async def admin_inject_fault(self, req: FaultRequest) -> Fault:
        return await self._write(
            "admin-inject-fault",
            "POST",
            "/admin/faults",
            Fault.from_api,
            json_body=req.to_payload(),
        )

    async def admin_clear_faults(self) -> dict[str, Any]:
        return await self._write(
            "admin-clear-faults",
            "POST",
            "/admin/faults/clear",
            lambda payload: _as_free_mapping(payload, endpoint="/admin/faults/clear"),
        )

    async def admin_get_faults(self) -> list[Fault]:
        value, _, _ = await self._read(
            "admin-faults", "/admin/faults", self._list_parser(Fault)
        )
        return value

    async def admin_get_events(self) -> list[DomainEvent]:
        """``GET /admin/events`` (guide 7.13) -- last 50 injected events, id-desc.

        The read-side twin of :meth:`admin_inject_event`. It shares the path with
        the injector but a different method and cache key, and ``/admin/*``
        bypasses fault injection, so a response here never carries the
        ``X-Simulator-Stale`` signal.
        """
        value, _, _ = await self._read(
            "admin-events", "/admin/events", self._list_parser(DomainEvent)
        )
        return value

    # ------------------------------------------------------------------ #
    # Snapshot assembly (the one place staleness is expressed)
    # ------------------------------------------------------------------ #

    async def build_snapshot(self) -> Snapshot:
        """Assemble a ``Snapshot``, marked stale when any part is not current.

        ``Snapshot`` is the only type in the contract with ``stale`` and
        ``age_seconds`` fields, so this is where "last good answer, not a fresh
        one" is recorded. A component read is stale either because the breaker
        served its cache or because the simulator flagged the body
        ``X-Simulator-Stale`` (see :meth:`_read`); either way the assembled
        snapshot is marked stale with the age of its *oldest* component. If some
        part has no cached value at all, we fall back to the previous whole
        snapshot rather than mixing fresh and stale halves into something that
        looks complete. A stale snapshot is not itself cached.
        """
        try:
            parts = await asyncio.gather(
                self._read("instance", "/v1/instance", SimInstance.from_api),
                self._read("depots", "/v1/depots", self._list_parser(Depot)),
                self._read("stations", "/v1/stations", self._list_parser(Station)),
                self._read("routes", "/v1/routes", self._list_parser(Route)),
                self._read("regions", "/v1/regions", self._list_parser(Region)),
                self._read(
                    "supply-arrivals",
                    "/v1/supply-arrivals",
                    self._list_parser(SupplyArrival),
                ),
                self._read("events", "/v1/events", self._list_parser(DomainEvent)),
                self._read("metrics", "/v1/metrics", Metrics.from_api),
            )
        except SimulatorError as exc:
            cached = self._last_good.get("snapshot")
            if cached is not None:
                snapshot, ts = cached
                age = max(0.0, self._clock() - ts)
                logger.warning(
                    "serving stale snapshot: %s (age %.1fs)",
                    exc,
                    age,
                    extra={"event": "sim.snapshot_stale", "age_seconds": round(age, 3)},
                )
                return snapshot.as_stale(age_seconds=age)
            raise

        (
            (instance, inst_stale, inst_age),
            (depots, dep_stale, dep_age),
            (stations, sta_stale, sta_age),
            (routes, rou_stale, rou_age),
            (regions, reg_stale, reg_age),
            (arrivals, arr_stale, arr_age),
            (events, evt_stale, evt_age),
            (metrics, met_stale, met_age),
        ) = parts

        # Aggregate the honest worst case: any component from cache makes the
        # whole snapshot stale, and the age reported is the oldest piece in it.
        stale = any(
            (
                inst_stale,
                dep_stale,
                sta_stale,
                rou_stale,
                reg_stale,
                arr_stale,
                evt_stale,
                met_stale,
            )
        )
        age = max(inst_age, dep_age, sta_age, rou_age, reg_age, arr_age, evt_age, met_age)

        snapshot = Snapshot.assemble(
            instance=instance,
            depots=depots,
            stations=stations,
            routes=routes,
            regions=regions,
            supply_arrivals=arrivals,
            events=events,
            metrics=metrics,
            taken_at=self._clock(),
            stale=stale,
            age_seconds=age,
        )
        if not stale:
            self._last_good["snapshot"] = (snapshot, self._clock())
        return snapshot

    # ------------------------------------------------------------------ #
    # Streaming
    # ------------------------------------------------------------------ #

    async def stream(self) -> AsyncIterator[StreamEvent]:
        """Yield events from ``GET /v1/stream``, reconnecting forever.

        Exponential backoff on every drop, with the attempt counter reset once a
        connection actually delivers an event -- so a server that accepts and
        then immediately drops cannot be hammered at the base delay. Exits only
        when the client is closed or the surrounding task is cancelled.

        The events yielded here carry data that must not be trusted as state;
        see the module docstring.
        """
        attempt = 0
        while not self._closed:
            try:
                async with self._http().stream(
                    "GET",
                    "/v1/stream",
                    headers={"Accept": "text/event-stream"},
                    timeout=httpx.Timeout(None, connect=self._timeout),
                ) as response:
                    if response.status_code >= 400:
                        raw = await response.aread()
                        raise SimulatorError.from_body(
                            response.status_code,
                            _decode_stream_error_body(raw),
                            method="GET",
                            url="/v1/stream",
                        )
                    delivered = False
                    async for event in iter_stream_events(response):
                        if not delivered:
                            delivered = True
                            attempt = 0
                        yield event
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, SimulatorError) as exc:
                logger.warning(
                    "SSE stream dropped (%s); reconnecting", exc,
                    extra={"event": "sim.sse_reconnect", "attempt": attempt},
                )
            except Exception:  # noqa: BLE001 - a stream must never kill the consumer
                logger.warning(
                    "unexpected SSE failure; reconnecting",
                    exc_info=True,
                    extra={"event": "sim.sse_reconnect_unexpected"},
                )

            if self._closed:
                return
            delay = self._stream_backoff(attempt)
            attempt += 1
            await self._sleep(delay)

    def subscribe(
        self, cb: Callable[[StreamEvent], Awaitable[None]]
    ) -> Callable[[], None]:
        """Register an event callback. Returns an unsubscribe callable.

        Subscribers are called with a *hint*. The right response to one is to
        re-fetch the REST endpoint it concerns.
        """
        self._subscribers.append(cb)

        def _unsubscribe() -> None:
            if cb in self._subscribers:
                self._subscribers.remove(cb)

        return _unsubscribe

    async def _consume_stream(self) -> None:
        while not self._closed:
            try:
                async for event in self.stream():
                    if self._closed:
                        return
                    await self._dispatch(event)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - restart rather than die silently
                logger.warning(
                    "SSE consumer crashed; restarting after backoff",
                    exc_info=True,
                    extra={"event": "sim.sse_consumer_restart"},
                )
                await self._sleep(self._stream_backoff(0))
            else:
                return

    async def _dispatch(self, event: StreamEvent) -> None:
        for cb in tuple(self._subscribers):
            try:
                result = cb(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 - one bad subscriber must not
                # silence the stream for everyone else.
                logger.warning(
                    "SSE subscriber raised; stream continues",
                    exc_info=True,
                    extra={"event": "sim.sse_subscriber_error", "sse_event": event.name},
                )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._timeout),
                transport=self._transport,
                headers={"Accept": "application/json"},
            )
        return self._client

    @staticmethod
    def _list_parser(model: type) -> Callable[[Any], list[Any]]:
        name = model.__name__

        def _parse(payload: Any) -> list[Any]:
            return _as_model_list(model, payload, endpoint=name)

        return _parse

    async def _read(
        self,
        key: str,
        path: str,
        parser: Callable[[Any], Any],
        *,
        params: dict[str, Any] | None = None,
    ) -> tuple[Any, bool, float]:
        """A memoised, coalesced, cached, breakered, retried GET.

        Returns ``(value, stale, age_seconds)``. The public ``get_*`` methods
        drop the last two; ``build_snapshot`` uses them to mark staleness.

        ``stale`` means "this value must not be trusted as current". There are
        two ways a read earns it:

        * the breaker refused the call and a last-good value was served
          (:meth:`_serve_last_good`) -- the simulator was not reached; or
        * the simulator *did* answer, but flagged the body ``X-Simulator-Stale``
          because a ``stale_data`` fault is active (guide 6.4/7.10) -- the
          number is on the wire, it just is not current.

        Both cases leave the value out of the caches, so anything reported stale
        is reported stale on the way in as well as the way out.

        Two guards stand in front of the network, and they answer different
        problems:

        * **The memo.** A value read less than :attr:`memo_seconds` ago is
          reused, carrying its true age. Only values the simulator stood behind
          are ever stored (see :meth:`_attempt`), so a memo hit is never stale.
        * **Coalescing**, in :meth:`_coalesced_read`. Callers arriving while a
          fetch for the same key is in flight await that fetch. This is what
          stops N concurrent operators becoming N simulator requests.

        Both are skipped while the breaker is open. That branch owns its own
        cache semantics and reports everything it serves as stale; letting the
        memo short-circuit it would make an open breaker report cached reads
        as fresh -- and ``/api/v1/status`` probes ``get_health()`` through this
        method, so the console would show a healthy simulator during an outage.
        """
        if not self._breaker.allow():
            # The call above is the only ``allow()`` in a read. It is stateful
            # -- under HALF_OPEN the first call claims the single trial and a
            # second would be refused -- so asking again downstream would
            # reject the very request it just authorised.
            return await self._serve_last_good(key, path)

        # HALF_OPEN means ``allow()`` just spent the one trial on this call, and
        # a trial has to reach the simulator to settle it. Serving the memo here
        # would leave that trial unresolved and strand the breaker half-open.
        if self._breaker.state is CircuitState.CLOSED:
            memo = self._last_good.get(key)
            if memo is not None:
                age = max(0.0, self._clock() - memo[1])
                if age < self._memo_seconds:
                    self._metric("record_sim_memo_hit", key, age)
                    return memo[0], False, age

        return await self._coalesced_read(key, path, parser, params=params)

    async def _coalesced_read(
        self,
        key: str,
        path: str,
        parser: Callable[[Any], Any],
        *,
        params: dict[str, Any] | None = None,
    ) -> tuple[Any, bool, float]:
        """Run one fetch per key, however many callers are waiting on it.

        ``shield`` keeps the shared fetch alive if the caller that started it
        is cancelled -- an operator closing a tab must not cancel the read that
        everyone else is awaiting. The registration is removed by a
        done-callback guarded on identity, so a slow fetch cannot evict the
        entry that has since replaced it.
        """
        inflight = self._inflight.get(key)
        if inflight is not None:
            return await asyncio.shield(inflight)

        task: asyncio.Task[tuple[Any, bool, float]] = asyncio.create_task(
            self._attempt(key, path, parser, params=params)
        )
        self._inflight[key] = task

        def _forget(finished: asyncio.Task[Any], key: str = key) -> None:
            if self._inflight.get(key) is finished:
                self._inflight.pop(key, None)
            if not finished.cancelled():
                # Mark any exception as retrieved. Every awaiter does this
                # already, but if the one that started the fetch disconnected
                # before the others arrived, asyncio would otherwise log a
                # "never retrieved" warning for a handled failure.
                finished.exception()

        task.add_done_callback(_forget)
        return await asyncio.shield(task)

    async def _serve_last_good(self, key: str, path: str) -> tuple[Any, bool, float]:
        """The breaker refused this read: serve the last good value, or fail.

        Never fabricates: with nothing cached for ``key`` the read raises
        rather than inventing a plausible-looking value.
        """
        cached = self._last_good.get(key)
        if cached is not None:
            value, ts = cached
            age = max(0.0, self._clock() - ts)
            logger.warning(
                "simulator circuit open; serving last-good %r (age %.1fs)",
                key,
                age,
                extra={
                    "event": "sim.stale_read",
                    "cache_key": key,
                    "age_seconds": round(age, 3),
                },
            )
            self._metric("record_sim_stale_read", key, age)
            return value, True, age
        # Degrade, never fabricate: no cached value means an honest failure.
        raise SimulatorError.circuit_open(
            f"simulator circuit breaker is open and there is no cached "
            f"value for {key!r}; refusing to fabricate one",
            url=path,
        )

    async def _attempt(
        self,
        key: str,
        path: str,
        parser: Callable[[Any], Any],
        *,
        params: dict[str, Any] | None = None,
    ) -> tuple[Any, bool, float]:
        """One real GET, whose outcome settles whatever the breaker decided.

        The caller has already been authorised by ``allow()``. Asking again
        here would be refused under HALF_OPEN, where the first call claims the
        single trial and the second finds it already in flight.

        A 2xx is cached as last-good *unless* the simulator flagged the body
        stale, in which case the value is returned ``stale=True`` and written
        nowhere -- see :meth:`_read` for why both halves matter.
        """
        try:
            payload, simulator_stale = await self._send_with_retry(
                "GET", path, params=params
            )
            value = self._parse(key, parser, payload, path)
        except SimulatorError as exc:
            self._record_failure(exc, url=path)
            raise

        self._breaker.record_success()
        self._emit_breaker_state()

        if simulator_stale:
            # The simulator answered, and told us the body must not be trusted
            # as current. Recording success with the breaker is still right --
            # the service is demonstrably up -- but nothing may be cached: a
            # value the simulator itself has disowned can neither be memoised
            # as fresh nor come back later as "last good", which build_snapshot
            # would relabel as merely aged.
            logger.warning(
                "simulator flagged %r as stale (%s); not caching",
                key,
                _STALE_HEADER,
                extra={"event": "sim.stale_response", "cache_key": key},
            )
            self._metric("record_sim_stale_read", key, 0.0)
            return value, True, 0.0

        self._last_good[key] = (value, self._clock())
        return value, False, 0.0

    async def _write(
        self,
        key: str,
        method: str,
        path: str,
        parser: Callable[[Any], Any],
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """A breakered, retried, *never cached* write.

        Retries are safe for the endpoints that take an idempotency key. There
        is deliberately no last-good fallback: a write that cannot reach the
        simulator must fail loudly, because the alternative is telling an
        operator that an allocation was submitted when it was not.
        """
        if not self._breaker.allow():
            raise SimulatorError.circuit_open(
                f"simulator circuit breaker is open; refusing {method} {path}",
                url=path,
            )

        try:
            payload, _ = await self._send_with_retry(
                method, path, params=params, json_body=json_body
            )
            value = self._parse(key, parser, payload, path)
        except SimulatorError as exc:
            self._record_failure(exc, url=path)
            raise

        self._breaker.record_success()
        self._emit_breaker_state()
        return value

    async def _send_with_retry(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> tuple[Any, bool]:
        """Perform one request with bounded retries.

        Returns ``(payload, stale)``, where ``stale`` is the simulator's own
        ``X-Simulator-Stale`` signal for the 2xx that won. Raises
        ``SimulatorError``.

        Retry policy (CONTRACT.md 5.5): 5xx and transport errors only, with
        exponential backoff. A 4xx is raised on the first response -- it is our
        request that is wrong, and repeating it would burn the timeout budget to
        get the same rejection. A non-transport ``httpx.HTTPError`` is marked
        ``retryable=False`` and so is raised on the spot for the same reason.

        Any 2xx is success. This matters for ``POST /v1/allocations``, which the
        guide documents as 200 and the live simulator answers 201 on an
        idempotency replay.
        """
        url = f"{self._base_url}{path}"
        last_error: SimulatorError | None = None

        for attempt in range(self._max_attempts):
            try:
                async with self._request_slots:
                    response = await self._http().request(
                        method, path, params=params, json=json_body
                    )
            except httpx.TransportError as exc:
                # Timeout, connect, read and protocol failures: the simulator is
                # unreachable or unhealthy. Always retryable.
                last_error = SimulatorError.transport(
                    f"{type(exc).__name__}: {exc}", method=method, url=url
                )
            except httpx.HTTPError as exc:
                # A non-transport HTTPError (a redirect loop, a stream or
                # decode failure) is non-retryable by construction, so it must
                # be raised here. Falling through would spend the whole attempt
                # budget repeating an error that repetition cannot fix.
                raise SimulatorError(
                    f"{type(exc).__name__}: {exc}",
                    kind="transport",
                    method=method,
                    url=url,
                    retryable=False,
                ) from exc
            else:
                if 200 <= response.status_code < 300:
                    try:
                        payload = self._decode(response, method=method, url=url)
                    except SimulatorError as exc:
                        # A 2xx whose body is not JSON is the simulator
                        # misbehaving, not our request being rejected, so it is
                        # retried on the same footing as a 5xx. (A 4xx is not:
                        # see the raise below.)
                        last_error = exc
                    else:
                        return payload, _is_simulator_stale(response)
                else:
                    last_error = SimulatorError.from_body(
                        response.status_code,
                        body_from_response(response),
                        method=method,
                        url=url,
                    )
                    if not last_error.retryable:
                        raise last_error

            if attempt < self._max_attempts - 1:
                delay = self._retry_backoff(attempt)
                logger.warning(
                    "simulator %s %s failed (%s); retrying in %.2fs (attempt %d/%d)",
                    method,
                    path,
                    last_error,
                    delay,
                    attempt + 1,
                    self._max_attempts,
                    extra={
                        "event": "sim.retry",
                        "method": method,
                        "path": path,
                        "attempt": attempt + 1,
                        "max_attempts": self._max_attempts,
                    },
                )
                self._metric(
                    "record_sim_retry", method, path, last_error.kind
                )
                await self._sleep(delay)

        if last_error is None:  # pragma: no cover - the loop always sets it
            raise SimulatorError(
                f"{method} {path}: no attempt was made",
                kind="invalid_request",
                method=method,
                url=url,
            )
        raise last_error

    def _decode(
        self, response: httpx.Response, *, method: str, url: str
    ) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise SimulatorError(
                f"simulator returned a non-JSON body on {method} {url}: {exc}",
                kind="decode",
                status_code=response.status_code,
                method=method,
                url=url,
                retryable=True,
            ) from exc

    def _parse(
        self,
        key: str,
        parser: Callable[[Any], Any],
        payload: Any,
        path: str,
    ) -> Any:
        """Run a model's ``from_api``, converting a shape violation to a typed error.

        A payload that does not match the model is a simulator fault, so it
        counts against the breaker -- just like a 5xx.
        """
        try:
            return parser(payload)
        except ValueError as exc:
            raise SimulatorError(
                f"simulator returned a payload that does not match {key!r}: {exc}",
                kind="invalid_payload",
                status_code=None,
                url=path,
                payload=payload,
                retryable=True,
            ) from exc

    def _counts_against_breaker(self, exc: SimulatorError) -> bool:
        """Which failures mean "the simulator is unhealthy"?

        A 4xx does not. It says our request was wrong, and opening the breaker
        over one bad query string would take the whole platform down while the
        simulator was perfectly healthy.
        """
        if exc.kind in _UNHEALTHY_KINDS:
            return True
        if exc.status_code is not None:
            return exc.status_code >= 500
        return False

    def _record_failure(self, exc: SimulatorError, *, url: str | None = None) -> None:
        countable = self._counts_against_breaker(exc)
        if countable:
            self._breaker.record_failure()
        else:
            # A 4xx is our request being wrong, not the simulator being
            # unhealthy -- so it must not open the breaker. But the simulator
            # *did* answer, which settles any half-open trial: without this the
            # trial would be left in flight and the breaker would sit in
            # HALF_OPEN refusing every request for good. It also resets the
            # consecutive-failure count, because the service is demonstrably up.
            self._breaker.record_success()
        self._emit_breaker_state()

        logger.warning(
            "simulator request failed: %s",
            exc,
            extra={
                "event": "sim.request_failed",
                "kind": exc.kind,
                "status_code": exc.status_code,
                "code": exc.code,
                "url": url or exc.url,
                "counts_against_breaker": countable,
            },
        )
        self._metric("record_sim_failure", exc.kind, exc.status_code, countable)

    def _on_breaker_state_change(self, previous: str, current: str) -> None:
        self._metric(
            "set_breaker", _BREAKER_COMPONENT, current
        )

    def _emit_breaker_state(self) -> None:
        self._metric("set_breaker", _BREAKER_COMPONENT, self._breaker.state.value)

    def _metric(self, name: str, *args: Any, **kwargs: Any) -> None:
        """Call an optional metric hook (A9's ``Metrics``) if it exists.

        The gateway must not fail a request because a metric could not be
        recorded, so a hook that raises is logged and swallowed -- explicitly,
        and with the exception attached, never silently.
        """
        if self._metrics is None:
            return
        hook = getattr(self._metrics, name, None)
        if not callable(hook):
            return
        try:
            hook(*args, **kwargs)
        except Exception:  # noqa: BLE001 - observability must not break traffic
            logger.warning(
                "metrics hook %r raised; continuing", name, exc_info=True
            )

    @staticmethod
    def _retry_backoff(attempt: int) -> float:
        base = min(_RETRY_MAX_DELAY, _RETRY_BASE_DELAY * (2**attempt))
        return base * (1.0 + _RETRY_JITTER * random.random())

    @staticmethod
    def _stream_backoff(attempt: int) -> float:
        base = min(_STREAM_MAX_DELAY, _STREAM_BASE_DELAY * (2**attempt))
        return base * (1.0 + _RETRY_JITTER * random.random())


def _decode_stream_error_body(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw.decode("utf-8", "replace")
