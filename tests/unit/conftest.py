"""Unit-test fixtures: the real service and API wired to in-memory fakes."""

import pytest
from fastapi.testclient import TestClient

from catalog.api import create_app
from catalog.config import Settings
from catalog.service import ProductService

from .fakes import FakeCache, FakeClock, FakeRepository


@pytest.fixture
def settings() -> Settings:
    return Settings(
        cache_fresh_seconds=30,
        cache_stale_seconds=3600,
        breaker_failure_threshold=3,
        breaker_reset_timeout_seconds=10,
        db_max_retries=2,
        log_level="WARNING",
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def repo() -> FakeRepository:
    repository = FakeRepository()
    repository.add("Keyboard", 3499.0, 25)
    repository.add("Mouse", 899.0, 120)
    return repository


@pytest.fixture
def cache() -> FakeCache:
    return FakeCache()


@pytest.fixture
def service(repo, cache, settings, clock) -> ProductService:
    # One fake clock drives cache ages and circuit timing; sleeps are no-ops.
    return ProductService(
        repo, cache, settings, clock=clock, monotonic=clock, sleep=lambda _seconds: None
    )


@pytest.fixture
def client(service) -> TestClient:
    with TestClient(create_app(service=service)) as test_client:
        yield test_client
