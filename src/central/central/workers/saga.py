"""
SAGA ORCHESTRATOR (Fase 6) — recibe las respuestas de las bodegas y avanza cada transferencia.

    bodegas (outbox) -> forwarder -> central.events -> cola central.saga (*.transfer.#) -> este worker

Ack manual + reintentos + DLQ (como todos los workers). Cada respuesta se
procesa con la fila de la transferencia bloqueada y es idempotente: si llega
repetida, se ignora (ver central/saga.py).
"""

import logging

from platform_common.central_topology import CENTRAL_EXCHANGES, SAGA_QUEUE, declare_central_topology
from platform_common.correlation import reset_correlation_id, set_correlation_id
from platform_common.events import InvalidEventError, parse_event
from platform_common.messaging import Consumer, PermanentError, QueueSpec

from central import models  # noqa: F401
from central.config import settings
from central.database import SessionLocal
from central.logging_config import setup_logging
from central.saga import handle_reply

logger = logging.getLogger("central.saga")


def make_handler(session_factory):
    def handle(body, properties, routing_key):
        try:
            event = parse_event(body)
        except InvalidEventError as error:
            raise PermanentError(str(error)) from error
        token = set_correlation_id(event.correlation_id)
        try:
            with session_factory() as db:
                result = handle_reply(db, event)
                db.commit()
            logger.info("Respuesta de la Saga procesada", extra={"event": "saga_reply", "context": {
                "transfer_id": event.payload.get("transfer_id"), "event_type": event.event_type,
                "warehouse_id": event.warehouse_id, "result": result}})
        finally:
            reset_correlation_id(token)

    return handle


def main() -> None:
    setup_logging("central-saga")
    spec = QueueSpec(SAGA_QUEUE.name, SAGA_QUEUE.bindings, settings.WORKER_RETRY_DELAY_MS, settings.WORKER_MAX_RETRIES)
    Consumer(settings.AMQP_URL, f"central-saga-{settings.INSTANCE_NAME}", spec, CENTRAL_EXCHANGES,
             make_handler(SessionLocal), declare_topology=declare_central_topology, prefetch=1).run()


if __name__ == "__main__":
    main()
