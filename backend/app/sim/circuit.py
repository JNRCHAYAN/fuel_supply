"""Circuit breaker for the simulator gateway (CONTRACT.md 5.5, brief section 11).

Three states::

    CLOSED  --(N consecutive unhealthy failures)-->  OPEN
    OPEN    --(reset_seconds elapsed)------------->  HALF_OPEN
    HALF_OPEN --(trial succeeds)------------------>  CLOSED
    HALF_OPEN --(trial fails)--------------------->  OPEN   (window restarts)

Exactly one trial is admitted while half-open; every other caller is refused
until that trial resolves. Without that, a recovering simulator would be hit by
the full concurrency of the process the instant the window elapsed.

The clock is injectable so the reset window can be tested without sleeping.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from app.sim.models import CircuitState

__all__ = ["CircuitBreaker", "CircuitState"]

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Consecutive-failure breaker with a half-open trial.

    Not thread-safe by design of its use: the gateway is asyncio-only and every
    method here is synchronous, so no ``await`` can interleave inside one.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        on_state_change: Callable[[str, str], None] | None = None,
        name: str = "simulator",
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if reset_seconds < 0:
            raise ValueError("reset_seconds must not be negative")
        self._threshold = int(failure_threshold)
        self._reset_seconds = float(reset_seconds)
        self._clock = clock
        self._on_state_change = on_state_change
        self._name = name
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at: float | None = None
        self._trial_in_flight = False

    # -- introspection ---------------------------------------------------- #

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> CircuitState:
        # Reading the state is allowed to *advance* time: if the window has
        # elapsed, the next caller is entitled to a trial, and reporting "open"
        # while admitting requests would make /api/v1/status lie.
        if self._state is CircuitState.OPEN and self._window_elapsed():
            self._transition(CircuitState.HALF_OPEN)
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failures

    @property
    def threshold(self) -> int:
        return self._threshold

    @property
    def reset_seconds(self) -> float:
        return self._reset_seconds

    @property
    def opened_at(self) -> float | None:
        return self._opened_at

    @property
    def seconds_until_half_open(self) -> float:
        """Reporting only -- used by ``/api/v1/status``."""
        if self._state is not CircuitState.OPEN or self._opened_at is None:
            return 0.0
        return max(0.0, self._reset_seconds - (self._clock() - self._opened_at))

    # -- gate ------------------------------------------------------------- #

    def allow(self) -> bool:
        """May a request be attempted right now?"""
        state = self.state  # may promote OPEN -> HALF_OPEN
        if state is CircuitState.CLOSED:
            return True
        if state is CircuitState.HALF_OPEN:
            if self._trial_in_flight:
                return False
            self._trial_in_flight = True
            return True
        return False  # OPEN and still inside the reset window

    # -- outcomes --------------------------------------------------------- #

    def record_success(self) -> None:
        self._failures = 0
        self._trial_in_flight = False
        if self._state is not CircuitState.CLOSED:
            self._transition(CircuitState.CLOSED)

    def record_failure(self) -> None:
        """Record one *unhealthy* failure.

        Callers are expected to have filtered out 4xx first: a 404 or a 422 says
        our request was wrong, not that the simulator is down, and tripping the
        breaker on it would take the whole platform down over one bad query
        string. :meth:`SimulatorClient._counts_against_breaker` owns that rule.
        """
        self._trial_in_flight = False

        if self._state is CircuitState.HALF_OPEN:
            # The trial failed: straight back to OPEN, window restarts.
            self._failures = self._threshold
            self._open()
            return

        if self._state is CircuitState.OPEN:
            # Should not normally happen (allow() gates callers), but if it does
            # the honest reading is "still broken": keep the window running
            # from now rather than letting it lapse while failures continue.
            self._opened_at = self._clock()
            return

        self._failures += 1
        if self._failures >= self._threshold:
            self._open()

    def reset(self) -> None:
        """Force back to a clean CLOSED state. For tests and admin recovery."""
        self._failures = 0
        self._opened_at = None
        self._trial_in_flight = False
        self._transition(CircuitState.CLOSED)

    # -- internals -------------------------------------------------------- #

    def _window_elapsed(self) -> bool:
        if self._opened_at is None:
            return True
        return (self._clock() - self._opened_at) >= self._reset_seconds

    def _open(self) -> None:
        self._opened_at = self._clock()
        self._trial_in_flight = False
        self._transition(CircuitState.OPEN)

    def _transition(self, new_state: CircuitState) -> None:
        previous = self._state
        if previous is new_state:
            return
        self._state = new_state
        logger.warning(
            "circuit breaker %s: %s -> %s",
            self._name,
            previous.value,
            new_state.value,
            extra={
                "event": "sim.circuit_state",
                "component": self._name,
                "breaker_previous": previous.value,
                "breaker_state": new_state.value,
                "failures": self._failures,
            },
        )
        if self._on_state_change is not None:
            try:
                self._on_state_change(previous.value, new_state.value)
            except Exception:  # noqa: BLE001 - a metrics hook must never break the breaker
                logger.warning(
                    "circuit breaker %s: on_state_change hook raised",
                    self._name,
                    exc_info=True,
                )

    def snapshot(self) -> dict[str, Any]:
        """A JSON-able view for ``/api/v1/status``."""
        return {
            "state": self.state.value,
            "failures": self._failures,
            "threshold": self._threshold,
            "reset_seconds": self._reset_seconds,
            "seconds_until_half_open": round(self.seconds_until_half_open, 3),
        }
