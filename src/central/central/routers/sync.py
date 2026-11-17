from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from central.database import SessionLocal, get_db
from central.models import SyncRunEntity, WarehouseEntity
from central.projection import as_aware
from central.sync import WarehouseClient, catch_up, snapshot_sync

router = APIRouter(prefix="/sync", tags=["Sync"])


@router.get("/runs", summary="Últimas sincronizaciones (catch-up y snapshot)")
def sync_runs(limit: int = Query(30, ge=1, le=500), db: Session = Depends(get_db)):
    rows = db.query(SyncRunEntity).order_by(SyncRunEntity.id.desc()).limit(limit).all()
    return [{"id": r.id, "warehouse_id": r.warehouse_id, "kind": r.kind, "status": r.status, "events_applied": r.events_applied,
             "detail": r.detail, "started_at": as_aware(r.started_at), "finished_at": as_aware(r.finished_at)} for r in rows]


@router.post("/warehouses/{warehouse_id}/catch-up", summary="Pedir YA a la bodega los eventos que falten")
def force_catch_up(warehouse_id: str, db: Session = Depends(get_db)):
    applied = catch_up(SessionLocal, warehouse_id, WarehouseClient())
    warehouse = db.get(WarehouseEntity, warehouse_id)
    db.refresh(warehouse)
    return {"warehouse_id": warehouse_id, "events_applied": applied, "watermark_sequence": warehouse.watermark_sequence,
            "status": warehouse.last_sync_status, "error": warehouse.last_sync_error}


@router.post("/warehouses/{warehouse_id}/snapshot", summary="Resincronización total con la foto de la bodega")
def force_snapshot(warehouse_id: str, db: Session = Depends(get_db)):
    changed = snapshot_sync(SessionLocal, warehouse_id, WarehouseClient())
    warehouse = db.get(WarehouseEntity, warehouse_id)
    db.refresh(warehouse)
    return {"warehouse_id": warehouse_id, "inventory_rows_changed": changed, "status": warehouse.last_sync_status,
            "error": warehouse.last_sync_error}
