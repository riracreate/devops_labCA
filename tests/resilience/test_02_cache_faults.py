"""Redis faults. Redis is only a cache, so none of these may break the API.

The worst-case cost of a broken cache is two Redis timeouts per request (one
read, one write-back) until the circuit breaker opens and skips Redis entirely.
"""

from .conftest import SLO_DEGRADED_READ, SLO_FAST_FAIL, SLO_MAX_FAILURES_BEFORE_OPEN, source


def test_slow_redis_does_not_slow_down_reads(app, toxiproxy, scorecard):
    """Redis answers 2 s late"""
    product = app.create_product()
    budget = SLO_DEGRADED_READ
    with toxiproxy.toxic("redis", "latency", latency=2000):
        response, elapsed = app.get_product(product["id"])
    scorecard(
        fault="+2000 ms latency on every Redis reply",
        expected=f"200 from database in < {budget:.2f}s",
        observed=f"{response.status_code} from {source(response)} in {elapsed:.2f}s",
    )
    assert response.status_code == 200
    assert source(response) == "database"
    assert response.json() == product
    assert elapsed < budget, "the Redis timeout did not cut the slow call off"


def test_redis_outage_degrades_gracefully(app, toxiproxy, scorecard):
    """Redis is down"""
    product = app.create_product()
    with toxiproxy.disabled("redis"):
        read, read_time = app.get_product(product["id"])
        write, _ = app.timed("POST", "/api/products", json={"name": "during-outage", "price": 1})
        ready, _ = app.timed("GET", "/health/ready")
    readiness = ready.json()
    scorecard(
        fault="Redis proxy disabled (connection refused)",
        expected="reads 200, writes 201, readiness 200 'degraded'",
        observed=(
            f"read {read.status_code} from {source(read)} in {read_time:.2f}s, "
            f"write {write.status_code}, readiness {ready.status_code} '{readiness['status']}'"
        ),
    )
    assert read.status_code == 200 and source(read) == "database"
    assert read_time < SLO_DEGRADED_READ
    assert write.status_code == 201
    assert ready.status_code == 200
    assert readiness["status"] == "degraded"
    assert readiness["checks"]["redis"]["ok"] is False
    assert readiness["checks"]["postgres"]["ok"] is True


def test_redis_blackhole_is_cut_off_by_timeout(app, toxiproxy, scorecard):
    """Redis accepts connections but never replies"""
    product = app.create_product()
    budget = SLO_DEGRADED_READ
    with toxiproxy.toxic("redis", "timeout", timeout=0):  # 0 = hold data forever
        response, elapsed = app.get_product(product["id"])
    scorecard(
        fault="'timeout' toxic: Redis replies are swallowed",
        expected=f"200 from database in < {budget:.2f}s",
        observed=f"{response.status_code} from {source(response)} in {elapsed:.2f}s",
    )
    assert response.status_code == 200
    assert source(response) == "database"
    assert elapsed < budget


def test_redis_breaker_opens_and_cache_is_bypassed(app, toxiproxy, scorecard):
    """Persistently slow Redis trips its circuit"""
    product = app.create_product()
    bypassed_before = app.counter("cache_bypassed")
    with toxiproxy.toxic("redis", "latency", latency=2000):
        # Each read costs up to two Redis timeouts until the breaker opens.
        for _ in range(SLO_MAX_FAILURES_BEFORE_OPEN):
            app.get_product(product["id"])
            if app.breaker_state("redis") == "open":
                break
        state = app.breaker_state("redis")
        timings = []
        for _ in range(3):
            response, elapsed = app.get_product(product["id"])
            assert response.status_code == 200
            timings.append(elapsed)
    bypassed = app.counter("cache_bypassed") - bypassed_before
    slowest = max(timings)
    scorecard(
        fault="+2000 ms latency on Redis, sustained",
        expected=f"circuit opens; later reads skip Redis (< {SLO_FAST_FAIL:.2f}s each)",
        observed=f"circuit {state}; slowest of 3 reads {slowest * 1000:.0f} ms; {bypassed} skips",
    )
    assert state == "open"
    assert slowest < SLO_FAST_FAIL, "reads still waited on Redis after the circuit opened"
    assert bypassed >= 3
