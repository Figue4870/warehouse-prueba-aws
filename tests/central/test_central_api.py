"""Fase 7: API central (consultas globales, estado de bodegas y reportes)."""

from datetime import datetime, timedelta, timezone

from central.models import WarehouseEntity


def test_inventario_global_suma_todas_las_bodegas(central_client, apply, wh1, wh2):
    apply(wh1.heartbeat(0), wh2.heartbeat(0))
    apply(wh1.product(), wh1.movement(quantity=100, after=100))
    apply(wh2.product(), wh2.movement(quantity=50, after=50), wh2.movement(m_type="RESERVATION", quantity=5, after=45, reserved=5))
    data = central_client.get("/global/inventory/SKU-1").json()
    assert data["total_quantity"] == 145 and data["total_reserved"] == 5
    assert [w["warehouse_id"] for w in data["warehouses"]] == ["WH-001", "WH-002"]
    assert data["consistency"] == "eventual" and data["warnings"] == []
    assert data["product"]["name"] == "Casco"


def test_bodega_sin_contacto_se_marca_desactualizada(central_client, apply, central_sf, wh1, wh2):
    apply(wh1.heartbeat(0), wh2.heartbeat(0), wh1.movement(), wh2.movement())
    with central_sf() as db:
        db.get(WarehouseEntity, "WH-002").last_heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.commit()
    data = central_client.get("/global/inventory/SKU-1").json()
    wh2_row = next(w for w in data["warehouses"] if w["warehouse_id"] == "WH-002")
    assert wh2_row["warehouse_status"] == "OFFLINE" and wh2_row["stale"] is True
    assert any("WH-002" in w and "desactualizados" in w for w in data["warnings"])
    statuses = {w["warehouse_id"]: w["status"] for w in central_client.get("/warehouses").json()}
    assert statuses == {"WH-001": "ONLINE", "WH-002": "OFFLINE"}


def test_sku_desconocido_404(central_client):
    assert central_client.get("/global/inventory/NO-EXISTE").status_code == 404


def test_reportes_globales(central_client, apply, wh1, wh2):
    apply(wh1.movement(quantity=10, after=10), wh1.movement(m_type="SHIPMENT", quantity=3, after=7))
    apply(wh2.movement(quantity=5, after=5), wh2.movement(m_type="SHIPMENT", quantity=2, after=3))
    [top] = central_client.get("/reports/top-products").json()
    assert top["units_sold"] == 5 and top["sales_value"] == "500.00"
    sales = central_client.get("/reports/sales-by-day").json()
    assert {s["warehouse_id"]: s["units"] for s in sales} == {"WH-001": 3, "WH-002": 2}
    stock = central_client.get("/reports/stock-by-warehouse").json()
    assert stock["rows"][0]["by_warehouse"] == {"WH-001": 7, "WH-002": 3} and stock["rows"][0]["total_on_hand"] == 10
    by_type = central_client.get("/reports/movements-by-type").json()
    assert {"warehouse_id": "WH-001", "type": "SHIPMENT", "movements": 1, "units": 3} in by_type


def test_health_y_resumen(central_client, apply, wh1):
    apply(wh1.heartbeat(0), wh1.movement(quantity=8, after=8))
    health = central_client.get("/health").json()
    assert health["status"] == "ok" and health["warehouses_online"] == 1
    assert central_client.get("/global/summary").json()["total_quantity"] == 8


def test_tablero_web(central_client):
    page = central_client.get("/dashboard")
    assert page.status_code == 200 and "Plataforma Central" in page.text and "/global/inventory" in page.text
