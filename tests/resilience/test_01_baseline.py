"""Baseline: prove the stack works before breaking it.

If these fail, every fault-injection result below would be meaningless.
"""

import uuid

from .conftest import source


def test_stack_is_healthy_without_faults(app, scorecard):
    """Baseline: all dependencies healthy"""
    response, elapsed = app.timed("GET", "/health/ready")
    body = response.json()
    scorecard(
        fault="none",
        expected="readiness 200, both circuits closed",
        observed=f"{response.status_code} {body['status']} in {elapsed:.2f}s",
    )
    assert response.status_code == 200
    assert body["status"] == "ready"
    assert body["checks"]["postgres"]["breaker"] == "closed"
    assert body["checks"]["redis"]["breaker"] == "closed"


def test_reads_go_to_database_then_cache(app, scorecard):
    """Baseline: cache-aside read path"""
    product = app.create_product()
    first, _ = app.get_product(product["id"])
    second, elapsed = app.get_product(product["id"])
    scorecard(
        fault="none",
        expected="1st read from database, 2nd from cache, same data",
        observed=f"{source(first)} then {source(second)} ({elapsed * 1000:.0f} ms)",
    )
    assert first.status_code == second.status_code == 200
    assert source(first) == "database"
    assert source(second) == "cache"
    assert first.json() == second.json() == product


def test_repeated_write_key_returns_the_original_product(app, scorecard):
    """Baseline: repeated write key is idempotent"""
    key = f"resilience-{uuid.uuid4().hex}"
    payload = {"name": "Retry-safe product", "price": 499.0, "stock": 7}
    headers = {"Idempotency-Key": key}
    first = app.http.post("/api/products", json=payload, headers=headers)
    replay = app.http.post("/api/products", json=payload, headers=headers)
    conflict = app.http.post("/api/products", json={**payload, "stock": 8}, headers=headers)
    scorecard(
        fault="same write key sent twice, then reused with different data",
        expected="same product returned twice; changed payload rejected with 409",
        observed=(
            f"ids {first.json().get('id')} and {replay.json().get('id')}; "
            f"conflict {conflict.status_code}"
        ),
    )
    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json()
    assert conflict.status_code == 409
