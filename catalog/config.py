"""Runtime configuration, read from environment variables.

Every resilience setting (timeouts, retries, circuit breaker thresholds, cache
freshness) is configurable, so the same Docker image runs with production values
in deployment and with faster values in the CI resilience suite.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, fields


class ConfigError(ValueError):
    """An environment variable holds an invalid value."""


# Field name -> environment variable name.
ENV_VARS: dict[str, str] = {
    "service_name": "SERVICE_NAME",
    "version": "APP_VERSION",
    "log_level": "LOG_LEVEL",
    "db_host": "DB_HOST",
    "db_port": "DB_PORT",
    "db_name": "DB_NAME",
    "db_user": "DB_USER",
    "db_password": "DB_PASSWORD",
    "db_timeout_seconds": "DB_TIMEOUT_SECONDS",
    "db_max_retries": "DB_MAX_RETRIES",
    "db_pool_size": "DB_POOL_SIZE",
    "seed_data": "SEED_DATA",
    "redis_host": "REDIS_HOST",
    "redis_port": "REDIS_PORT",
    "redis_timeout_seconds": "REDIS_TIMEOUT_SECONDS",
    "cache_fresh_seconds": "CACHE_FRESH_SECONDS",
    "cache_stale_seconds": "CACHE_STALE_SECONDS",
    "breaker_failure_threshold": "CB_FAILURE_THRESHOLD",
    "breaker_reset_timeout_seconds": "CB_RESET_TIMEOUT_SECONDS",
}

_SECRET_FIELDS = {"db_password"}


def _parse_bool(raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"expected true/false, got {raw!r}")


_PARSERS = {"str": str, "int": int, "float": float, "bool": _parse_bool}


@dataclass(frozen=True)
class Settings:
    service_name: str = "catalog-api"
    version: str = "dev"
    log_level: str = "INFO"

    # PostgreSQL: the primary store. db_timeout_seconds is the deadline for one whole
    # operation (getting a connection + running the query), not just one network read.
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "catalog"
    db_user: str = "catalog"
    db_password: str = "catalog"
    db_timeout_seconds: float = 1.0
    db_max_retries: int = 2
    db_pool_size: int = 5
    seed_data: bool = True

    # Redis: an optional cache. It must fail fast, so its timeout is short.
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_timeout_seconds: float = 0.25

    # Cache entries are "fresh" for this long; older copies are kept as a
    # fallback and served (marked stale) only when PostgreSQL is unavailable.
    cache_fresh_seconds: float = 30.0
    cache_stale_seconds: int = 3600

    # Circuit breaker shared policy for both dependencies.
    breaker_failure_threshold: int = 3
    breaker_reset_timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        values: dict[str, object] = {}
        for field in fields(cls):
            var = ENV_VARS[field.name]
            raw = env.get(var)
            if raw is None or raw.strip() == "":
                continue
            try:
                values[field.name] = _PARSERS[str(field.type)](raw.strip())
            except ValueError as exc:
                raise ConfigError(f"{var}: {exc}") from exc
        settings = cls(**values)
        settings.validate()
        return settings

    def validate(self) -> None:
        checks = [
            (self.db_timeout_seconds > 0, "DB_TIMEOUT_SECONDS must be > 0"),
            (self.redis_timeout_seconds > 0, "REDIS_TIMEOUT_SECONDS must be > 0"),
            (self.db_max_retries >= 0, "DB_MAX_RETRIES must be >= 0"),
            (self.db_pool_size >= 1, "DB_POOL_SIZE must be >= 1"),
            (self.breaker_failure_threshold >= 1, "CB_FAILURE_THRESHOLD must be >= 1"),
            (self.breaker_reset_timeout_seconds > 0, "CB_RESET_TIMEOUT_SECONDS must be > 0"),
            (self.cache_fresh_seconds >= 0, "CACHE_FRESH_SECONDS must be >= 0"),
            (
                self.cache_stale_seconds >= self.cache_fresh_seconds,
                "CACHE_STALE_SECONDS must be >= CACHE_FRESH_SECONDS",
            ),
        ]
        for ok, message in checks:
            if not ok:
                raise ConfigError(message)

    def public_view(self) -> dict[str, object]:
        """Settings safe to expose on /status (no secrets)."""
        return {
            field.name: getattr(self, field.name)
            for field in fields(self)
            if field.name not in _SECRET_FIELDS
        }
