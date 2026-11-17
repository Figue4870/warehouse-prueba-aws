from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from central.database import get_db
from central.models import WarehouseEntity
from central.status import warehouse_status

router = APIRouter(prefix="/warehouses", tags=["Warehouses"])


@router.get("", summary="Bodegas conocidas y qué tan al día está su información")
def list_warehouses(db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    return [warehouse_status(w, now) for w in db.query(WarehouseEntity).order_by(WarehouseEntity.warehouse_id).all()]


@router.get("/{warehouse_id}", summary="Estado de una bodega")
def get_warehouse(warehouse_id: str, db: Session = Depends(get_db)):
    warehouse = db.get(WarehouseEntity, warehouse_id)
    if warehouse is None:
        raise HTTPException(status_code=404, detail="Bodega desconocida")
    return warehouse_status(warehouse, datetime.now(timezone.utc))
