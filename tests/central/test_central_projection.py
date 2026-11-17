"""Fase 7: la proyección es idempotente, no pisa datos nuevos y detecta huecos."""

from central.models import (CatalogProductEntity, CentralAlertEntity, MovementHistoryEntity, ProcessedEventEntity,
                            WarehouseEntity, WarehouseInventoryEntity, WarehouseProductEntity)
from central.projection import apply_snapshot


def test_un_movimiento_actualiza_saldo_historial_y_catalogo(apply, get_row, wh1):
    assert apply(wh1.product(), wh1.movement(quantity=10, after=10)) == ["applied", "applied"]
    inventory = get_row(WarehouseInventoryEntity, {"warehouse_id": "WH-001", "sku": "SKU-1"})
    assert inventory.quantity == 10 and inventory.version == 1
    assert get_row(CatalogProductEntity, "SKU-1").retail_price == 100
    warehouse = get_row(WarehouseEntity, "WH-001")
    assert warehouse.watermark_sequence == 2 and warehouse.max_received_sequence == 2


def test_evento_repetido_no_se_cuenta_dos_veces(apply, central_sf, wh1):
    movement = wh1.movement()
    assert apply(movement, movement) == ["applied", "duplicate"]
    with central_sf() as db:
        assert db.query(MovementHistoryEntity).count() == 1
        assert db.query(ProcessedEventEntity).count() == 1


def test_eventos_desordenados_no_pisan_el_saldo_nuevo(apply, get_row, wh1):
    first = wh1.movement(quantity=10, after=10, version=1)
    second = wh1.movement(m_type="SHIPMENT", quantity=4, after=6, version=2)
    apply(second, first)  # llegan al revés
    assert get_row(WarehouseInventoryEntity, {"warehouse_id": "WH-001", "sku": "SKU-1"}).quantity == 6


def test_el_watermark_se_detiene_en_el_hueco(apply, get_row, wh1):
    e1, e2, e3, e4 = (wh1.movement(after=i, version=i) for i in range(1, 5))
    apply(e1, e2, e4)  # falta la secuencia 3
    warehouse = get_row(WarehouseEntity, "WH-001")
    assert warehouse.watermark_sequence == 2 and warehouse.max_received_sequence == 4
    apply(e3)  # llega la que faltaba
    assert get_row(WarehouseEntity, "WH-001").watermark_sequence == 4


def test_heartbeat_actualiza_contacto_sin_contar_como_evento(apply, get_row, central_sf, wh1):
    assert apply(wh1.heartbeat(last_sequence=7, api_url="http://10.0.0.5:8001/")) == ["heartbeat"]
    warehouse = get_row(WarehouseEntity, "WH-001")
    assert warehouse.reported_sequence == 7 and warehouse.api_url == "http://10.0.0.5:8001"
    assert warehouse.last_heartbeat_at is not None
    with central_sf() as db:
        assert db.query(ProcessedEventEntity).count() == 0


def test_tipos_que_no_se_proyectan_igual_avanzan_la_secuencia(apply, get_row, wh1):
    apply(wh1.make("REPORT_REQUESTED", {"job_id": "JOB-1"}), wh1.movement())
    assert get_row(WarehouseEntity, "WH-001").watermark_sequence == 2


def test_baja_de_producto_y_catalogo_no_retrocede(apply, get_row, wh1, ago):
    newer = wh1.product(name="Nombre nuevo", version=2, event_type="PRODUCT_UPDATED")
    older = wh1.product(name="Nombre viejo", version=1, occurred_at=ago(10))
    apply(newer, older)
    assert get_row(CatalogProductEntity, "SKU-1").name == "Nombre nuevo"
    apply(wh1.product(version=3, active=False, event_type="PRODUCT_DEACTIVATED"))
    assert get_row(WarehouseProductEntity, {"warehouse_id": "WH-001", "sku": "SKU-1"}).is_active is False


def test_alertas_de_las_bodegas_llegan_a_la_central(apply, central_sf, wh1):
    apply(wh1.make("STOCK_DEPLETED", {"alert_type": "OUT_OF_STOCK", "sku": "SKU-1", "message": "agotado"}))
    with central_sf() as db:
        [alert] = db.query(CentralAlertEntity).all()
    assert alert.alert_type == "OUT_OF_STOCK" and alert.warehouse_id == "WH-001"


def test_snapshot_respeta_versiones(apply, central_sf, get_row, wh1):
    apply(wh1.movement(after=50, version=5))
    snapshot = {"taken_at": "2026-11-17T00:00:00+00:00", "last_sequence": 9, "products": [
        {"product_id": 1, "sku": "SKU-1", "name": "Casco", "quantity": 40, "reserved_quantity": 0, "inventory_version": 4, "product_version": 1},
        {"product_id": 2, "sku": "SKU-2", "name": "Guante", "quantity": 7, "reserved_quantity": 1, "inventory_version": 3, "product_version": 1},
    ]}
    with central_sf() as db:
        assert apply_snapshot(db, "WH-001", snapshot) == 1  # solo SKU-2 cambió
        db.commit()
    assert get_row(WarehouseInventoryEntity, {"warehouse_id": "WH-001", "sku": "SKU-1"}).quantity == 50  # v5 > v4
    assert get_row(WarehouseInventoryEntity, {"warehouse_id": "WH-001", "sku": "SKU-2"}).quantity == 7
    warehouse = get_row(WarehouseEntity, "WH-001")
    assert warehouse.last_snapshot_at is not None and warehouse.reported_sequence == 9
