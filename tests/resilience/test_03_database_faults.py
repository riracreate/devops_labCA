"""PostgreSQL faults. The database is required, so the goal is not to hide the
failure but to fail *well*: quickly, honestly (503 + Retry-After), with stale data
where it exists, and without piling up stuck requests.
"""

import time

from .conftest import SLO_FAILURE_RESPONSE, SLO_FAST_FAIL, SLO_MAX_FAILURES_BEFORE_OPEN, source


def test_slow_database_fails_fast_instead_of_hanging(app, toxiproxy, scorecard):
    """PostgreSQL answers 3 s late"""
    product = app.create_product()  # never read, so not cached
    budget = SLO_FAILURE_RESPONSE
    with toxiproxy.toxic("postgres", "latency", latency=3000):
        response, elapsed = app.get_product(product["id"])
    scorecard(
        fault="+3000 ms latency on every PostgreSQL reply",
        expected=f"503 with Retry-After in < {budget:.2f}s (no 3 s hang)",
        observed=(
            f"{response.status_code} in {elapsed:.2f}s, "
            f"Retry-After={response.headers.get('Retry-After')}"
        ),
    )
    assert response.status_code == 503
    assert "Retry-After" in response.headers
    assert response.json()["dependency"] == "postgres"
    assert elapsed < budget, "the database timeout did not cut the slow query off"


def test_slow_round_trips_cannot_add_up_past_the_deadline(app, toxiproxy, scorecard):
    """Every PostgreSQL round trip is a little slow"""
    # 700 ms is under the per-read timeout, but a parameterised query needs three
    # round trips (2.1 s). Only a deadline on the whole operation stops that.
    product = app.create_product()
    budget = SLO_FAILURE_RESPONSE
    with toxiproxy.toxic("postgres", "latency", latency=700):
        response, elapsed = app.get_product(product["id"])
    scorecard(
        fault="+700 ms on every PostgreSQL reply (below the timeout, 3 per query)",
        expected=f"503 in < {budget:.2f}s: the whole query has a deadline",
        observed=f"{response.status_code} in {elapsed:.2f}s",
    )
    assert response.status_code == 503
    assert elapsed < budget, "per-round-trip delays added up past the deadline"


def test_moderate_database_latency_is_tolerated(app, toxiproxy, scorecard):
    """PostgreSQL is slow but within budget"""
    product = app.create_product()
    with toxiproxy.toxic("postgres", "latency", latency=150):
        response, elapsed = app.get_product(product["id"])
    state = app.breaker_state("postgres")
    scorecard(
        fault="+150 ms latency on PostgreSQL",
        expected="200 from database, circuit stays closed (no false alarm)",
        observed=(
            f"{response.status_code} from {source(response)} in {elapsed:.2f}s, circuit {state}"
        ),
    )
    assert response.status_code == 200
    assert source(response) == "database"
    assert state == "closed"


def test_stale_cache_is_served_during_database_outage(app, toxiproxy, cfg, scorecard):
    """PostgreSQL is down, product was read before"""
    product = app.create_product()
    warm, _ = app.get_product(product["id"])
    assert source(warm) == "database"
    time.sleep(cfg.cache_fresh + 0.2)  # the cached copy is now too old to count as fresh
    with toxiproxy.disabled("postgres"):
        response, elapsed = app.get_product(product["id"])
    scorecard(
        fault="PostgreSQL proxy disabled; cached copy is stale",
        expected="200 with the last known data, marked stale-cache",
        observed=f"{response.status_code} from {source(response)} in {elapsed:.2f}s",
    )
    assert response.status_code == 200
    assert source(response) == "stale-cache"
    assert response.json() == warm.json()
    assert elapsed < SLO_FAILURE_RESPONSE


def test_breaker_opens_and_sheds_load_during_database_outage(app, toxiproxy, scorecard):
    """PostgreSQL is down under sustained traffic"""
    products = [app.create_product() for _ in range(SLO_MAX_FAILURES_BEFORE_OPEN + 1)]
    with toxiproxy.disabled("postgres"):
        attempts = 0
        for product in products[:-1]:
            response, _ = app.get_product(product["id"])
            attempts += 1
            assert response.status_code == 503
            if app.breaker_state("postgres") == "open":
                break
        rejected, elapsed = app.get_product(products[-1]["id"])
        ready, _ = app.timed("GET", "/health/ready")
    scorecard(
        fault="PostgreSQL proxy disabled, repeated uncached reads",
        expected=(
            f"circuit opens within {SLO_MAX_FAILURES_BEFORE_OPEN} failures; "
            f"next call 503 in < {SLO_FAST_FAIL}s; readiness 503"
        ),
        observed=(
            f"opened after {attempts}; next call {rejected.status_code} in "
            f"{elapsed * 1000:.0f} ms; readiness {ready.status_code}"
        ),
    )
    assert app.breaker_state("postgres") == "open", "the circuit never opened"
    assert attempts <= SLO_MAX_FAILURES_BEFORE_OPEN
    assert rejected.status_code == 503
    assert rejected.json()["detail"] == "circuit breaker open"
    assert elapsed < SLO_FAST_FAIL
    assert ready.status_code == 503
    assert ready.json()["status"] == "unavailable"


def test_writes_fail_cleanly_during_database_outage(app, toxiproxy, scorecard):
    """Write while PostgreSQL is down"""
    budget = SLO_FAILURE_RESPONSE
    with toxiproxy.disabled("postgres"):
        response, elapsed = app.timed(
            "POST", "/api/products", json={"name": "lost-write", "price": 10, "stock": 1}
        )
    scorecard(
        fault="PostgreSQL proxy disabled during a write",
        expected=f"503 with Retry-After in < {budget:.2f}s (never a 500)",
        observed=f"{response.status_code} in {elapsed:.2f}s",
    )
    assert response.status_code == 503
    assert "Retry-After" in response.headers
    assert elapsed < budget


def test_dropped_connections_are_replaced_transparently(app, toxiproxy, scorecard):
    """All database connections drop at once"""
    product = app.create_product()  # leaves a pooled connection behind
    retries_before = app.counter("db_retries")
    toxiproxy.set_enabled("postgres", False)  # kills every open connection...
    toxiproxy.set_enabled("postgres", True)  # ...and the database is back at once
    response, elapsed = app.get_product(product["id"])
    retries = app.counter("db_retries") - retries_before
    scorecard(
        fault="every open PostgreSQL connection is cut (failover blip)",
        expected="200: dead pooled connection detected, read retried on a new one",
        observed=(
            f"{response.status_code} from {source(response)} after {retries} retry, {elapsed:.2f}s"
        ),
    )
    assert response.status_code == 200
    assert source(response) == "database"
    assert retries >= 1
    assert app.breaker_state("postgres") == "closed"
