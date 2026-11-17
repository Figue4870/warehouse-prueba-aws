from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.modules.alerts.alerts_service import AlertService
from app.modules.alerts.schemas import AlertResponse

router = APIRouter(prefix="/alerts", tags=["Alerts"])


@router.get("", response_model=List[AlertResponse], summary="Alertas automáticas (agotados y desfases)")
def list_alerts(
    limit: int = Query(50, ge=1, le=500),
    type: Optional[Literal["OUT_OF_STOCK", "DISCREPANCY"]] = Query(None),
    product_id: Optional[int] = Query(None, gt=0),
    db: Session = Depends(get_db),
):
    return AlertService(db).list_alerts(limit, type, product_id)
