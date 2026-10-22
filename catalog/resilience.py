"""Resilience primitives: a circuit breaker and retry with exponential backoff.

Both are written from scratch (instead of using a library) so every line of the
behaviour under test is visible and explainable.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)


class CircuitOpenError(Exception):
    """Raised instead of calling a dependency whose circuit is open."""

    def __init__(self, name: str, retry_after: float):
        super().__init__(f"circuit '{name}' is open; next attempt in {retry_after:.1f}s")
        self.name = name
        self.retry_after = retry_after


class CircuitBreaker:
    """Stops calling a dependency that keeps failing, then probes it for recovery.

    closed     Calls flow normally. Consecutive failures are counted; reaching
               ``failure_threshold`` opens the circuit.
    open       Calls are rejected immediately with ``CircuitOpenError`` (fail fast,
               no waiting on timeouts) until ``reset_timeout`` seconds have passed.
    half_open  One trial call is let through. Success closes the circuit, failure
               opens it again for another ``reset_timeout``.

    Only exceptions listed in ``failure_exceptions`` count as failures. Anything
    else (for example a SQL error) proves the dependency answered, so it counts as
    a success for availability purposes.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        reset_timeout: float = 10.0,
        failure_exceptions: tuple[type[BaseException], ...] = (Exception,),
        clock: Callable[[], float] = time.monotonic,
    ):
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if reset_timeout <= 0:
            raise ValueError("reset_timeout must be > 0")
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self._failure_exceptions = failure_exceptions
        self._clock = clock
        self._lock = threading.Lock()
        self._state = self.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._trial_in_flight = False
        self._last_error: str | None = None
        self._stats = {"successes": 0, "failures": 0, "rejections": 0, "times_opened": 0}

    # -- public API -----------------------------------------------------------

    def call[T](self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        is_trial = self._before_call()
        try:
            result = fn(*args, **kwargs)
        except self._failure_exceptions as exc:
            self._on_failure(exc, is_trial)
            raise
        except Exception:
            self._on_success(is_trial)
            raise
        except BaseException:
            self._release_trial(is_trial)
            raise
        self._on_success(is_trial)
        return result

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def retry_after(self) -> float:
        """Seconds until the next call will be allowed through (0 if closed)."""
        with self._lock:
            return self._retry_after_locked()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "state": self._state,
                "consecutive_failures": self._consecutive_failures,
                "failure_threshold": self.failure_threshold,
                "reset_timeout_seconds": self.reset_timeout,
                "retry_after_seconds": round(self._retry_after_locked(), 2),
                "last_error": self._last_error,
                **self._stats,
            }

    def reset(self) -> None:
        with self._lock:
            self._transition(self.CLOSED)
            self._consecutive_failures = 0
            self._trial_in_flight = False

    # -- state machine (all helpers below expect self._lock to be held) -------

    def _before_call(self) -> bool:
        """Return True if this call is the half-open trial; raise if rejected."""
        with self._lock:
            if self._state == self.CLOSED:
                return False
            if self._state == self.OPEN:
                remaining = self.reset_timeout - (self._clock() - self._opened_at)
                if remaining > 0:
                    self._stats["rejections"] += 1
                    raise CircuitOpenError(self.name, remaining)
                self._transition(self.HALF_OPEN)
            if self._trial_in_flight:
                # Another request is already probing the dependency.
                self._stats["rejections"] += 1
                raise CircuitOpenError(self.name, min(1.0, self.reset_timeout))
            self._trial_in_flight = True
            return True

    def _on_success(self, is_trial: bool) -> None:
        with self._lock:
            self._stats["successes"] += 1
            if is_trial:
                self._trial_in_flight = False
                self._consecutive_failures = 0
                self._transition(self.CLOSED)
            elif self._state == self.CLOSED:
                self._consecutive_failures = 0

    def _on_failure(self, exc: BaseException, is_trial: bool) -> None:
        with self._lock:
            self._stats["failures"] += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            if is_trial:
                self._trial_in_flight = False
                self._open()
            elif self._state == self.CLOSED:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.failure_threshold:
                    self._open()
            # A late failure from a call that started before the circuit opened
            # changes nothing: the circuit is already open.

    def _release_trial(self, is_trial: bool) -> None:
        if is_trial:
            with self._lock:
                self._trial_in_flight = False

    def _open(self) -> None:
        self._opened_at = self._clock()
        self._stats["times_opened"] += 1
        self._transition(self.OPEN)

    def _transition(self, new_state: str) -> None:
        if new_state != self._state:
            log.warning("circuit %s: %s -> %s", self.name, self._state, new_state)
            self._state = new_state

    def _retry_after_locked(self) -> float:
        if self._state != self.OPEN:
            return 0.0
        return max(0.0, self.reset_timeout - (self._clock() - self._opened_at))


def backoff_delay(
    attempt: int, base: float, cap: float, rng: Callable[[], float] = random.random
) -> float:
    """Exponential backoff with "full jitter": uniform in [0, min(cap, base * 2^(n-1))].

    Jitter spreads retries out so that many clients recovering at once do not
    hit the dependency in synchronized waves (the thundering-herd problem).
    """
    return rng() * min(cap, base * (2 ** (attempt - 1)))


def retry_call[T](
    fn: Callable[[], T],
    *,
    attempts: int,
    is_retryable: Callable[[BaseException], bool],
    base_delay: float = 0.05,
    max_delay: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[[], float] = random.random,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Call ``fn`` up to ``attempts`` times, retrying only retryable errors."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    attempt = 1
    while True:
        try:
            return fn()
        except Exception as exc:
            if attempt >= attempts or not is_retryable(exc):
                raise
            delay = backoff_delay(attempt, base_delay, max_delay, rng)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            sleep(delay)
            attempt += 1
