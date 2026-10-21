"""Business logic plus the resilience policies that protect it.

Read path (cache-aside with serve-stale-on-error):

    1. Ask Redis. A *fresh* entry is returned immediately      -> source "cache"
    2. Otherwise ask PostgreSQL (timeout + retry + breaker)    -> source "database"
    3. PostgreSQL failed but an older cached copy exists       -> source "stale-cache"
    4. Nothing to fall back on                                  -> 503 + Retry-After

Redis is optional: if it is slow or down the service keeps working without it.
Writes go to PostgreSQL only and are never retried, because a retried INSERT whose
first response was lost would create a duplicate row.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from catalog.config import Settings
from catalog.errors import DatabaseConnectionError, DependencyError
from catalog.metrics import Metrics
from catalog.resilience import CircuitBreaker, CircuitOpenError, retry_call

log = logging.getLogger(__name__)

SOURCE_CACHE = "cache"
SOURCE_DATABASE = "database"
SOURCE_STALE = "stale-cache"


class NotFoundError(Exception):
    pass


class ServiceUnavailableError(Exception):
    """A required dependency is down and there is nothing to fall back on."""

    def __init__(self, dependency: str, reason: str, retry_after: float):
        super().__init__(f"{dependency} unavailable: {reason}")
        self.dependency = dependency
        self.reason = reason
        self.retry_after = retry_after


@dataclass(frozen=True)
class Lookup:
    data: Any
    source: str


@dataclass(frozen=True)
class CacheEntry:
    data: Any
    cached_at: float

    def age(self, now: float) -> float:
        return max(0.0, now - self.cached_at)

    def encode(self) -> str:
        return json.dumps({"cached_at": self.cached_at, "data": self.data}, separators=(",", ":"))

    @classmethod
    def decode(cls, raw: str) -> CacheEntry:
        try:
            payload = json.loads(raw)
            return cls(data=payload["data"], cached_at=float(payload["cached_at"]))
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("corrupt cache entry") from exc


def _is_transient(exc: BaseException) -> bool:
    """Retry dropped/refused connections, but never timeouts (they would only add delay)."""
    return isinstance(exc, DatabaseConnectionError)


class ProductService:
    LIST_KEY = "products:all"

    def __init__(
        self,
        repository: Any,
        cache: Any,
        settings: Settings,
        metrics: Metrics | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.repository = repository
        self.cache = cache
        self.settings = settings
        self.metrics = metrics or Metrics()
        self._clock = clock
        self._sleep = sleep
        breaker_args = {
            "failure_threshold": settings.breaker_failure_threshold,
            "reset_timeout": settings.breaker_reset_timeout_seconds,
            "failure_exceptions": (DependencyError,),
            "clock": monotonic,
        }
        self.db_breaker = CircuitBreaker("postgres", **breaker_args)
        self.cache_breaker = CircuitBreaker("redis", **breaker_args)

    # -- products -------------------------------------------------------------

    def get_product(self, product_id: int) -> Lookup:
        return self._read_through(
            f"product:{product_id}",
            lambda: self.repository.get_product(product_id),
            not_found=f"product {product_id} does not exist",
        )

    def list_products(self) -> Lookup:
        return self._read_through(self.LIST_KEY, self.repository.list_products)

    def create_product(self, name: str, price: float, stock: int) -> dict[str, Any]:
        product = self._through_db_breaker(
            lambda: self.repository.create_product(name, price, stock)
        )
        self._cache_call(self.cache.delete, self.LIST_KEY)
        return product

    # -- health ---------------------------------------------------------------

    def readiness(self) -> dict[str, Any]:
        postgres = self._probe(self.db_breaker, lambda: self._with_retries(self.repository.ping))
        redis = self._probe(self.cache_breaker, self.cache.ping)
        if not postgres["ok"]:
            status = "unavailable"  # cannot serve uncached reads or any write
        elif not redis["ok"]:
            status = "degraded"  # fully functional, just slower
        else:
            status = "ready"
        return {"status": status, "checks": {"postgres": postgres, "redis": redis}}

    def status(self) -> dict[str, Any]:
        return {
            "service": self.settings.service_name,
            "version": self.settings.version,
            "breakers": {
                "postgres": self.db_breaker.snapshot(),
                "redis": self.cache_breaker.snapshot(),
            },
            "counters": self.metrics.snapshot(),
            "config": self.settings.public_view(),
        }

    def close(self) -> None:
        for resource in (self.repository, self.cache):
            close = getattr(resource, "close", None)
            if close is not None:
                close()

    # -- read path ------------------------------------------------------------

    def _read_through(self, key: str, load: Callable[[], Any], not_found: str = "") -> Lookup:
        entry = self._cache_get(key)
        if entry is not None and entry.age(self._clock()) < self.settings.cache_fresh_seconds:
            self.metrics.incr("cache_hits")
            return Lookup(entry.data, SOURCE_CACHE)

        self.metrics.incr("cache_misses")
        try:
            data = self._through_db_breaker(lambda: self._with_retries(load))
        except ServiceUnavailableError:
            if entry is None:
                raise
            self.metrics.incr("stale_served")
            log.warning(
                "PostgreSQL unavailable; serving stale %s (age %.1fs)",
                key,
                entry.age(self._clock()),
            )
            return Lookup(entry.data, SOURCE_STALE)

        if data is None:
            raise NotFoundError(not_found or f"{key} not found")
        self._cache_set(key, data)
        return Lookup(data, SOURCE_DATABASE)

    # -- database policies ----------------------------------------------------

    def _with_retries(self, operation: Callable[[], Any]) -> Any:
        return retry_call(
            operation,
            attempts=1 + self.settings.db_max_retries,
            is_retryable=_is_transient,
            sleep=self._sleep,
            on_retry=self._on_db_retry,
        )

    def _on_db_retry(self, attempt: int, exc: BaseException, delay: float) -> None:
        self.metrics.incr("db_retries")
        log.info("database attempt %d failed (%s); retrying in %.3fs", attempt, exc, delay)

    def _through_db_breaker(self, operation: Callable[[], Any]) -> Any:
        self.metrics.incr("db_calls")
        try:
            return self.db_breaker.call(operation)
        except CircuitOpenError as exc:
            self.metrics.incr("db_rejected")
            raise ServiceUnavailableError(
                "postgres", "circuit breaker open", exc.retry_after
            ) from exc
        except DependencyError as exc:
            self.metrics.incr("db_errors")
            retry_after = self.db_breaker.retry_after() or 1.0
            raise ServiceUnavailableError("postgres", str(exc), retry_after) from exc

    # -- cache policies (never fatal) -----------------------------------------

    def _cache_get(self, key: str) -> CacheEntry | None:
        raw = self._cache_call(self.cache.get, key)
        if raw is None:
            return None
        try:
            return CacheEntry.decode(raw)
        except ValueError:
            self.metrics.incr("cache_errors")
            return None

    def _cache_set(self, key: str, data: Any) -> None:
        entry = CacheEntry(data=data, cached_at=self._clock())
        self._cache_call(self.cache.set, key, entry.encode(), self.settings.cache_stale_seconds)

    def _cache_call(self, fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return self.cache_breaker.call(fn, *args)
        except CircuitOpenError:
            self.metrics.incr("cache_bypassed")
        except DependencyError as exc:
            self.metrics.incr("cache_errors")
            log.info("cache unavailable, continuing without it: %s", exc)
        return None

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _probe(breaker: CircuitBreaker, check: Callable[[], Any]) -> dict[str, Any]:
        started = time.perf_counter()
        error = None
        try:
            breaker.call(check)
        except CircuitOpenError as exc:
            error = f"circuit open, next probe in {exc.retry_after:.1f} s"
        except DependencyError as exc:
            error = str(exc)
        return {
            "ok": error is None,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "breaker": breaker.state,
            "error": error,
        }
