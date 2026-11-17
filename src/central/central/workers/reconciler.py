"""
RECONCILER — cada RECONCILE_SECONDS revisa cada bodega:
  * ¿es nueva (nunca se tomó su snapshot)?           -> snapshot
  * ¿le faltan eventos que no llegaron por RabbitMQ? -> catch-up por HTTP
Ver central/sync.py.
"""

import logging
import time

from platform_common.messaging import GracefulStop

from central import models  # noqa: F401
from central.config import settings
from central.database import SessionLocal
from central.logging_config import setup_logging
from central.sync import WarehouseClient, reconcile_once

logger = logging.getLogger("central.reconciler")


def main() -> None:
    setup_logging("central-reconciler")
    stopper = GracefulStop()
    client = WarehouseClient()
    logger.info("Reconciliador iniciado", extra={"event": "reconciler_started", "context": {"every_seconds": settings.RECONCILE_SECONDS}})
    while not stopper.stop:
        try:
            actions = reconcile_once(SessionLocal, client)
            if actions:
                logger.info("Reconciliación", extra={"event": "reconcile", "context": {"actions": actions}})
        except Exception as error:  # noqa: BLE001 - la base central puede estar caída: se reintenta
            logger.warning("Reconciliación fallida", extra={"event": "reconcile_failed", "context": {"error": str(error)}})
        for _ in range(settings.RECONCILE_SECONDS):
            if stopper.stop:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
