from typing import List, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.modules.alerts.models import InventoryAlertEntity


class AlertRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, product_id: int, alert_type: str, message: str, movement_id: Optional[int]) -> InventoryAlertEntity:
        alert = InventoryAlertEntity(
            product_id=product_id,
            warehouse_id=settings.WAREHOUSE_ID,
            type=alert_type,
            message=message,
            movement_id=movement_id,
        )
        self.db.add(alert)
        self.db.flush()  # para conocer alert.id (va dentro del evento)
        return alert

    def list(self, limit: int, alert_type: Optional[str], product_id: Optional[int]) -> List[InventoryAlertEntity]:
        query = self.db.query(InventoryAlertEntity).filter(InventoryAlertEntity.warehouse_id == settings.WAREHOUSE_ID)
        if alert_type:
            query = query.filter(InventoryAlertEntity.type == alert_type)
        if product_id:
            query = query.filter(InventoryAlertEntity.product_id == product_id)
        return query.order_by(InventoryAlertEntity.id.desc()).limit(limit).all()
