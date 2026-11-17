"""
PROJECTOR — consume la cola central.projection y aplica cada evento al modelo
de lectura (projection.apply_event). Ack manual + reintentos + DLQ, como los
workers de las bodegas. Varias copias pueden correr a la vez (son idempotentes).
"""

import logging

from platform_common.central_topology import CENTRAL_EXCHANGES, PROJECTION_QUEUE, declare_central_topology
from platform_common.correlation import reset_correlation_id, set_correlation_id
from platform_common.events import InvalidEventError, parse_event
from platform_common.messaging import Consumer, PermanentError, QueueSpec

from central import models  # noqa: F401
from central.config import settings
from central.database import SessionLocal
from central.logging_config import setup_logging
from central.projection import apply_event

logger = logging.getLogger("central.projector")


def make_handler(session_factory):
    def handle(body, properties, routing_key):
        try:
            event = parse_event(body)
        except InvalidEventError as error:
            raise PermanentError(str(error)) from error
        token = set_correlation_id(event.correlation_id)
        try:
            with session_factory() as db:
                result = apply_event(db, event, source="stream")
                db.commit()
            if result != "heartbeat":
                logger.info("Evento proyectado", extra={"event": "event_projected", "context": {
                    "warehouse_id": event.warehouse_id, "event_type": event.event_type, "sequence": event.sequence, "result": result}})
        except InvalidEventError as error:
            raise PermanentError(str(error)) from error
        finally:
            reset_correlation_id(token)

    return handle


def main() -> None:
    setup_logging("central-projector")
    spec = QueueSpec(PROJECTION_QUEUE.name, PROJECTION_QUEUE.bindings, settings.WORKER_RETRY_DELAY_MS, settings.WORKER_MAX_RETRIES)
    Consumer(settings.AMQP_URL, f"central-projector-{settings.INSTANCE_NAME}", spec, CENTRAL_EXCHANGES,
             make_handler(SessionLocal), declare_topology=declare_central_topology).run()


if __name__ == "__main__":
    main()
