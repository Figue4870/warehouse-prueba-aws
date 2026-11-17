"""Fase 7: la central recupera lo que le falta (catch-up) y se resincroniza (snapshot)."""

from datetime import datetime, timedelta, timezone

from central.models import SyncRunEntity, WarehouseEntity, WarehouseInventoryEntity
from central.sync import catch_up, needs_catch_up, reconcile_once


class FakeWarehouse:
    """Imita los endpoints /sync de una bodega."""

    def __init__(self, events=(), snapshot=None, down=False, oldest=1):
        self.events = list(events)
        self.snapshot_data = snapshot or {"products": [], "last_sequence": 0}
        self.down = down
        self.oldest = oldest
        self.calls = []

    def events_after(self, api_url, after_sequence, limit):
        self.calls.append(("events", after_sequence))
        if self.down:
            raise ConnectionError("bodega caída")
        page = [e.model_dump() for e in self.events if e.sequence > after_sequence][:limit]
        return {"last_sequence": max((e.sequence for e in self.events), default=0), "oldest_available_sequence": self.oldest, "events": page}

    def snapshot(self, api_url):
        self.calls.append(("snapshot",))
        if self.down:
            raise ConnectionError("bodega caída")
        return self.snapshot_data


def test_catch_up_llena_el_hueco(apply, central_sf, get_row, wh1):
    events = [wh1.movement(after=i, version=i) for i in range(1, 6)]
    apply(wh1.heartbeat(5), events[0], events[1], events[3])  # faltan 3 y 5
    assert get_row(WarehouseEntity, "WH-001").watermark_sequence == 2
    fake = FakeWarehouse(events)
    assert catch_up(central_sf, "WH-001", fake) == 2  # solo aplicó los que faltaban (4 era duplicado)
    assert fake.calls[0] == ("events", 2)
    warehouse = get_row(WarehouseEntity, "WH-001")
    assert warehouse.watermark_sequence == 5 and warehouse.last_sync_status == "OK"
    assert get_row(WarehouseInventoryEntity, {"warehouse_id": "WH-001", "sku": "SKU-1"}).quantity == 5


def test_bodega_caida_queda_registrado_y_se_reintenta(apply, central_sf, get_row, wh1):
    apply(wh1.heartbeat(3))
    catch_up(central_sf, "WH-001", FakeWarehouse(down=True))  # no explota
    warehouse = get_row(WarehouseEntity, "WH-001")
    assert warehouse.last_sync_status == "ERROR" and "bodega caída" in warehouse.last_sync_error
    with central_sf() as db:
        assert db.query(SyncRunEntity).filter(SyncRunEntity.status == "ERROR").count() == 1


def test_hueco_reciente_espera_antes_de_pedir(apply, get_row, wh1):
    apply(wh1.heartbeat(4), wh1.movement())
    warehouse = get_row(WarehouseEntity, "WH-001")
    now = datetime.now(timezone.utc)
    assert needs_catch_up(warehouse, now) is False                       # puede que aún esté en camino
    assert needs_catch_up(warehouse, now + timedelta(seconds=60)) is True  # ya no: hay que pedirlo


def test_reconciliador_snapshot_inicial_y_luego_catch_up(apply, central_sf, get_row, wh1):
    events = [wh1.movement(after=i, version=i) for i in range(1, 4)]
    apply(wh1.heartbeat(3))
    fake = FakeWarehouse(events, snapshot={"last_sequence": 3, "products": []})
    actions = reconcile_once(central_sf, fake, now=datetime.now(timezone.utc) + timedelta(seconds=60))
    assert actions == ["snapshot:WH-001", "catch_up:WH-001"]
    assert get_row(WarehouseEntity, "WH-001").watermark_sequence == 3
    # siguiente vuelta: ya está al día, no hace nada
    assert reconcile_once(central_sf, fake, now=datetime.now(timezone.utc) + timedelta(seconds=120)) == []


def test_eventos_ya_borrados_en_la_bodega_se_cubren_con_snapshot(apply, central_sf, get_row, wh1):
    events = [wh1.movement(after=i, version=i) for i in range(1, 11)]
    apply(wh1.heartbeat(10))
    snapshot = {"last_sequence": 10, "products": [{"product_id": 1, "sku": "SKU-1", "name": "Casco", "quantity": 10,
                                                   "reserved_quantity": 0, "inventory_version": 10, "product_version": 1}]}
    fake = FakeWarehouse(events[7:], snapshot=snapshot, oldest=8)  # la bodega solo conserva 8, 9, 10
    catch_up(central_sf, "WH-001", fake)
    assert ("snapshot",) in fake.calls
    assert get_row(WarehouseEntity, "WH-001").watermark_sequence == 10
    assert get_row(WarehouseInventoryEntity, {"warehouse_id": "WH-001", "sku": "SKU-1"}).quantity == 10
