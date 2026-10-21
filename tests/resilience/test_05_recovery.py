"""Self-healing: once a dependency comes back, the service must recover on its
own (no restart, no human). The measured time is the service's recovery time
for that fault, and it must be within the recovery SLO.
"""

import time

from .conftest import SLO_MAX_FAILURES_BEFORE_OPEN, SLO_RECOVERY, source

POLL_SECONDS = 0.1


def _poll(fn, deadline: float):
    """Call fn every POLL_SECONDS until it returns a truthy value or time runs out."""
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(POLL_SECONDS)
    return None


def test_service_recovers_after_database_outage(app, toxiproxy, scorecard):
    """Recovery after a PostgreSQL outage"""
    probe = app.create_product()
    victims = [app.create_product() for _ in range(SLO_MAX_FAILURES_BEFORE_OPEN)]
    with toxiproxy.disabled("postgres"):
        for product in victims:  # uncached reads, so each one needs the database
            app.get_product(product["id"])
            if app.breaker_state("postgres") == "open":
                break
        assert app.breaker_state("postgres") == "open"

    started = time.monotonic()  # the database is reachable again from here on
    limit = SLO_RECOVERY

    def served() -> bool:
        response, _ = app.get_product(probe["id"])
        return response.status_code == 200

    recovered = _poll(served, started + limit)
    recovery = time.monotonic() - started
    state = app.breaker_state("postgres")
    scorecard(
        fault="PostgreSQL down long enough to open the circuit, then restored",
        expected=f"automatic recovery in < {limit:.1f}s, circuit closed again",
        observed=f"recovered in {recovery:.2f}s, circuit {state}" if recovered else "no recovery",
    )
    assert recovered, f"service did not recover within {limit:.1f}s"
    assert state == "closed"


def test_cache_is_used_again_after_redis_recovers(app, toxiproxy, scorecard):
    """Recovery after a Redis outage"""
    product = app.create_product()
    with toxiproxy.disabled("redis"):
        for _ in range(SLO_MAX_FAILURES_BEFORE_OPEN):
            response, _ = app.get_product(product["id"])
            assert response.status_code == 200
            if app.breaker_state("redis") == "open":
                break
        assert app.breaker_state("redis") == "open"

    started = time.monotonic()
    limit = SLO_RECOVERY

    def cache_hit() -> bool:
        response, _ = app.get_product(product["id"])
        return response.status_code == 200 and source(response) == "cache"

    recovered = _poll(cache_hit, started + limit)
    recovery = time.monotonic() - started
    scorecard(
        fault="Redis down long enough to open the circuit, then restored",
        expected=f"cache hits resume in < {limit:.1f}s",
        observed=f"cache hits resumed after {recovery:.2f}s" if recovered else "no recovery",
    )
    assert recovered, f"cache was not used again within {limit:.1f}s"
    assert app.breaker_state("redis") == "closed"
