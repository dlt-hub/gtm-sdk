from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class State(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class CircuitBreakerConfig:
    consecutive_failure_threshold: int = 5
    failure_rate_window_seconds: float = 30.0
    failure_rate_threshold: float = 0.5
    failure_rate_min_samples: int = 5
    open_duration_seconds: float = 60.0


class CircuitBreaker:
    """Thread-safe circuit breaker with rolling-window failure tracking.

    Opens when either:
    - ``consecutive_failure_threshold`` consecutive failures are recorded, or
    - More than ``failure_rate_threshold`` of the last
      ``failure_rate_window_seconds`` of samples failed (requires at least
      ``failure_rate_min_samples`` samples in the window — guards against
      opening on a single failure from a cold container).

    Auto-transitions to ``half_open`` after ``open_duration_seconds``. While
    half-open, a single success closes the breaker and a single failure
    re-opens it.
    """

    def __init__(
        self,
        name: str,
        config: CircuitBreakerConfig | None = None,
        on_transition: Callable[[State, State], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._name = name
        self._cfg = config or CircuitBreakerConfig()
        self._on_transition = on_transition
        self._clock = clock
        self._lock = threading.Lock()
        self._state = State.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._window: deque[tuple[float, bool]] = deque()

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> State:
        with self._lock:
            self._maybe_half_open_locked()
            return self._state

    def allow_call(self) -> bool:
        with self._lock:
            self._maybe_half_open_locked()
            return self._state in (State.CLOSED, State.HALF_OPEN)

    def record_success(self) -> None:
        with self._lock:
            self._maybe_half_open_locked()
            self._consecutive_failures = 0
            self._append_locked(success=True)
            if self._state == State.HALF_OPEN:
                self._transition_locked(State.CLOSED)

    def record_failure(self) -> None:
        with self._lock:
            self._maybe_half_open_locked()
            self._consecutive_failures += 1
            self._append_locked(success=False)
            if self._state == State.HALF_OPEN:
                self._transition_locked(State.OPEN)
                return
            if self._state == State.CLOSED and self._should_open_locked():
                self._transition_locked(State.OPEN)

    def _maybe_half_open_locked(self) -> None:
        if self._state != State.OPEN or self._opened_at is None:
            return
        if self._clock() - self._opened_at >= self._cfg.open_duration_seconds:
            self._transition_locked(State.HALF_OPEN)

    def _should_open_locked(self) -> bool:
        if self._consecutive_failures >= self._cfg.consecutive_failure_threshold:
            return True
        if len(self._window) >= self._cfg.failure_rate_min_samples:
            failures = sum(1 for _, ok in self._window if not ok)
            if failures / len(self._window) > self._cfg.failure_rate_threshold:
                return True
        return False

    def _append_locked(self, *, success: bool) -> None:
        now = self._clock()
        self._window.append((now, success))
        cutoff = now - self._cfg.failure_rate_window_seconds
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()

    def _transition_locked(self, new_state: State) -> None:
        if new_state == self._state:
            return
        old = self._state
        self._state = new_state
        self._opened_at = self._clock() if new_state == State.OPEN else None
        if new_state == State.CLOSED:
            self._window.clear()
            self._consecutive_failures = 0
        if self._on_transition is not None:
            self._on_transition(old, new_state)
