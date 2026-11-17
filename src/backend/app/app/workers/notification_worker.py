"""
NOTIFICATION WORKER — avisa al Administrador de bodega (requisito del PDF:
"alertas ... de manera asíncrona al Administrador de bodega").

Recibe stock.depleted / stock.discrepancy. "Enviar" la notificación es:
  1) un log WARNING con event="admin_notified" (siempre), y
  2) un POST JSON a NOTIFICATION_WEBHOOK_URL si está configurada
     (por ejemplo un webhook de Slack/Teams o un servicio de correo).
Luego marca la alerta como notificada (notified_at). Si ya estaba notificada,
no vuelve a avisar (idempotente).
"""

import json
import logging
import urllib.request
from datetime import datetime, timezone

from platform_common.messaging import PermanentError

from app.config import settings
from app.database import SessionLocal
from app.modules.alerts.models import InventoryAlertEntity
from app.workers.base import parse_or_permanent, run_consumer

logger = logging.getLogger("warehouse.notifications")


def send_webhook(url: str, message: dict) -> None:
    request = urllib.request.Request(url, data=json.dumps(message).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - URL configurada por el administrador
        if response.status >= 300:
            raise RuntimeError(f"Webhook respondió {response.status}")


def make_handler(session_factory, webhook_url=None, sender=send_webhook):
    def handle(body, properties, routing_key):
        event = parse_or_permanent(body)
        alert_id = event.payload.get("alert_id")
        if alert_id is None:
            raise PermanentError("El evento no trae alert_id")
        with session_factory() as db:
            alert = db.get(InventoryAlertEntity, int(alert_id))
            if alert is None:
                raise PermanentError(f"La alerta {alert_id} no existe")
            if alert.notified_at is not None:
                return  # mensaje repetido: ya se avisó
            message = {
                "warehouse_id": settings.WAREHOUSE_ID,
                "alert_type": alert.type,
                "sku": event.payload.get("sku"),
                "product_name": event.payload.get("product_name"),
                "message": alert.message,
                "event_id": event.event_id,
            }
            channel = "log"
            if webhook_url:
                sender(webhook_url, message)  # si falla, excepción -> reintento
                channel = "webhook"
            logger.warning("Alerta notificada al administrador de bodega", extra={"event": "admin_notified", "context": message})
            alert.notified_at = datetime.now(timezone.utc)
            alert.notification_channel = channel
            db.commit()

    return handle


def main() -> None:
    run_consumer("notifications", make_handler(SessionLocal, settings.NOTIFICATION_WEBHOOK_URL), "notification-worker")


if __name__ == "__main__":
    main()
