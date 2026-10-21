"""RedisCache: error translation and the fail-fast client configuration."""

import pytest
import redis

from catalog.cache import RedisCache
from catalog.config import Settings
from catalog.errors import CacheUnavailable


class StubRedis:
    def __init__(self, error=None):
        self.error = error
        self.data = {}
        self.closed = False

    def _check(self):
        if self.error:
            raise self.error

    def get(self, key):
        self._check()
        return self.data.get(key)

    def set(self, key, value, ex=None):
        self._check()
        self.data[key] = value

    def delete(self, *keys):
        self._check()
        for key in keys:
            self.data.pop(key, None)

    def ping(self):
        self._check()
        return True

    def close(self):
        self.closed = True


def test_round_trip():
    cache = RedisCache(Settings(), client=StubRedis())
    cache.set("k", "v", ttl_seconds=60)
    assert cache.get("k") == "v"
    cache.delete("k")
    cache.delete()  # no keys: no call
    assert cache.get("k") is None
    cache.ping()


@pytest.mark.parametrize(
    "error",
    [
        redis.exceptions.TimeoutError("Timeout reading from socket"),
        redis.exceptions.ConnectionError("Error 111 connecting. Connection refused."),
        OSError("network unreachable"),
    ],
)
def test_driver_errors_become_cache_unavailable(error):
    cache = RedisCache(Settings(), client=StubRedis(error=error))
    with pytest.raises(CacheUnavailable):
        cache.get("k")


def test_real_client_is_configured_to_fail_fast():
    cache = RedisCache(Settings(redis_timeout_seconds=0.25))
    kwargs = cache._client.connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] == 0.25
    assert kwargs["socket_connect_timeout"] == 0.25
    retry = kwargs["retry"]
    assert retry.get_retries() == 0  # redis-py's default would be 10
    cache.close()


def test_close_is_best_effort():
    class Exploding(StubRedis):
        def close(self):
            raise RuntimeError("already closed")

    RedisCache(Settings(), client=Exploding()).close()
