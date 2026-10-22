"""In-memory stand-ins for PostgreSQL, Redis and the clock.

Each fake can be told to fail: ``fail_next(exc, ...)`` queues one-off errors and
``down = exc`` makes every call fail until it is set back to ``None``.
"""

from __future__ import annotations

from typing import Any

from catalog.errors import IdempotencyConflictError


class FakeClock:
    def __init__(self, start: float = 1_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Failable:
    def __init__(self) -> None:
        self.down: Exception | None = None
        self._queued: list[Exception] = []
        self.calls = 0

    def fail_next(self, *errors: Exception) -> None:
        self._queued.extend(errors)

    def _maybe_fail(self) -> None:
        self.calls += 1
        if self._queued:
            raise self._queued.pop(0)
        if self.down is not None:
            raise self.down


class FakeRepository(_Failable):
    def __init__(self) -> None:
        super().__init__()
        self.products: dict[int, dict[str, Any]] = {}
        self._next_id = 1
        self._write_keys: dict[str, int] = {}
        self.closed = False

    def add(self, name: str = "Widget", price: float = 10.0, stock: int = 1) -> dict[str, Any]:
        product = {
            "id": self._next_id,
            "name": name,
            "price": price,
            "stock": stock,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        self.products[self._next_id] = product
        self._next_id += 1
        return product

    def ping(self) -> None:
        self._maybe_fail()

    def get_product(self, product_id: int) -> dict[str, Any] | None:
        self._maybe_fail()
        product = self.products.get(product_id)
        return dict(product) if product else None

    def list_products(self) -> list[dict[str, Any]]:
        self._maybe_fail()
        return [dict(p) for p in self.products.values()]

    def create_product(
        self, name: str, price: float, stock: int, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        self._maybe_fail()
        if idempotency_key in self._write_keys:
            existing = self.products[self._write_keys[idempotency_key]]
            if (existing["name"], existing["price"], existing["stock"]) != (name, price, stock):
                raise IdempotencyConflictError("idempotency key was used for a different product")
            return dict(existing)
        product = self.add(name, price, stock)
        if idempotency_key is not None:
            self._write_keys[idempotency_key] = product["id"]
        return product

    def close(self) -> None:
        self.closed = True


class FakeCache(_Failable):
    def __init__(self) -> None:
        super().__init__()
        self.store: dict[str, str] = {}
        self.closed = False

    def get(self, key: str) -> str | None:
        self._maybe_fail()
        return self.store.get(key)

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._maybe_fail()
        self.store[key] = value

    def delete(self, *keys: str) -> None:
        self._maybe_fail()
        for key in keys:
            self.store.pop(key, None)

    def ping(self) -> None:
        self._maybe_fail()

    def close(self) -> None:
        self.closed = True
