"""
Fase 7 — SINCRONIZACIÓN con la central (la central llama a estos endpoints).

  GET /sync/events?after_sequence=120   -> eventos 121, 122... desde el outbox
  GET /sync/snapshot                     -> foto completa y consistente de productos + inventario

Son la "red de seguridad" contra pérdida: si a la central le falta un evento
(por ejemplo, alguien vació una cola o su base se restauró de una copia vieja),
detecta el hueco en la secuencia y lo pide aquí. El outbox guarda los eventos
publicados durante OUTBOX_RETENTION_DAYS; para más tiempo, está el snapshot.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.events.models import OutboxEventEntity
from app.events.outbox import envelope_from_row
from app.events.payloads import product_payload
from app.modules.inventory.models import InventoryEntity
from app.modules.products.models import ProductEntity

router = APIRouter(prefix="/sync", tags=["Sync (central)"])


def _last_sequence(db: Session) -> int:
    return db.query(func.max(OutboxEventEntity.sequence)).scalar() or 0


@router.get("/events", summary="Eventos con secuencia mayor a after_sequence (para recuperar huecos)")
def events_after(
    after_sequence: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(OutboxEventEntity)
        .filter(OutboxEventEntity.sequence.isnot(None), OutboxEventEntity.sequence > after_sequence)
        .order_by(OutboxEventEntity.sequence)
        .limit(limit)
        .all()
    )
    oldest_kept = db.query(func.min(OutboxEventEntity.sequence)).scalar()
    return {
        "warehouse_id": settings.WAREHOUSE_ID,
        "last_sequence": _last_sequence(db),
        # si after_sequence + 1 < oldest_kept, esos eventos ya se limpiaron: hay que pedir un snapshot
        "oldest_available_sequence": oldest_kept,
        "events": [envelope_from_row(row) for row in rows],
    }


@router.get("/snapshot", summary="Foto consistente de productos e inventario (resincronización total)")
def snapshot(db: Session = Depends(get_db)):
    if db.bind.dialect.name == "postgresql":
        # Todas las lecturas ven la base en el MISMO instante (nada cambia a mitad de la foto)
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    last_sequence = _last_sequence(db)
    rows = (
        db.query(ProductEntity, InventoryEntity)
        .join(InventoryEntity, InventoryEntity.product_id == ProductEntity.id)
        .filter(InventoryEntity.warehouse_id == settings.WAREHOUSE_ID)
        .order_by(ProductEntity.id)
        .all()
    )
    return {
        "warehouse_id": settings.WAREHOUSE_ID,
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "last_sequence": last_sequence,
        "products": [
            {
                **product_payload(product),
                "quantity": inventory.quantity,
                "reserved_quantity": inventory.reserved_quantity,
                "inventory_version": inventory.version,
                "inventory_updated_at": inventory.updated_at.isoformat() if inventory.updated_at else None,
            }
            for product, inventory in rows
        ],
    }
