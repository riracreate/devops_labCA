"""HTTP contract: status codes, headers and payloads, with fake dependencies."""

from catalog.errors import CacheUnavailable, DatabaseConnectionError, DatabaseTimeout


def test_dashboard_is_served_at_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<html" in response.text.lower()


def test_store_page_is_served_and_links_to_the_monitor(client):
    response = client.get("/shop")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'href="/"' in response.text
    assert 'href="/shop"' in client.get("/").text


def test_openapi_docs_are_available(client):
    assert client.get("/docs").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/products/{product_id}" in paths


def test_liveness_never_touches_dependencies(client, repo, cache):
    repo.down = DatabaseTimeout("slow")
    cache.down = CacheUnavailable("down")
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readiness_status_codes(client, repo, cache):
    assert client.get("/health/ready").status_code == 200
    cache.down = CacheUnavailable("down")
    degraded = client.get("/health/ready")
    assert degraded.status_code == 200  # still serving traffic
    assert degraded.json()["status"] == "degraded"
    repo.down = DatabaseTimeout("slow")
    assert client.get("/health/ready").status_code == 503


def test_get_product_reports_where_the_data_came_from(client):
    first = client.get("/api/products/1")
    second = client.get("/api/products/1")
    assert first.status_code == 200
    assert first.headers["X-Data-Source"] == "database"
    assert second.headers["X-Data-Source"] == "cache"
    assert first.json()["name"] == "Keyboard"


def test_unknown_product_is_404(client):
    response = client.get("/api/products/12345")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_invalid_product_id_is_422(client):
    assert client.get("/api/products/0").status_code == 422
    assert client.get("/api/products/abc").status_code == 422


def test_list_products(client):
    response = client.get("/api/products")
    assert response.status_code == 200
    assert [p["name"] for p in response.json()] == ["Keyboard", "Mouse"]
    assert response.headers["X-Data-Source"] == "database"


def test_create_product_trims_and_validates(client):
    created = client.post("/api/products", json={"name": "  Webcam ", "price": 2499, "stock": 4})
    assert created.status_code == 201
    assert created.json()["name"] == "Webcam"
    for invalid in (
        {"name": "", "price": 1},
        {"name": "   ", "price": 1},
        {"name": "x", "price": -1},
        {"name": "x", "price": 1, "stock": -2},
    ):
        assert client.post("/api/products", json=invalid).status_code == 422, invalid


def test_create_product_can_be_retried_without_a_duplicate(client, repo):
    headers = {"Idempotency-Key": "create-webcam-1"}
    payload = {"name": "Webcam", "price": 2499, "stock": 4}
    first = client.post("/api/products", json=payload, headers=headers)
    replay = client.post("/api/products", json=payload, headers=headers)
    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json()
    assert len(repo.products) == 3

    conflict = client.post("/api/products", json={**payload, "stock": 5}, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "idempotency_conflict"
    assert len(repo.products) == 3


def test_create_product_rejects_malformed_idempotency_keys(client):
    payload = {"name": "Webcam", "price": 2499}
    for key in ("", "has spaces", "a" * 129):
        response = client.post("/api/products", json=payload, headers={"Idempotency-Key": key})
        assert response.status_code == 422


def test_database_outage_maps_to_503_with_retry_after(client, repo):
    repo.down = DatabaseConnectionError("refused")
    response = client.get("/api/products/1")
    assert response.status_code == 503
    assert int(response.headers["Retry-After"]) >= 1
    body = response.json()
    assert body["error"] == "dependency_unavailable"
    assert body["dependency"] == "postgres"


def test_stale_data_is_labelled(client, repo, clock, settings):
    client.get("/api/products/1")
    clock.advance(settings.cache_fresh_seconds + 1)
    repo.down = DatabaseConnectionError("refused")
    response = client.get("/api/products/1")
    assert response.status_code == 200
    assert response.headers["X-Data-Source"] == "stale-cache"


def test_status_endpoint(client):
    body = client.get("/status").json()
    assert body["service"] == "catalog-api"
    assert body["breakers"]["postgres"]["state"] == "closed"
    assert body["uptime_seconds"] >= 0
    assert "db_password" not in body["config"]
