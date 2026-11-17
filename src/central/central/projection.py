"""
PROYECCIÓN (CQRS): cómo cada evento de una bodega actualiza el modelo de lectura.

Tres garantías:

1) IDEMPOTENCIA — un evento se aplica una sola vez.
   processed_events guarda cada event_id. Si el mismo evento llega dos veces
   (reintento de RabbitMQ, o por RabbitMQ Y por /sync), la segunda vez se ignora.

2) NUNCA PISAR UN DATO NUEVO CON UNO VIEJO.
   Cada saldo trae inventory_version (sube con cada movimiento en la bodega).
   Solo se aplica si es MAYOR a la versión guardada. Así, aunque los eventos
   lleguen desordenados, el saldo final es el correcto.

3) DETECTAR PÉRDIDAS — marca de agua (watermark) por bodega.
   Cada evento trae su secuencia (1, 2, 3... sin huecos). La central lleva
   "watermark_sequence" = hasta dónde tiene TODO. Si llegan 1, 2, 4 el
   watermark se queda en 2: falta el 3. El reconciliador lo pedirá (sync.py).
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from platform_common.events import EventEnvelope, InvalidEventError
from sqlalchemy.orm import Session

from central.models import (
    CatalogProductEntity,
    CentralAlertEntity,
    MovementHistoryEntity,
    ProcessedEventEntity,
    WarehouseEntity,
    WarehouseInventoryEntity,
    WarehouseProductEntity,
)

APPLIED, DUPLICATE, HEARTBEAT = "applied", "duplicate", "heartbeat"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def as_aware(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite devuelve fechas sin zona horaria; las tratamos como UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def money(value) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


# ---------------------------------------------------------------- bodegas
def get_or_create_warehouse(db: Session, warehouse_id: str, lock: bool = False) -> WarehouseEntity:
    query = db.query(WarehouseEntity).filter(WarehouseEntity.warehouse_id == warehouse_id)
    if lock:
        # Serializa los eventos de UNA bodega (dos proyectores no avanzan el watermark a la vez)
        query = query.with_for_update()
    warehouse = query.first()
    if warehouse is None:
        warehouse = WarehouseEntity(warehouse_id=warehouse_id, first_seen_at=now_utc(), watermark_updated_at=now_utc(),
                                    reported_sequence=0, max_received_sequence=0, watermark_sequence=0)
        db.add(warehouse)
        db.flush()
    return warehouse


def record_heartbeat(db: Session, warehouse_id: str, payload: dict) -> None:
    warehouse = get_or_create_warehouse(db, warehouse_id, lock=True)
    warehouse.last_heartbeat_at = now_utc()
    warehouse.reported_sequence = max(warehouse.reported_sequence or 0, int(payload.get("last_sequence") or 0))
    warehouse.outbox_pending = payload.get("outbox_pending")
    if payload.get("api_url"):
        warehouse.api_url = str(payload["api_url"]).rstrip("/")


def advance_watermark(db: Session, warehouse: WarehouseEntity) -> None:
    """Sube el watermark mientras las secuencias sigan seguidas (sin huecos)."""
    following = (
        db.query(ProcessedEventEntity.sequence)
        .filter(ProcessedEventEntity.warehouse_id == warehouse.warehouse_id,
                ProcessedEventEntity.sequence > warehouse.watermark_sequence)
        .order_by(ProcessedEventEntity.sequence)
        .limit(5000)
        .all()
    )
    moved = False
    for (sequence,) in following:
        if sequence != warehouse.watermark_sequence + 1:
            break  # hueco: falta warehouse.watermark_sequence + 1
        warehouse.watermark_sequence = sequence
        moved = True
    if moved:
        warehouse.watermark_updated_at = now_utc()


# ---------------------------------------------------------------- catálogo e inventario
def upsert_catalog(db: Session, warehouse_id: str, payload: dict, occurred_at: datetime) -> None:
    sku = payload["sku"]
    product = db.get(CatalogProductEntity, sku)
    if product is None:
        product = CatalogProductEntity(sku=sku, name=payload.get("name") or payload.get("product_name") or sku)
        db.add(product)
        db.flush()
    elif product.source_updated_at and as_aware(product.source_updated_at) > occurred_at:
        return  # ya tenemos datos más nuevos de este SKU
    for field in ("name", "description", "category", "unit", "supplier"):
        if payload.get(field) is not None:
            setattr(product, field, payload[field])
    for field in ("supplier_cost", "retail_price", "wholesale_price"):
        if field in payload:
            setattr(product, field, money(payload[field]))
    product.source_warehouse_id = warehouse_id
    product.source_updated_at = occurred_at


def upsert_warehouse_product(db: Session, warehouse_id: str, payload: dict) -> None:
    key = {"warehouse_id": warehouse_id, "sku": payload["sku"]}
    row = db.get(WarehouseProductEntity, key)
    version = int(payload.get("product_version") or 0)
    if row is None:
        row = WarehouseProductEntity(**key, product_id_local=payload["product_id"], is_active=payload.get("is_active", True), product_version=version)
        db.add(row)
        db.flush()
    elif version > (row.product_version or 0):
        row.product_id_local = payload["product_id"]
        row.is_active = payload.get("is_active", True)
        row.product_version = version


def upsert_inventory(db: Session, warehouse_id: str, sku: str, quantity: int, reserved: int, version: int,
                     source_updated_at: Optional[datetime]) -> bool:
    """Aplica el saldo SOLO si su versión es más nueva. Devuelve True si cambió algo."""
    key = {"warehouse_id": warehouse_id, "sku": sku}
    row = db.get(WarehouseInventoryEntity, key)
    if row is None:
        row = WarehouseInventoryEntity(**key, quantity=0, reserved_quantity=0, version=-1, projected_at=now_utc())
        db.add(row)
        db.flush()
    if version <= (row.version if row.version is not None else -1):
        return False  # evento viejo o repetido: no pisa un saldo más nuevo
    row.quantity = quantity
    row.reserved_quantity = reserved
    row.version = version
    row.source_updated_at = source_updated_at
    row.projected_at = now_utc()
    return True


# ---------------------------------------------------------------- un handler por tipo de evento
def _on_product(db: Session, event: EventEnvelope, warehouse_id: str, occurred_at: datetime) -> None:
    upsert_warehouse_product(db, warehouse_id, event.payload)
    upsert_catalog(db, warehouse_id, event.payload, occurred_at)


def _on_movement(db: Session, event: EventEnvelope, warehouse_id: str, occurred_at: datetime) -> None:
    p = event.payload
    if db.get(WarehouseProductEntity, {"warehouse_id": warehouse_id, "sku": p["sku"]}) is None:
        upsert_warehouse_product(db, warehouse_id, {"sku": p["sku"], "product_id": p["product_id"], "product_version": 0})
    if db.get(CatalogProductEntity, p["sku"]) is None:
        upsert_catalog(db, warehouse_id, {"sku": p["sku"], "name": p.get("product_name"), "category": p.get("category"),
                                           "unit": p.get("unit")}, occurred_at)
    upsert_inventory(db, warehouse_id, p["sku"], int(p["quantity_after"]), int(p["reserved_after"]),
                     int(p.get("inventory_version") or 0), parse_time(p.get("created_at")) or occurred_at)
    if db.get(MovementHistoryEntity, event.event_id) is None:
        db.add(MovementHistoryEntity(
            event_id=event.event_id, warehouse_id=warehouse_id, sequence=event.sequence, movement_id=p.get("movement_id"),
            sku=p["sku"], product_name=p.get("product_name"), category=p.get("category"), type=p["type"],
            quantity=int(p["quantity"]), quantity_after=p.get("quantity_after"), reserved_after=p.get("reserved_after"),
            unit_retail_price=money(p.get("retail_price")), unit_supplier_cost=money(p.get("supplier_cost")),
            lot=p.get("lot"), reference=p.get("reference"),
            occurred_at=parse_time(p.get("created_at")) or occurred_at,
        ))


def _on_alert(db: Session, event: EventEnvelope, warehouse_id: str, occurred_at: datetime) -> None:
    p = event.payload
    if db.get(CentralAlertEntity, event.event_id) is None:
        db.add(CentralAlertEntity(event_id=event.event_id, warehouse_id=warehouse_id, alert_type=p.get("alert_type") or event.event_type,
                                  sku=p.get("sku"), product_name=p.get("product_name"), message=p.get("message"), occurred_at=occurred_at))


HANDLERS = {
    "PRODUCT_CREATED": _on_product,
    "PRODUCT_UPDATED": _on_product,
    "PRODUCT_DEACTIVATED": _on_product,
    "INVENTORY_MOVEMENT_RECORDED": _on_movement,
    "STOCK_DEPLETED": _on_alert,
    "STOCK_DISCREPANCY": _on_alert,
}


def apply_event(db: Session, event: EventEnvelope, source: str = "stream") -> str:
    """Aplica un evento al modelo de lectura. NO hace commit (lo hace quien llama)."""
    warehouse_id = event.warehouse_id or event.payload.get("warehouse_id")
    if not warehouse_id:
        raise InvalidEventError("El evento no dice de qué bodega viene")

    if event.event_type == "WAREHOUSE_HEARTBEAT":
        record_heartbeat(db, warehouse_id, event.payload)
        return HEARTBEAT

    warehouse = get_or_create_warehouse(db, warehouse_id, lock=True)
    if db.get(ProcessedEventEntity, event.event_id) is not None:
        return DUPLICATE

    occurred_at = parse_time(event.occurred_at) or now_utc()
    handler = HANDLERS.get(event.event_type)
    if handler is not None:
        handler(db, event, warehouse_id, occurred_at)
    # Los tipos que la central no proyecta (ej. REPORT_REQUESTED) igual se registran:
    # cuentan para la secuencia y así no parecen "perdidos".
    db.add(ProcessedEventEntity(event_id=event.event_id, warehouse_id=warehouse_id, sequence=event.sequence,
                                event_type=event.event_type, source=source, processed_at=now_utc()))
    if warehouse.last_event_at is None or as_aware(warehouse.last_event_at) < occurred_at:
        warehouse.last_event_at = occurred_at
    if event.sequence:
        warehouse.max_received_sequence = max(warehouse.max_received_sequence or 0, event.sequence)
        db.flush()
        advance_watermark(db, warehouse)
    return APPLIED


def apply_snapshot(db: Session, warehouse_id: str, snapshot: dict) -> int:
    """
    Resincronización total: aplica la foto de productos e inventario de una bodega.
    Respeta las versiones (no pisa datos más nuevos que ya hubieran llegado por eventos).
    """
    warehouse = get_or_create_warehouse(db, warehouse_id, lock=True)
    taken_at = parse_time(snapshot.get("taken_at")) or now_utc()
    changed = 0
    for product in snapshot.get("products", []):
        upsert_warehouse_product(db, warehouse_id, product)
        upsert_catalog(db, warehouse_id, product, parse_time(product.get("updated_at")) or taken_at)
        if upsert_inventory(db, warehouse_id, product["sku"], int(product["quantity"]), int(product["reserved_quantity"]),
                            int(product.get("inventory_version") or 0), parse_time(product.get("inventory_updated_at")) or taken_at):
            changed += 1
    warehouse.reported_sequence = max(warehouse.reported_sequence or 0, int(snapshot.get("last_sequence") or 0))
    warehouse.last_snapshot_at = now_utc()
    return changed
