import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.modules.alerts.alerts_repository import AlertRepository

logger = logging.getLogger("warehouse.alerts")

OUT_OF_STOCK = "OUT_OF_STOCK"
DISCREPANCY = "DISCREPANCY"


class AlertService:
    """
    Registra alertas automáticas del inventario.
    IMPORTANTE: no hace commit. La alerta se guarda dentro de la MISMA transacción
    del movimiento que la provocó: si el movimiento se revierte (ROLLBACK),
    la alerta también desaparece. Nunca queda una alerta de algo que no pasó.
    """

    def __init__(self, db: Session):
        self.repo = AlertRepository(db)

    def register(self, product_id: int, alert_type: str, message: str, movement_id: Optional[int]):
        return self.repo.create(product_id, alert_type, message, movement_id)

    def list_alerts(self, limit: int, alert_type: Optional[str], product_id: Optional[int]):
        return self.repo.list(limit, alert_type, product_id)
