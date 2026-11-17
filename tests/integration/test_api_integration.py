"""Pruebas de integración: API + PostgreSQL + Redis reales."""

from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest


def test_health_con_postgres_y_redis(api):
    health = api.get("/health").json()
    assert health["status"] == "ok"
    assert health["database"] == "ok"
    assert health["redis"] in ("ok", "disabled")


def test_flujo_completo_de_inventario(api, new_product):
    product = new_product(initial_stock=10)
    pid = product["id"]

    r = api.post("/inventory/movements", json={"product_id": pid, "type": "SHIPMENT", "quantity": 4, "reference": "FAC-IT"})
    assert r.status_code == 201 and r.json()["quantity_after"] == 6

    rejected = api.post("/inventory/movements", json={"product_id": pid, "type": "SHIPMENT", "quantity": 15})
    assert rejected.status_code == 409

    assert api.get(f"/inventory/{pid}").json()["quantity"] == 6
    movements = api.get(f"/inventory/{pid}/movements").json()
    assert [m["type"] for m in movements] == ["SHIPMENT", "RECEIPT"]


def test_salidas_concurrentes_nunca_dejan_negativo(api, api_url, new_product):
    """
    Stock = 10. Lanzamos 25 salidas de 1 unidad AL MISMO TIEMPO.
    Gracias a SELECT ... FOR UPDATE, exactamente 10 deben pasar y 15 fallar.
    Sin el bloqueo podrían "venderse" más de 10 unidades.
    """
    product = new_product(initial_stock=10)
    pid = product["id"]

    def ship(_):
        with httpx.Client(base_url=api_url, timeout=20) as client:
            return client.post("/inventory/movements", json={"product_id": pid, "type": "SHIPMENT", "quantity": 1}).status_code

    with ThreadPoolExecutor(max_workers=25) as pool:
        codes = list(pool.map(ship, range(25)))

    assert codes.count(201) == 10
    assert codes.count(409) == 15
    assert api.get(f"/inventory/{pid}").json()["quantity"] == 0
    shipments = [m for m in api.get(f"/inventory/{pid}/movements").json() if m["type"] == "SHIPMENT"]
    assert len(shipments) == 10
    # Solo una alerta de agotado (cuando pasó de 1 a 0)
    assert len(api.get("/alerts", params={"type": "OUT_OF_STOCK", "product_id": pid}).json()) == 1


def test_cache_aside_miss_hit_e_invalidacion(api, new_product):
    product = new_product(initial_stock=100)
    pid = product["id"]

    first = api.get(f"/inventory/{pid}")
    second = api.get(f"/inventory/{pid}")
    if first.headers["X-Cache"] == "BYPASS":
        pytest.skip("El caché está apagado (CACHE_ENABLED=false) o Redis no está disponible")
    assert first.headers["X-Cache"] == "MISS"
    assert second.headers["X-Cache"] == "HIT"

    api.post("/inventory/movements", json={"product_id": pid, "type": "SHIPMENT", "quantity": 10})
    after = api.get(f"/inventory/{pid}")
    assert after.headers["X-Cache"] == "MISS"
    assert after.json()["quantity"] == 90  # nunca el valor viejo (100)


def test_clave_y_ttl_en_redis(api, new_product, redis_client):
    product = new_product()
    api.get(f"/inventory/{product['id']}")
    warehouse_id = api.get("/health").json()["warehouse_id"]
    key = f"warehouse:{warehouse_id}:inventory:{product['id']}"
    assert redis_client.exists(key)
    assert 0 < redis_client.ttl(key) <= 30
