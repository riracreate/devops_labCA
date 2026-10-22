"""Degraded (not broken) networks: packets arrive in tiny fragments or slowly.

Protocol code that assumes "one read = one message" breaks here. The data the
client receives must be byte-for-byte what was stored.
"""

import pytest

from .conftest import source

DEGRADATIONS = [
    pytest.param(
        "slicer",
        {"average_size": 16, "size_variation": 8, "delay": 100},
        "replies split into ~16-byte fragments",
        id="fragmented",
    ),
    pytest.param("bandwidth", {"rate": 8}, "links throttled to 8 KB/s", id="throttled"),
]


@pytest.mark.parametrize(("toxic_type", "attributes", "description"), DEGRADATIONS)
def test_degraded_links_do_not_corrupt_data(
    app, toxiproxy, scorecard, toxic_type, attributes, description
):
    """Degraded network on both dependencies"""
    product = app.create_product()
    with (
        toxiproxy.toxic("postgres", toxic_type, **attributes),
        toxiproxy.toxic("redis", toxic_type, **attributes),
    ):
        from_db, db_time = app.get_product(product["id"])
        from_cache, cache_time = app.get_product(product["id"])
    scorecard(
        fault=f"{toxic_type}: {description} (PostgreSQL and Redis)",
        expected="200 from database, then cache; data identical to what was stored",
        observed=(
            f"{from_db.status_code} {source(from_db)} ({db_time:.2f}s), "
            f"{from_cache.status_code} {source(from_cache)} ({cache_time:.2f}s)"
        ),
    )
    assert from_db.status_code == 200 and source(from_db) == "database"
    assert from_cache.status_code == 200 and source(from_cache) == "cache"
    assert from_db.json() == product
    assert from_cache.json() == product
