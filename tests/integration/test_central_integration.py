"""
Fase 7 contra la plataforma REAL: bodega WH-001 + central, cada una en su proyecto de Docker.

Requiere:
    docker network create warehouse_platform
    ./manage_warehouse.sh start central        (o .\\manage.ps1 start central)
    docker compose -f basic-docker-compose.yml up -d --build

Variable opcional: CENTRAL_URL (por defecto http://localhost:8090)
"""

import os
import time

import httpx
import pytest

CENTRAL_URL = os.getenv("CENTRAL_URL", "http://localhost:8090")


@pytest.fixture(scope="module")
def central():
    try:
        httpx.get(f"{CENTRAL_URL}/health", timeout=3)
    except httpx.HTTPError:
        pytest.skip(f"La central no responde en {CENTRAL_URL}. Levántala con ./manage_warehouse.sh start central.")
    with httpx.Client(base_url=CENTRAL_URL, timeout=10) as client:
        yield client


def wait_until(check, timeout=60.0, every=1.0, what="la central"):
    start, last = time.time(), None
    while time.time() - start < timeout:
        try:
            last = check()
            if last:
                return last
        except (httpx.HTTPError, KeyError, ValueError) as error:
            last = error
        time.sleep(every)
    pytest.fail(f"Se acabó el tiempo esperando a {what}: {last}")


def test_la_central_esta_sana(central):
    body = central.get("/health").json()
    assert body["status"] == "ok" and body["database"] == "ok"


def test_un_movimiento_en_la_bodega_llega_a_la_central(central, api, new_product):
    product = new_product(initial_stock=12)
    api.post("/inventory/movements", json={"product_id": product["id"], "type": "SHIPMENT", "quantity": 2})

    data = wait_until(lambda: (d := central.get(f"/global/inventory/{product['sku']}").json()).get("total_quantity") == 10 and d,
                      what="que el evento llegue al modelo de lectura")
    wh = next(w for w in data["warehouses"] if w["warehouse_id"] == "WH-001")
    assert wh["quantity"] == 10


def test_la_bodega_aparece_online_y_al_dia(central, api, new_product):
    new_product(initial_stock=1)

    def caught_up():
        warehouses = {w["warehouse_id"]: w for w in central.get("/warehouses").json()}
        wh = warehouses.get("WH-001")
        return wh and wh["status"] == "ONLINE" and wh["events_missing"] == 0 and wh["watermark_sequence"] == wh["reported_sequence"] and wh

    wh = wait_until(caught_up, what="que WH-001 quede ONLINE y sin eventos pendientes")
    assert wh["api_url"]  # aprendida del heartbeat: la central sabe cómo llamar a la bodega


def test_consulta_en_vivo_llega_a_la_bodega(central, new_product):
    product = new_product(initial_stock=7)
    # La central llama a la bodega por la red warehouse_platform (lb_warehouse_WH-001:80)
    live = wait_until(lambda: (d := central.get(f"/global/inventory/{product['sku']}/live").json()).get("total_quantity") == 7 and d,
                      what="la consulta en vivo")
    assert "WH-001" not in live["unreachable_warehouses"]
