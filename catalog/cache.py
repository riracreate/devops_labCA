"""Redis cache client configured to fail fast.

redis-py's defaults are tuned for durability, not latency: a 5 second socket
timeout and 10 automatic retries with exponential backoff. For an *optional*
cache that means one slow Redis node can stall a request for close to a minute.
This wrapper sets a short timeout and disables retries; the circuit breaker in
the service layer decides when to stop trying altogether.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from catalog.config import Settings
from catalog.errors import CacheUnavailable


class RedisCache:
    def __init__(self, settings: Settings, client: Any | None = None):
        self._client = client or redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            socket_timeout=settings.redis_timeout_seconds,
            socket_connect_timeout=settings.redis_timeout_seconds,
            retry=Retry(NoBackoff(), 0),  # override the 10-retry default
            decode_responses=True,
            health_check_interval=0,
        )

    def get(self, key: str) -> str | None:
        return self._call(self._client.get, key)

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._call(self._client.set, key, value, ex=ttl_seconds)

    def delete(self, *keys: str) -> None:
        if keys:
            self._call(self._client.delete, *keys)

    def ping(self) -> None:
        self._call(self._client.ping)

    def close(self) -> None:
        with contextlib.suppress(Exception):  # best effort during shutdown
            self._client.close()

    @staticmethod
    def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (redis.RedisError, OSError) as exc:
            raise CacheUnavailable(f"Redis unavailable ({type(exc).__name__}: {exc})") from exc
