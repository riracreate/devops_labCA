"""Circuit breaker state machine and retry/backoff behaviour."""

import contextlib
import threading

import pytest

from catalog.resilience import CircuitBreaker, CircuitOpenError, backoff_delay, retry_call

from .fakes import FakeClock


class Boom(Exception):
    pass


class Harmless(Exception):
    pass


def failing():
    raise Boom("down")


def make_breaker(clock: FakeClock, threshold: int = 3, reset: float = 10.0) -> CircuitBreaker:
    return CircuitBreaker(
        "test",
        failure_threshold=threshold,
        reset_timeout=reset,
        failure_exceptions=(Boom,),
        clock=clock,
    )


def trip(breaker: CircuitBreaker, times: int) -> None:
    for _ in range(times):
        with pytest.raises(Boom):
            breaker.call(failing)


# -- circuit breaker ------------------------------------------------------------


def test_breaker_starts_closed_and_passes_results_through():
    breaker = make_breaker(FakeClock())
    assert breaker.state == CircuitBreaker.CLOSED
    assert breaker.call(lambda x: x * 2, 21) == 42


def test_breaker_opens_after_threshold_consecutive_failures():
    breaker = make_breaker(FakeClock(), threshold=3)
    trip(breaker, 2)
    assert breaker.state == CircuitBreaker.CLOSED
    trip(breaker, 1)
    assert breaker.state == CircuitBreaker.OPEN


def test_success_resets_the_consecutive_failure_count():
    breaker = make_breaker(FakeClock(), threshold=3)
    trip(breaker, 2)
    breaker.call(lambda: "ok")
    trip(breaker, 2)
    assert breaker.state == CircuitBreaker.CLOSED


def test_open_breaker_rejects_without_calling_the_dependency():
    clock = FakeClock()
    breaker = make_breaker(clock, threshold=1, reset=10)
    trip(breaker, 1)
    calls = []
    clock.advance(4)
    with pytest.raises(CircuitOpenError) as excinfo:
        breaker.call(lambda: calls.append("called"))
    assert calls == []
    assert excinfo.value.retry_after == pytest.approx(6)
    assert breaker.snapshot()["rejections"] == 1


def test_half_open_trial_success_closes_the_circuit():
    clock = FakeClock()
    breaker = make_breaker(clock, threshold=1, reset=10)
    trip(breaker, 1)
    clock.advance(10)
    assert breaker.call(lambda: "recovered") == "recovered"
    assert breaker.state == CircuitBreaker.CLOSED


def test_half_open_trial_failure_reopens_for_a_full_period():
    clock = FakeClock()
    breaker = make_breaker(clock, threshold=1, reset=10)
    trip(breaker, 1)
    clock.advance(10)
    trip(breaker, 1)  # the trial fails
    assert breaker.state == CircuitBreaker.OPEN
    assert breaker.retry_after() == pytest.approx(10)
    assert breaker.snapshot()["times_opened"] == 2


def test_only_one_trial_call_is_allowed_while_half_open():
    clock = FakeClock()
    breaker = make_breaker(clock, threshold=1, reset=1)
    trip(breaker, 1)
    clock.advance(1)
    inside, release = threading.Event(), threading.Event()

    def slow_trial():
        inside.set()
        release.wait(5)
        return "done"

    worker = threading.Thread(target=breaker.call, args=(slow_trial,))
    worker.start()
    assert inside.wait(5)
    with pytest.raises(CircuitOpenError):
        breaker.call(lambda: "second caller")
    release.set()
    worker.join(5)
    assert breaker.state == CircuitBreaker.CLOSED


def test_errors_outside_failure_exceptions_prove_the_dependency_answered():
    clock = FakeClock()
    breaker = make_breaker(clock, threshold=1, reset=1)
    trip(breaker, 1)
    clock.advance(1)

    def answers_with_an_error():
        raise Harmless("bad request")

    with pytest.raises(Harmless):
        breaker.call(answers_with_an_error)
    assert breaker.state == CircuitBreaker.CLOSED


def test_late_failure_from_an_older_call_does_not_extend_the_open_period():
    clock = FakeClock()
    breaker = make_breaker(clock, threshold=1, reset=10)
    started = threading.Event()
    release = threading.Event()

    def slow_failure():
        started.set()
        release.wait(5)
        raise Boom("late")

    def run_older_call():
        with contextlib.suppress(Boom):
            breaker.call(slow_failure)

    worker = threading.Thread(target=run_older_call)
    worker.start()
    assert started.wait(5)
    trip(breaker, 1)  # opens at t=1000
    clock.advance(5)
    release.set()  # the older call now fails at t=1005
    worker.join(5)
    assert breaker.retry_after() == pytest.approx(5)


def test_base_exceptions_release_the_trial_slot():
    clock = FakeClock()
    breaker = make_breaker(clock, threshold=1, reset=1)
    trip(breaker, 1)
    clock.advance(1)

    def interrupted():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        breaker.call(interrupted)
    assert breaker.call(lambda: "next trial allowed") == "next trial allowed"


def test_snapshot_and_reset():
    breaker = make_breaker(FakeClock(), threshold=1)
    trip(breaker, 1)
    snap = breaker.snapshot()
    assert snap["state"] == "open"
    assert snap["last_error"] == "Boom: down"
    assert snap["failures"] == 1
    breaker.reset()
    assert breaker.state == CircuitBreaker.CLOSED


@pytest.mark.parametrize(("threshold", "reset"), [(0, 1), (1, 0)])
def test_breaker_rejects_invalid_settings(threshold, reset):
    with pytest.raises(ValueError):
        CircuitBreaker("bad", failure_threshold=threshold, reset_timeout=reset)


# -- retry ----------------------------------------------------------------------


def test_retry_succeeds_after_transient_failures():
    attempts, sleeps, seen = [], [], []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise Boom("transient")
        return "ok"

    result = retry_call(
        flaky,
        attempts=3,
        is_retryable=lambda e: isinstance(e, Boom),
        sleep=sleeps.append,
        rng=lambda: 1.0,
        on_retry=lambda attempt, exc, delay: seen.append(attempt),
    )
    assert result == "ok"
    assert len(attempts) == 3
    assert sleeps == [0.05, 0.1]  # exponential: base, 2 x base
    assert seen == [1, 2]


def test_retry_gives_up_after_the_last_attempt():
    calls = []

    def always_failing():
        calls.append(1)
        raise Boom("down")

    with pytest.raises(Boom):
        retry_call(always_failing, attempts=3, is_retryable=lambda e: True, sleep=lambda s: None)
    assert len(calls) == 3


def test_non_retryable_errors_are_raised_immediately():
    calls = []

    def fails_permanently():
        calls.append(1)
        raise Harmless("not transient")

    with pytest.raises(Harmless):
        retry_call(fails_permanently, attempts=5, is_retryable=lambda e: False)
    assert len(calls) == 1


def test_retry_requires_at_least_one_attempt():
    with pytest.raises(ValueError):
        retry_call(lambda: None, attempts=0, is_retryable=lambda e: True)


def test_backoff_is_capped_and_jittered():
    assert backoff_delay(1, base=0.05, cap=0.5, rng=lambda: 1.0) == 0.05
    assert backoff_delay(10, base=0.05, cap=0.5, rng=lambda: 1.0) == 0.5
    assert backoff_delay(3, base=0.05, cap=0.5, rng=lambda: 0.5) == pytest.approx(0.1)
    assert backoff_delay(3, base=0.05, cap=0.5, rng=lambda: 0.0) == 0.0
