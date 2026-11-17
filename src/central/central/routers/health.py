from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from central.config import settings
from central.database import get_db
from central.models import WarehouseEntity
from central.status import ONLINE, warehouse_status

router = APIRouter(tags=["System"])


@router.get("/health", summary="Estado de la central")
def health(response: Response, db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "error", "database": "down", "instance": settings.INSTANCE_NAME}
    now = datetime.now(timezone.utc)
    statuses = [warehouse_status(w, now) for w in db.query(WarehouseEntity).all()]
    return {
        "status": "ok",
        "database": "ok",
        "instance": settings.INSTANCE_NAME,
        "warehouses_total": len(statuses),
        "warehouses_online": sum(1 for s in statuses if s["status"] == ONLINE),
    }
