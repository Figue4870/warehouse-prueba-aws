"""
Fase 6 contra la plataforma REAL: transferencia WH-001 -> WH-002 orquestada por la central.

Requiere la central, WH-001 y WH-002 levantadas (.\\manage.ps1 start all).
Variables opcionales: CENTRAL_URL (http://localhost:8090), API_URL_2 (http://localhost:8002)
"""

import os
import time
import uuid

import httpx
import pytest

CENTRAL_URL = os.getenv("CENTRAL_URL", "http://localhost:8090")
API_URL_2 = os.getenv("API_URL_2", "http://localhost:8002")


@pytest.fixture(scope="module")
def platform(api):
    try:
        httpx.get(f"{CENTRAL_URL}/health", timeout=3)
        httpx.get(f"{API_URL_2}/health", timeout=3)
    except httpx.HTTPError:
        pytest.skip("Hacen falta la central y WH-002 (.\\manage.ps1 start all)")
    with httpx.Client(base_url=CENTRAL_URL, timeout=10) as central, httpx.Client(base_url=API_URL_2, timeout=10) as wh2:
        yield api, wh2, central


def wait_until(check, timeout=90.0, what="la Saga"):
    start, last = time.time(), None
    while time.time() - start < timeout:
        try:
            last = check()
            if last:
                return last
        except (httpx.HTTPError, KeyError) as error:
            last = error
        time.sleep(1)
    pytest.fail(f"Se acabó el tiempo esperando {what}: {last}")


def test_transferencia_completa_entre_dos_bodegas(platform):
    wh1, wh2, central = platform
    sku = f"IT-TR-{uuid.uuid4().hex[:8].upper()}"
    product = wh1.post("/products", json={"sku": sku, "name": "Transferencia de integración", "unit": "UNIDAD"}).json()
    assert wh1.post("/inventory/movements", json={"product_id": product["id"], "type": "RECEIPT", "quantity": 10}).status_code == 201
    # La central debe conocer el SKU (para que WH-002 pueda crear el producto, que aún no tiene)
    wait_until(lambda: central.get(f"/global/inventory/{sku}").status_code == 200, what="el SKU en la central")

    response = central.post("/transfers", json={"sku": sku, "source_warehouse_id": "WH-001",
                                                "destination_warehouse_id": "WH-002", "quantity": 4},
                            headers={"Idempotency-Key": f"it-{sku}"})
    assert response.status_code == 202 and response.json()["status"] == "PROCESSING"
    transfer_id = response.json()["transfer_id"]

    done = wait_until(lambda: (t := central.get(f"/transfers/{transfer_id}").json())["state"] in ("COMPLETED", "FAILED",
                      "RESERVATION_RELEASED") and t)
    assert done["state"] == "COMPLETED", done["note"]
    assert wh1.get(f"/inventory/by-sku/{sku}").json()["quantity"] == 6
    assert wh2.get(f"/inventory/by-sku/{sku}").json()["quantity"] == 4    # WH-002 creó el producto al recibirlo
