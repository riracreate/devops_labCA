"""Exceptions shared across layers.

Low-level driver errors (pg8000, redis-py, socket errors) are translated into
this small hierarchy so that the resilience policies can make decisions based on
*what kind* of failure happened rather than which library raised it.
"""


class DependencyError(Exception):
    """A downstream dependency failed or could not be reached."""

    dependency = "unknown"


class DatabaseUnavailable(DependencyError):
    dependency = "postgres"


class DatabaseTimeout(DatabaseUnavailable):
    """PostgreSQL did not answer within the client-side timeout.

    Not retried: if the database is slow, retrying multiplies the wait.
    """


class DatabaseConnectionError(DatabaseUnavailable):
    """The connection was refused, reset or dropped.

    Retried for idempotent reads, because a fresh connection often succeeds
    (for example after a failover dropped every pooled connection).
    """


class DatabaseQueryError(Exception):
    """PostgreSQL answered with an error (bad SQL, constraint violation...).

    The database is reachable, so this must not trip the circuit breaker.
    """


class CacheUnavailable(DependencyError):
    dependency = "redis"
