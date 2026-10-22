"""PostgreSQL access: deadlines, a small connection pool, error classification.

Two layers of timeout protect every database operation:

* a **deadline** on the whole operation (getting a connection + running the
  query), enforced by running it on a worker thread; and
* pg8000's **socket timeout** on every network read and write, as a backstop
  that stops abandoned worker threads.

The socket timeout alone is not enough: a parameterised query takes three
network round trips and opening a connection takes six, so delays that are each
under the timeout can add up to several times the intended limit. This gap was
found by the resilience suite (test_slow_round_trips_cannot_add_up_past_the_deadline).
Server-side settings such as ``statement_timeout`` cannot help either, because
they do not see a slow *network*, which is exactly what Toxiproxy simulates.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections import deque
from collections.abc import Callable, Iterator
from concurrent import futures
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import pg8000.native

from catalog.config import Settings
from catalog.errors import (
    DatabaseConnectionError,
    DatabaseQueryError,
    DatabaseTimeout,
    DependencyError,
    IdempotencyConflictError,
)

log = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS products (
    id         SERIAL PRIMARY KEY,
    name       TEXT          NOT NULL,
    price      NUMERIC(10,2) NOT NULL CHECK (price >= 0),
    stock      INTEGER       NOT NULL DEFAULT 0 CHECK (stock >= 0),
    idempotency_key TEXT UNIQUE,
    created_at TIMESTAMPTZ   NOT NULL DEFAULT now()
)
"""

# Existing installations may have created the table before write keys existed.
MIGRATE_SQL = "ALTER TABLE products ADD COLUMN IF NOT EXISTS idempotency_key TEXT UNIQUE"

SEED_SQL = """
INSERT INTO products (name, price, stock)
SELECT name, price, stock FROM (VALUES
    ('Mechanical keyboard', 3499.00, 25),
    ('Wireless mouse', 899.00, 120),
    ('USB-C hub, 7 ports', 1999.00, 40),
    ('27-inch monitor', 14999.00, 8),
    ('Noise-cancelling headphones', 7499.00, 15),
    ('Aluminium laptop stand', 1299.00, 60)
) AS seed(name, price, stock)
WHERE NOT EXISTS (SELECT 1 FROM products)
"""

_COLUMNS = "id, name, price, stock, created_at"

# SQLSTATE classes that mean "the connection is the problem", not the query:
# 08 = connection exception, 57P = operator intervention (shutdown, starting up),
# 53300 = too many connections.
_CONNECTION_SQLSTATES = ("08", "57P", "53300")


def classify_error(exc: BaseException) -> Exception:
    """Translate a driver/socket exception into the catalog error hierarchy.

    The mapping below was derived by running pg8000 through every Toxiproxy fault:
    timeouts surface as a bare ``TimeoutError``; refused, reset and dropped
    connections surface as ``InterfaceError`` or ``ConnectionError``.
    """
    if isinstance(exc, (DependencyError, DatabaseQueryError)):
        return exc
    if isinstance(exc, TimeoutError) or isinstance(exc.__cause__, TimeoutError):
        return DatabaseTimeout(f"PostgreSQL did not respond in time ({exc})")
    if isinstance(exc, pg8000.native.DatabaseError):
        details = exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {}
        code = str(details.get("C", ""))
        message = details.get("M", str(exc))
        if code.startswith(_CONNECTION_SQLSTATES):
            return DatabaseConnectionError(f"PostgreSQL unavailable [{code}]: {message}")
        return DatabaseQueryError(f"[{code}] {message}")
    if isinstance(exc, (pg8000.native.InterfaceError, OSError)):
        return DatabaseConnectionError(f"PostgreSQL connection failed ({exc})")
    return DatabaseConnectionError(f"unexpected database error ({type(exc).__name__}: {exc})")


def _close_quietly(conn: Any) -> None:
    with contextlib.suppress(Exception):  # the connection is being thrown away anyway
        conn.close()


class ConnectionPool:
    """A tiny thread-safe LIFO pool.

    A connection that raised an error is discarded, never returned to the pool,
    so a broken socket cannot poison later requests.
    """

    def __init__(self, factory: Callable[[], Any], max_idle: int):
        self._factory = factory
        self._max_idle = max_idle
        self._idle: deque[Any] = deque()
        self._lock = threading.Lock()

    @contextmanager
    def connection(self) -> Iterator[Any]:
        conn = self._acquire()
        try:
            yield conn
        except BaseException:
            _close_quietly(conn)
            raise
        self._release(conn)

    def flush(self) -> int:
        """Close every idle connection (they likely share the fate of the one that failed)."""
        with self._lock:
            idle, self._idle = list(self._idle), deque()
        for conn in idle:
            _close_quietly(conn)
        return len(idle)

    close = flush

    @property
    def idle_count(self) -> int:
        with self._lock:
            return len(self._idle)

    def _acquire(self) -> Any:
        with self._lock:
            if self._idle:
                return self._idle.pop()
        return self._factory()

    def _release(self, conn: Any) -> None:
        with self._lock:
            if len(self._idle) < self._max_idle:
                self._idle.append(conn)
                return
        _close_quietly(conn)


def _to_product(row: dict[str, Any]) -> dict[str, Any]:
    created_at = row["created_at"]
    return {
        "id": row["id"],
        "name": row["name"],
        "price": float(row["price"]),
        "stock": row["stock"],
        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
    }


class PostgresRepository:
    """Product persistence. Raises only catalog errors, never driver errors."""

    def __init__(self, settings: Settings, connect: Callable[[], Any] | None = None):
        self._settings = settings
        self._connect = connect or self._open_connection
        self._pool = ConnectionPool(self._new_connection, max_idle=settings.db_pool_size)
        self._workers = futures.ThreadPoolExecutor(
            max_workers=max(2, 2 * settings.db_pool_size), thread_name_prefix="db"
        )
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    # -- queries --------------------------------------------------------------

    def ping(self) -> None:
        self._query("SELECT 1 AS ok")

    def get_product(self, product_id: int) -> dict[str, Any] | None:
        rows = self._query(f"SELECT {_COLUMNS} FROM products WHERE id = :id", id=product_id)
        return _to_product(rows[0]) if rows else None

    def list_products(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._query(f"SELECT {_COLUMNS} FROM products ORDER BY id LIMIT :limit", limit=limit)
        return [_to_product(row) for row in rows]

    def create_product(
        self, name: str, price: float, stock: int, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        rows = self._query(
            "INSERT INTO products (name, price, stock, idempotency_key) "
            "VALUES (:name, :price, :stock, :idempotency_key) "
            "ON CONFLICT (idempotency_key) DO UPDATE "
            "SET idempotency_key = EXCLUDED.idempotency_key "
            "WHERE products.name = EXCLUDED.name "
            "AND products.price = EXCLUDED.price "
            "AND products.stock = EXCLUDED.stock "
            f"RETURNING {_COLUMNS}",
            name=name,
            price=Decimal(str(price)),
            stock=stock,
            idempotency_key=idempotency_key,
        )
        if not rows:
            raise IdempotencyConflictError("idempotency key was used for a different product")
        return _to_product(rows[0])

    def close(self) -> None:
        self._workers.shutdown(wait=False, cancel_futures=True)
        self._pool.close()

    # -- plumbing -------------------------------------------------------------

    def _query(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        """Run a query, giving up once the deadline for the whole operation passes.

        The worker thread keeps going after the caller gives up, until it finishes
        or its socket timeout fires; a connection that fails is discarded.
        """
        deadline = self._settings.db_timeout_seconds
        future = self._workers.submit(self._query_now, sql, params)
        try:
            return future.result(timeout=deadline)
        except futures.TimeoutError:
            future.cancel()  # never started (all workers busy): do not run it later
            raise DatabaseTimeout(f"PostgreSQL did not answer within {deadline:g} s") from None

    def _query_now(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            with self._pool.connection() as conn:
                rows = conn.run(sql, **params)
                columns = [column["name"] for column in (conn.columns or [])]
        except Exception as exc:
            error = classify_error(exc)
            if isinstance(error, (DatabaseTimeout, DatabaseConnectionError)):
                dropped = self._pool.flush()
                log.info("database error (%s); dropped %d idle connections", error, dropped)
            if error is exc:
                raise
            raise error from exc
        return [dict(zip(columns, row, strict=True)) for row in rows or []]

    def _open_connection(self) -> Any:
        s = self._settings
        return pg8000.native.Connection(
            user=s.db_user,
            password=s.db_password,
            host=s.db_host,
            port=s.db_port,
            database=s.db_name,
            timeout=s.db_timeout_seconds,  # applies to connect AND every read/write
            application_name=s.service_name,
        )

    def _new_connection(self) -> Any:
        conn = self._connect()
        try:
            self._ensure_schema(conn)
        except BaseException:
            _close_quietly(conn)
            raise
        return conn

    def _ensure_schema(self, conn: Any) -> None:
        """Create the table on first contact, so the app can start before the database."""
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            conn.run(SCHEMA_SQL)
            conn.run(MIGRATE_SQL)
            if self._settings.seed_data:
                conn.run(SEED_SQL)
            self._schema_ready = True
            log.info("database schema ready")
