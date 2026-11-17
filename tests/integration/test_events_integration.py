"""
Fase 3 contra la bodega REAL (API + PostgreSQL + Redis + RabbitMQ + workers).
Requiere: docker compose -f basic-docker-compose.yml up -d --build
"""

import time

import pytest


def wait_until(check, timeout=30.0, every=0.5):
    start = time.time()
    while time.time() - start < timeout:
        value = check()
        if value:
            return value
        time.sleep(every)
    pytest.fail("Se acabó el tiempo esperando a los workers (¿están corriendo relay y workers?)")


def test_reporte_asincrono_de_punta_a_punta(api):
    response = api.post("/reports", json={"type": "STOCK_VALUATION"})
    assert response.status_code == 202
    job_url = response.json()["status_url"]
    job = wait_until(lambda: (j := api.get(job_url).json())["status"] == "COMPLETED" and j)
    assert job["result"]["report_type"] == "STOCK_VALUATION"
    assert api.get(f"{job_url}/download").status_code == 200


def test_el_relay_vacia_el_outbox(api, new_product):
    new_product(initial_stock=3)
    wait_until(lambda: api.get("/metrics").json()["outbox"]["pending"] == 0)
    assert api.get("/metrics").json()["outbox"]["last_sequence"] > 0


def test_la_alerta_de_agotado_se_notifica(api, new_product):
    product = new_product(initial_stock=1)
    api.post("/inventory/movements", json={"product_id": product["id"], "type": "SHIPMENT", "quantity": 1})
    alert = wait_until(lambda: next(
        (a for a in api.get("/alerts", params={"product_id": product["id"]}).json() if a["notified_at"]), None))
    assert alert["type"] == "OUT_OF_STOCK"


def test_correlation_id_de_ida_y_vuelta(api):
    response = api.get("/health", headers={"X-Correlation-ID": "COR-INTEGRACION-1"})
    assert response.headers["X-Correlation-ID"] == "COR-INTEGRACION-1"
