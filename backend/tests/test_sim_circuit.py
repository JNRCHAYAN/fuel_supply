"""Circuit breaker tests (CONTRACT.md 5.5).

Every timing assertion uses an injected clock, so the reset window is exercised
without a single ``sleep``.
"""

from __future__ import annotations

import pytest

from app.sim.circuit import CircuitBreaker, CircuitState


class FakeClock:
    """A monotonic stand-in the test drives by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def breaker(clock: FakeClock) -> CircuitBreaker:
    return CircuitBreaker(failure_threshold=3, reset_seconds=30.0, clock=clock)


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "kwargs",
    [{"failure_threshold": 0}, {"failure_threshold": -1}, {"reset_seconds": -1.0}],
)
def test_invalid_construction_is_rejected(kwargs):
    with pytest.raises(ValueError):
        CircuitBreaker(**kwargs)


def test_starts_closed_and_allows(breaker: CircuitBreaker):
    assert breaker.state is CircuitState.CLOSED
    assert breaker.allow() is True
    assert breaker.failure_count == 0


# --------------------------------------------------------------------------- #
# Opening
# --------------------------------------------------------------------------- #


def test_stays_closed_below_the_threshold(breaker: CircuitBreaker):
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED
    assert breaker.allow() is True


def test_opens_at_the_threshold(breaker: CircuitBreaker):
    for _ in range(3):
        assert breaker.allow() is True
        breaker.record_failure()
    assert breaker.state is CircuitState.OPEN


def test_refuses_every_request_while_open(breaker: CircuitBreaker):
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    assert breaker.allow() is False
    assert breaker.allow() is False


def test_success_resets_the_consecutive_counter(breaker: CircuitBreaker):
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    # Without the reset, this third failure would open the breaker.
    breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED
    assert breaker.failure_count == 1


# --------------------------------------------------------------------------- #
# Half-open
# --------------------------------------------------------------------------- #


def test_half_opens_only_after_the_reset_window(breaker: CircuitBreaker, clock: FakeClock):
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state is CircuitState.OPEN

    clock.advance(29.0)
    assert breaker.state is CircuitState.OPEN
    assert breaker.allow() is False

    clock.advance(1.0)  # exactly the window, not a moment more
    assert breaker.allow() is True
    assert breaker.state is CircuitState.HALF_OPEN


def test_half_open_admits_exactly_one_trial(breaker: CircuitBreaker, clock: FakeClock):
    for _ in range(3):
        breaker.record_failure()
    clock.advance(30.0)

    assert breaker.allow() is True, "the first caller gets the trial"
    assert breaker.allow() is False, "and every other caller is held back"
    assert breaker.allow() is False


def test_successful_trial_closes_the_breaker(breaker: CircuitBreaker, clock: FakeClock):
    for _ in range(3):
        breaker.record_failure()
    clock.advance(30.0)

    assert breaker.allow() is True
    breaker.record_success()

    assert breaker.state is CircuitState.CLOSED
    assert breaker.failure_count == 0
    assert breaker.allow() is True
    # A fresh full budget of failures is required to open it again.
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED


def test_failed_trial_reopens_immediately_and_restarts_the_window(
    breaker: CircuitBreaker, clock: FakeClock
):
    for _ in range(3):
        breaker.record_failure()
    clock.advance(30.0)

    assert breaker.allow() is True
    breaker.record_failure()

    assert breaker.state is CircuitState.OPEN
    # The window restarts from the failed trial, so this is not yet due.
    clock.advance(29.0)
    assert breaker.allow() is False
    clock.advance(1.0)
    assert breaker.allow() is True


def test_seconds_until_half_open_is_reported(breaker: CircuitBreaker, clock: FakeClock):
    assert breaker.seconds_until_half_open == 0.0
    for _ in range(3):
        breaker.record_failure()
    assert breaker.seconds_until_half_open == pytest.approx(30.0)
    clock.advance(12.0)
    assert breaker.seconds_until_half_open == pytest.approx(18.0)
    clock.advance(100.0)
    assert breaker.seconds_until_half_open == 0.0


# --------------------------------------------------------------------------- #
# Callback, reset, reporting
# --------------------------------------------------------------------------- #


def test_state_change_callback_reports_both_ends(clock: FakeClock):
    seen: list[tuple[str, str]] = []
    breaker = CircuitBreaker(
        failure_threshold=2,
        reset_seconds=10.0,
        clock=clock,
        on_state_change=lambda prev, cur: seen.append((prev, cur)),
    )
    breaker.record_failure()
    breaker.record_failure()  # -> open
    clock.advance(10.0)
    breaker.allow()  # -> half_open
    breaker.record_success()  # -> closed

    assert seen == [
        ("closed", "open"),
        ("open", "half_open"),
        ("half_open", "closed"),
    ]


def test_a_raising_callback_does_not_break_the_breaker(clock: FakeClock):
    def explode(previous: str, current: str) -> None:
        raise RuntimeError("metrics backend is down")

    breaker = CircuitBreaker(
        failure_threshold=1, reset_seconds=5.0, clock=clock, on_state_change=explode
    )
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN


def test_reset_forces_a_clean_closed_state(breaker: CircuitBreaker):
    for _ in range(3):
        breaker.record_failure()
    breaker.reset()
    assert breaker.state is CircuitState.CLOSED
    assert breaker.failure_count == 0
    assert breaker.opened_at is None
    assert breaker.allow() is True


def test_snapshot_is_json_ready(breaker: CircuitBreaker, clock: FakeClock):
    for _ in range(3):
        breaker.record_failure()
    snap = breaker.snapshot()
    assert snap["state"] == "open"
    assert snap["threshold"] == 3
    assert snap["reset_seconds"] == 30.0
    assert snap["seconds_until_half_open"] == pytest.approx(30.0)


def test_reading_state_promotes_open_to_half_open_once_elapsed(
    breaker: CircuitBreaker, clock: FakeClock
):
    """``/api/v1/status`` must not report "open" once a trial is admissible."""
    for _ in range(3):
        breaker.record_failure()
    clock.advance(30.0)
    assert breaker.state is CircuitState.HALF_OPEN
