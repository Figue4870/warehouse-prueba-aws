from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.modules.transfers.transfer_commands import operations_for

router = APIRouter(prefix="/transfer-operations", tags=["Transfers (Saga, Fase 6)"])


@router.get("", summary="Qué hizo esta bodega en cada paso de las transferencias (solo lectura)")
def list_operations(transfer_id: Optional[str] = Query(None), limit: int = Query(100, ge=1, le=1000),
                    db: Session = Depends(get_db)):
    return [{
        "warehouse_id": settings.WAREHOUSE_ID, "transfer_id": op.transfer_id, "step": op.step, "outcome": op.outcome,
        "sku": op.sku, "product_id": op.product_id, "quantity": op.quantity, "reason": op.reason,
        "movement_id": op.movement_id, "command_id": op.command_id, "created_at": op.created_at,
    } for op in operations_for(db, transfer_id, limit)]
