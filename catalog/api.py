"""HTTP layer: routes, health endpoints and error-to-status mapping.

Run with:  uvicorn catalog.api:create_app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import math
import time
from contextlib import asynccontextmanager
from datetime import datetime
from importlib import resources
from typing import Annotated

from fastapi import FastAPI, Header, Path, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, StringConstraints

from catalog.cache import RedisCache
from catalog.config import Settings
from catalog.db import PostgresRepository
from catalog.errors import IdempotencyConflictError
from catalog.service import NotFoundError, ProductService, ServiceUnavailableError

log = logging.getLogger("catalog")

ProductId = Annotated[int, Path(ge=1, le=2_147_483_647)]


class ProductIn(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    price: float = Field(ge=0, le=1_000_000, examples=[1499.0])
    stock: int = Field(default=0, ge=0, le=1_000_000, examples=[10])


class Product(ProductIn):
    id: int
    created_at: datetime


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def create_app(settings: Settings | None = None, service: ProductService | None = None) -> FastAPI:
    settings = settings or (service.settings if service else Settings.from_env())
    _configure_logging(settings.log_level)
    if service is None:
        service = ProductService(PostgresRepository(settings), RedisCache(settings), settings)
    static = resources.files("catalog").joinpath("static")
    dashboard_html = static.joinpath("dashboard.html").read_text("utf-8")
    shop_html = static.joinpath("shop.html").read_text("utf-8")
    started = time.monotonic()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        log.info(
            "%s %s starting (db %s:%s, redis %s:%s)",
            settings.service_name,
            settings.version,
            settings.db_host,
            settings.db_port,
            settings.redis_host,
            settings.redis_port,
        )
        yield
        service.close()

    app = FastAPI(
        title="Catalog API",
        version=settings.version,
        summary="Product catalog used to demonstrate automated resilience testing.",
        lifespan=lifespan,
    )
    app.state.service = service

    @app.exception_handler(ServiceUnavailableError)
    async def _unavailable(_request: Request, exc: ServiceUnavailableError) -> JSONResponse:
        retry_after = max(1, math.ceil(exc.retry_after))
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": str(retry_after)},
            content={
                "error": "dependency_unavailable",
                "dependency": exc.dependency,
                "detail": exc.reason,
                "retry_after_seconds": retry_after,
            },
        )

    @app.exception_handler(NotFoundError)
    async def _not_found(_request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": "not_found", "detail": str(exc)})

    @app.exception_handler(IdempotencyConflictError)
    async def _idempotency_conflict(
        _request: Request, exc: IdempotencyConflictError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"error": "idempotency_conflict", "detail": str(exc)},
        )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(dashboard_html)

    @app.get("/shop", response_class=HTMLResponse, include_in_schema=False)
    def shop() -> HTMLResponse:
        return HTMLResponse(shop_html)

    @app.get("/health/live", tags=["health"], summary="Liveness: the process is responsive")
    def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready", tags=["health"], summary="Readiness: dependencies are reachable")
    def ready() -> JSONResponse:
        report = service.readiness()
        code = 503 if report["status"] == "unavailable" else 200
        return JSONResponse(report, status_code=code)

    @app.get("/status", tags=["health"], summary="Circuit breakers, counters and settings")
    def status() -> dict:
        body = service.status()
        body["uptime_seconds"] = round(time.monotonic() - started, 1)
        return body

    @app.get("/api/products", response_model=list[Product], tags=["products"])
    def list_products(response: Response):
        lookup = service.list_products()
        response.headers["X-Data-Source"] = lookup.source
        return lookup.data

    @app.get("/api/products/{product_id}", response_model=Product, tags=["products"])
    def get_product(product_id: ProductId, response: Response):
        lookup = service.get_product(product_id)
        response.headers["X-Data-Source"] = lookup.source
        return lookup.data

    @app.post("/api/products", response_model=Product, status_code=201, tags=["products"])
    def create_product(
        payload: ProductIn,
        idempotency_key: Annotated[
            str | None,
            Header(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
        ] = None,
    ):
        return service.create_product(payload.name, payload.price, payload.stock, idempotency_key)

    return app
