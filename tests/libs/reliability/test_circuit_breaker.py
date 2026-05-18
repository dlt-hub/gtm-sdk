from __future__ import annotations

import threading
from typing import Callable

import pytest

from libs.reliability.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    State,
)


class FakeClock:
    """Manually-advanced monotonic clock for deterministic tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _make(
    *,
    clock: FakeClock | None = None,
    transitions: list[tuple[State, State]] | None = None,
    consecutive_failure_threshold: int = 5,
    failure_rate_min_samples: int = 5,
    failure_rate_threshold: float = 0.5,
    failure_rate_window_seconds: float = 30.0,
    open_duration_seconds: float = 60.0,
) -> tuple[CircuitBreaker, FakeClock, list[tuple[State, State]]]:
    clock = clock or FakeClock()
    transitions = transitions if transitions is not None else []

    def record(old: State, new: State) -> None:
        transitions.append((old, new))

    breaker = CircuitBreaker(
        name="test",
        config=CircuitBreakerConfig(
            consecutive_failure_threshold=consecutive_failure_threshold,
            failure_rate_min_samples=failure_rate_min_samples,
            failure_rate_threshold=failure_rate_threshold,
            failure_rate_window_seconds=failure_rate_window_seconds,
            open_duration_seconds=open_duration_seconds,
        ),
        on_transition=record,
        clock=clock,
    )
    return breaker, clock, transitions


def test_starts_closed_and_allows_calls() -> None:
    breaker, _, transitions = _make()
    assert breaker.state == State.CLOSED
    assert breaker.allow_call() is True
    assert transitions == []


def test_opens_after_n_consecutive_failures() -> None:
    breaker, _, transitions = _make(consecutive_failure_threshold=5)
    for _ in range(4):
        breaker.record_failure()
        assert breaker.state == State.CLOSED
    breaker.record_failure()
    assert breaker.state == State.OPEN
    assert transitions == [(State.CLOSED, State.OPEN)]
    assert breaker.allow_call() is False


def test_does_not_open_below_min_samples() -> None:
    breaker, _, _ = _make(
        consecutive_failure_threshold=999,
        failure_rate_min_samples=5,
        failure_rate_threshold=0.5,
    )
    # 4 failures — below min_samples even though 100% failure rate
    for _ in range(4):
        breaker.record_failure()
    assert breaker.state == State.CLOSED


def test_opens_on_failure_rate_with_enough_samples() -> None:
    breaker, _, _ = _make(
        consecutive_failure_threshold=999,
        failure_rate_min_samples=5,
        failure_rate_threshold=0.5,
    )
    # Pattern: S F F F F F — 5 failures out of 6 = 83% > 50%, ≥5 samples
    breaker.record_success()
    for _ in range(5):
        breaker.record_failure()
    assert breaker.state == State.OPEN


def test_success_resets_consecutive_failures() -> None:
    # Disable failure-rate logic to isolate the consecutive-failure reset behavior.
    breaker, _, _ = _make(
        consecutive_failure_threshold=3,
        failure_rate_min_samples=10_000,
    )
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    # consecutive_failures = 2, below threshold
    assert breaker.state == State.CLOSED


def test_half_open_after_open_duration() -> None:
    breaker, clock, transitions = _make(
        consecutive_failure_threshold=2,
        open_duration_seconds=60.0,
    )
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == State.OPEN

    clock.advance(30.0)
    assert breaker.state == State.OPEN  # still within open duration

    clock.advance(31.0)  # total 61s > 60s
    # state property triggers the lazy half-open transition
    assert breaker.state == State.HALF_OPEN
    assert transitions[-1] == (State.OPEN, State.HALF_OPEN)
    assert breaker.allow_call() is True


def test_half_open_success_closes() -> None:
    breaker, clock, transitions = _make(
        consecutive_failure_threshold=2,
        open_duration_seconds=10.0,
    )
    breaker.record_failure()
    breaker.record_failure()
    clock.advance(11.0)
    assert breaker.state == State.HALF_OPEN
    breaker.record_success()
    assert breaker.state == State.CLOSED
    assert (State.HALF_OPEN, State.CLOSED) in transitions


def test_half_open_failure_reopens() -> None:
    breaker, clock, transitions = _make(
        consecutive_failure_threshold=2,
        open_duration_seconds=10.0,
    )
    breaker.record_failure()
    breaker.record_failure()
    clock.advance(11.0)
    assert breaker.state == State.HALF_OPEN
    breaker.record_failure()
    assert breaker.state == State.OPEN
    # closed→open, open→half_open, half_open→open
    assert transitions == [
        (State.CLOSED, State.OPEN),
        (State.OPEN, State.HALF_OPEN),
        (State.HALF_OPEN, State.OPEN),
    ]


def test_rolling_window_prunes_old_entries() -> None:
    """Window entries older than failure_rate_window_seconds must drop off on next append."""
    breaker, clock, _ = _make(
        consecutive_failure_threshold=999,
        failure_rate_min_samples=10_000,  # disable rate trigger so we can observe window mechanics
        failure_rate_window_seconds=30.0,
    )
    for _ in range(4):
        breaker.record_failure()
    # trunk-ignore(pyright/reportPrivateUsage): inspecting deque is the point of this test
    assert len(breaker._window) == 4
    clock.advance(31.0)
    breaker.record_success()
    # Old entries pruned; only the new success remains
    # trunk-ignore(pyright/reportPrivateUsage): inspecting deque is the point of this test
    assert len(breaker._window) == 1


def test_closing_clears_window_and_consecutive_count() -> None:
    breaker, clock, _ = _make(
        consecutive_failure_threshold=2,
        open_duration_seconds=5.0,
    )
    breaker.record_failure()
    breaker.record_failure()
    clock.advance(6.0)
    breaker.record_success()  # half_open -> closed
    assert breaker.state == State.CLOSED
    # After closing, a single new failure should not immediately re-open
    breaker.record_failure()
    assert breaker.state == State.CLOSED


def test_no_transition_callback_emitted_on_repeated_recordings() -> None:
    breaker, _, transitions = _make(consecutive_failure_threshold=3)
    breaker.record_success()
    breaker.record_success()
    # CLOSED -> CLOSED is not a transition; no callback
    assert transitions == []


def test_thread_safety_smoke() -> None:
    """Concurrent record_success / record_failure must not crash or corrupt state."""
    breaker, _, _ = _make(
        consecutive_failure_threshold=999,
        failure_rate_min_samples=999,
    )

    def hammer(fn: Callable[[], None]) -> None:
        for _ in range(200):
            fn()

    threads = [
        threading.Thread(target=hammer, args=(breaker.record_success,)),
        threading.Thread(target=hammer, args=(breaker.record_failure,)),
        threading.Thread(target=hammer, args=(breaker.allow_call,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # No assertion on final state — invariants checked by the lock; this is a smoke test.
    assert breaker.state in (State.CLOSED, State.OPEN, State.HALF_OPEN)


@pytest.mark.parametrize(
    ("threshold", "failures", "expected"),
    [
        (1, 1, State.OPEN),
        (3, 2, State.CLOSED),
        (3, 3, State.OPEN),
    ],
)
def test_threshold_boundaries(
    threshold: int,
    failures: int,
    expected: State,
) -> None:
    breaker, _, _ = _make(consecutive_failure_threshold=threshold)
    for _ in range(failures):
        breaker.record_failure()
    assert breaker.state == expected
