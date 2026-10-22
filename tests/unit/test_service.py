"""Degradation policies of ProductService, driven by fakes and a fake clock."""

import pytest

from catalog.errors import (
    CacheUnavailable,
    DatabaseConnectionError,
    DatabaseQueryError,
    DatabaseTimeout,
)
from catalog.service import (
    SOURCE_CACHE,
    SOURCE_DATABASE,
    SOURCE_STALE,
    CacheEntry,
    NotFoundError,
    ServiceUnavailableError,
)

# -- happy path -------------------------------------------------------------------


def test_first_read_hits_database_then_cache(service, repo):
    first = service.get_product(1)
    second = service.get_product(1)
    assert first.source == SOURCE_DATABASE
    assert second.source == SOURCE_CACHE
    assert first.data == second.data == repo.products[1]
    assert repo.calls == 1


def test_fresh_cache_expires_after_the_configured_time(service, repo, clock, settings):
    service.get_product(1)
    clock.advance(settings.cache_fresh_seconds + 1)
    assert service.get_product(1).source == SOURCE_DATABASE
    assert repo.calls == 2


def test_missing_product_raises_not_found_and_is_not_cached(service, cache):
    with pytest.raises(NotFoundError):
        service.get_product(999)
    assert "product:999" not in cache.store


def test_list_is_cached_and_invalidated_by_create(service):
    assert service.list_products().source == SOURCE_DATABASE
    assert service.list_products().source == SOURCE_CACHE
    created = service.create_product("Monitor", 14999.0, 8)
    listing = service.list_products()
    assert listing.source == SOURCE_DATABASE
    assert created in listing.data


# -- Redis faults: never fatal -------------------------------------------------------


def test_cache_outage_falls_back_to_database(service, cache):
    cache.down = CacheUnavailable("connection refused")
    lookup = service.get_product(1)
    assert lookup.source == SOURCE_DATABASE
    assert service.metrics.snapshot()["cache_errors"] == 2  # failed read + failed write-back


def test_cache_breaker_opens_and_skips_redis(service, cache, settings):
    cache.down = CacheUnavailable("timeout")
    for _ in range(settings.breaker_failure_threshold):
        service.get_product(1)
    assert service.cache_breaker.state == "open"
    calls_before = cache.calls
    service.get_product(1)
    assert cache.calls == calls_before  # Redis not even contacted
    assert service.metrics.snapshot()["cache_bypassed"] >= 1


def test_corrupt_cache_entry_is_treated_as_a_miss(service, cache):
    cache.store["product:1"] = "{not json"
    assert service.get_product(1).source == SOURCE_DATABASE
    assert service.metrics.snapshot()["cache_errors"] == 1


def test_write_succeeds_even_if_cache_invalidation_fails(service, cache):
    cache.down = CacheUnavailable("down")
    product = service.create_product("Webcam", 2499.0, 5)
    assert product["name"] == "Webcam"


# -- PostgreSQL faults ---------------------------------------------------------------


def test_dropped_connection_is_retried_for_reads(service, repo):
    repo.fail_next(DatabaseConnectionError("connection reset"))
    lookup = service.get_product(2)
    assert lookup.source == SOURCE_DATABASE
    assert service.metrics.snapshot()["db_retries"] == 1
    assert service.db_breaker.state == "closed"


def test_timeouts_are_not_retried(service, repo):
    repo.fail_next(DatabaseTimeout("slow"), DatabaseTimeout("slow"))
    with pytest.raises(ServiceUnavailableError) as excinfo:
        service.get_product(2)
    assert excinfo.value.dependency == "postgres"
    assert repo.calls == 1
    assert service.metrics.snapshot()["db_retries"] == 0


def test_writes_are_never_retried(service, repo):
    repo.fail_next(DatabaseConnectionError("reset"))
    with pytest.raises(ServiceUnavailableError):
        service.create_product("Duplicate risk", 1.0, 1)
    assert repo.calls == 1
    assert len(repo.products) == 2  # nothing inserted twice


def test_stale_copy_is_served_when_database_is_down(service, repo, clock, settings):
    fresh = service.get_product(1)
    clock.advance(settings.cache_fresh_seconds + 1)
    repo.down = DatabaseConnectionError("refused")
    stale = service.get_product(1)
    assert stale.source == SOURCE_STALE
    assert stale.data == fresh.data
    assert service.metrics.snapshot()["stale_served"] == 1


def test_no_fallback_means_service_unavailable(service, repo):
    repo.down = DatabaseTimeout("slow")
    with pytest.raises(ServiceUnavailableError) as excinfo:
        service.get_product(1)
    assert excinfo.value.retry_after >= 1


def test_database_breaker_opens_then_fails_fast(service, repo, settings):
    repo.down = DatabaseConnectionError("refused")
    for _ in range(settings.breaker_failure_threshold):
        with pytest.raises(ServiceUnavailableError):
            service.get_product(1)
    assert service.db_breaker.state == "open"
    calls_before = repo.calls
    with pytest.raises(ServiceUnavailableError) as excinfo:
        service.get_product(1)
    assert excinfo.value.reason == "circuit breaker open"
    assert repo.calls == calls_before
    assert excinfo.value.retry_after == pytest.approx(settings.breaker_reset_timeout_seconds)


def test_database_recovers_after_reset_timeout(service, repo, clock, settings):
    repo.down = DatabaseConnectionError("refused")
    for _ in range(settings.breaker_failure_threshold):
        with pytest.raises(ServiceUnavailableError):
            service.get_product(1)
    repo.down = None
    clock.advance(settings.breaker_reset_timeout_seconds)
    assert service.get_product(1).source == SOURCE_DATABASE
    assert service.db_breaker.state == "closed"


def test_query_errors_propagate_without_tripping_the_breaker(service, repo, settings):
    for _ in range(settings.breaker_failure_threshold + 1):
        repo.fail_next(DatabaseQueryError("[42P01] relation does not exist"))
        with pytest.raises(DatabaseQueryError):
            service.get_product(1)
    assert service.db_breaker.state == "closed"


# -- health ---------------------------------------------------------------------------


def test_readiness_reports_ready_degraded_and_unavailable(service, repo, cache):
    assert service.readiness()["status"] == "ready"
    cache.down = CacheUnavailable("down")
    report = service.readiness()
    assert report["status"] == "degraded"
    assert report["checks"]["redis"]["ok"] is False
    repo.down = DatabaseTimeout("slow")
    assert service.readiness()["status"] == "unavailable"


def test_readiness_reports_open_circuits_without_probing(service, repo, settings):
    repo.down = DatabaseConnectionError("refused")
    for _ in range(settings.breaker_failure_threshold):
        service.readiness()
    calls_before = repo.calls
    report = service.readiness()
    assert repo.calls == calls_before
    assert "circuit open" in report["checks"]["postgres"]["error"]


def test_status_exposes_breakers_counters_and_safe_config(service):
    status = service.status()
    assert set(status["breakers"]) == {"postgres", "redis"}
    assert "cache_hits" in status["counters"]
    assert "db_password" not in status["config"]


def test_close_releases_resources(service, repo, cache):
    service.close()
    assert repo.closed and cache.closed


def test_cache_entry_round_trip_and_corruption():
    entry = CacheEntry(data={"id": 1}, cached_at=100.0)
    assert CacheEntry.decode(entry.encode()) == entry
    assert entry.age(160.0) == 60.0
    assert entry.age(50.0) == 0.0
    with pytest.raises(ValueError):
        CacheEntry.decode('{"data": 1}')
