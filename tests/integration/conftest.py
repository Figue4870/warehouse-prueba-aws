"""
Configuración de las PRUEBAS DE INTEGRACIÓN.

Estas pruebas hablan con la API REAL (PostgreSQL + Redis reales). Antes de
correrlas, levanta la bodega:

    docker compose -f basic-docker-compose.yml up -d --build
    pytest tests/integration

Variables opcionales:
    API_URL    (por defecto http://localhost:8001)
    REDIS_URL  (por defecto redis://localhost:6380/0) -> para revisar claves y TTL
"""

import os
import uuid

import httpx
import pytest

API_URL = os.getenv("API_URL", "http://localhost:8001")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6380/0")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def api_url():
    return API_URL


@pytest.fixture(scope="session")
def api():
    try:
        httpx.get(f"{API_URL}/health", timeout=3)
    except httpx.HTTPError:
        pytest.skip(f"La API no responde en {API_URL}. Levántala con docker compose antes de estas pruebas.")
    with httpx.Client(base_url=API_URL, timeout=10) as client:
        yield client


@pytest.fixture(scope="session")
def redis_client():
    try:
        import redis

        client = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        client.ping()
        return client
    except Exception:
        pytest.skip(f"No se pudo conectar a Redis en {REDIS_URL}")


@pytest.fixture
def new_product(api):
    """Crea un producto con SKU único (las pruebas no chocan con datos existentes)."""

    def _create(initial_stock: int = 0):
        sku = f"IT-{uuid.uuid4().hex[:10].upper()}"
        response = api.post("/products", json={"sku": sku, "name": f"Integración {sku}", "unit": "UNIDAD"})
        assert response.status_code == 201, response.text
        product = response.json()
        if initial_stock:
            r = api.post("/inventory/movements", json={"product_id": product["id"], "type": "RECEIPT", "quantity": initial_stock})
            assert r.status_code == 201, r.text
        return product

    return _create
