"""Error classification, connection pool and repository mapping.

The exception shapes below are the ones pg8000 actually raised when it was run
through each Toxiproxy fault while this project was built.
"""

import datetime
import time
from decimal import Decimal

import pg8000.native
import pytest

from catalog.config import Settings
from catalog.db import ConnectionPool, PostgresRepository, classify_error
from catalog.errors import (
    DatabaseConnectionError,
    DatabaseQueryError,
    DatabaseTimeout,
)

# -- classification ---------------------------------------------------------------


def _refused() -> Exception:
    try:
        raise ConnectionRefusedError(111, "Connection refused")
    except ConnectionRefusedError as cause:
        try:
            raise pg8000.native.InterfaceError("Can't create a connection") from cause
        except pg8000.native.InterfaceError as exc:
            return exc


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (TimeoutError("The read operation timed out"), DatabaseTimeout),  # latency toxic
        (pg8000.native.InterfaceError("network error"), DatabaseConnectionError),  # dropped
        (_refused(), DatabaseConnectionError),  # proxy disabled
        (ConnectionResetError(104, "reset by peer"), DatabaseConnectionError),  # reset_peer
        (pg8000.native.DatabaseError({"C": "57P01", "M": "terminating"}), DatabaseConnectionError),
        (pg8000.native.DatabaseError({"C": "42P01", "M": "no table"}), DatabaseQueryError),
        (RuntimeError("weird"), DatabaseConnectionError),
    ],
)
def test_driver_errors_are_classified(raised, expected):
    assert isinstance(classify_error(raised), expected)


def test_timeout_wrapped_in_another_error_is_still_a_timeout():
    try:
        raise TimeoutError("timed out")
    except TimeoutError as cause:
        try:
            raise pg8000.native.InterfaceError("wrapped") from cause
        except pg8000.native.InterfaceError as exc:
            assert isinstance(classify_error(exc), DatabaseTimeout)


def test_already_classified_errors_pass_through():
    err = DatabaseTimeout("slow")
    assert classify_error(err) is err


# -- pool ---------------------------------------------------------------------------


class FakeConn:
    created = 0

    def __init__(self, rows=None, error=None):
        FakeConn.created += 1
        self.rows = rows if rows is not None else []
        self.error = error
        self.columns = [{"name": "ok"}]
        self.closed = False
        self.statements = []

    def run(self, sql, **params):
        self.statements.append(sql)
        if self.error:
            raise self.error
        return self.rows

    def close(self):
        self.closed = True


def test_pool_reuses_healthy_connections():
    pool = ConnectionPool(FakeConn, max_idle=2)
    with pool.connection() as first:
        pass
    with pool.connection() as second:
        pass
    assert first is second
    assert pool.idle_count == 1


def test_pool_discards_connections_that_raised():
    pool = ConnectionPool(FakeConn, max_idle=2)
    with pytest.raises(ValueError), pool.connection() as conn:
        raise ValueError("boom")
    assert conn.closed
    assert pool.idle_count == 0


def test_pool_closes_connections_beyond_max_idle():
    pool = ConnectionPool(FakeConn, max_idle=1)
    with pool.connection() as a, pool.connection() as b:
        pass
    assert pool.idle_count == 1
    assert a.closed != b.closed


def test_flush_closes_every_idle_connection():
    pool = ConnectionPool(FakeConn, max_idle=3)
    with pool.connection() as a, pool.connection() as b:
        pass
    assert pool.flush() == 2
    assert a.closed and b.closed and pool.idle_count == 0


# -- repository -----------------------------------------------------------------------

ROW = [7, "Lamp", Decimal("1299.00"), 3, datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)]
PRODUCT_COLUMNS = [{"name": n} for n in ("id", "name", "price", "stock", "created_at")]


def make_repo(conn_factory, seed=True):
    return PostgresRepository(Settings(seed_data=seed), connect=conn_factory)


def test_schema_is_created_once_on_first_connection():
    conns = []

    def factory():
        conn = FakeConn(rows=[[1]])
        conns.append(conn)
        return conn

    repo = make_repo(factory)
    repo.ping()
    repo.ping()
    statements = conns[0].statements
    assert any("CREATE TABLE IF NOT EXISTS products" in s for s in statements)
    assert any("INSERT INTO products" in s for s in statements)
    assert sum("CREATE TABLE" in s for s in statements) == 1


def test_seeding_can_be_disabled():
    conn = FakeConn(rows=[[1]])
    make_repo(lambda: conn, seed=False).ping()
    assert not any("INSERT INTO products" in s for s in conn.statements)


def test_rows_are_mapped_to_json_friendly_products():
    conn = FakeConn(rows=[ROW])
    conn.columns = PRODUCT_COLUMNS
    repo = make_repo(lambda: conn)
    product = repo.get_product(7)
    assert product == {
        "id": 7,
        "name": "Lamp",
        "price": 1299.0,
        "stock": 3,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    assert repo.list_products() == [product]
    assert repo.create_product("Lamp", 1299.0, 3) == product


def test_missing_row_returns_none():
    conn = FakeConn(rows=[])
    conn.columns = PRODUCT_COLUMNS
    assert make_repo(lambda: conn).get_product(1) is None


def test_network_errors_are_translated_and_flush_the_pool():
    healthy = FakeConn(rows=[[1]])
    repo = make_repo(lambda: healthy)
    repo.ping()  # one idle connection in the pool
    healthy.error = TimeoutError("timed out")
    with pytest.raises(DatabaseTimeout):
        repo.ping()
    assert healthy.closed


def test_connect_failure_is_translated():
    def refuse():
        raise _refused()

    with pytest.raises(DatabaseConnectionError):
        make_repo(refuse).ping()


def test_failed_schema_setup_closes_the_connection_and_retries_later():
    conn = FakeConn(error=pg8000.native.DatabaseError({"C": "57P03", "M": "starting up"}))
    repo = make_repo(lambda: conn)
    with pytest.raises(DatabaseConnectionError):
        repo.ping()
    assert conn.closed
    conn.error = None
    conn.closed = False
    conn.rows = [[1]]
    repo.ping()  # schema creation is attempted again and succeeds


def test_close_empties_the_pool():
    conn = FakeConn(rows=[[1]])
    repo = make_repo(lambda: conn)
    repo.ping()
    repo.close()
    assert conn.closed


def test_the_deadline_covers_the_whole_operation():
    """Each round trip may be under the socket timeout; the total still may not be."""

    class SlowConn(FakeConn):
        def run(self, sql, **params):
            if "SELECT 1" in sql:  # schema setup is instant, the query is slow
                time.sleep(1.0)
            return [[1]]

    repo = PostgresRepository(Settings(db_timeout_seconds=0.2), connect=SlowConn)
    started = time.perf_counter()
    with pytest.raises(DatabaseTimeout, match="within 0.2 s"):
        repo.ping()
    assert time.perf_counter() - started < 0.6
    repo.close()
