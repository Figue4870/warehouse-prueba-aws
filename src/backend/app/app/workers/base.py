import logging

from platform_common.correlation import reset_correlation_id, set_correlation_id
from platform_common.events import EventEnvelope, InvalidEventError, parse_event
from platform_common.messaging import Consumer, PermanentError

import app.all_models  # noqa: F401 - registra todos los modelos (ver all_models.py)
from app.config import settings
from app.events.topology import EXCHANGES, QUEUES, declare_topology
from app.logging_config import setup_logging


def parse_or_permanent(body: bytes) -> EventEnvelope:
    try:
        return parse_event(body)
    except InvalidEventError as error:
        raise PermanentError(str(error)) from error


def with_correlation(handler):
    """Pone el correlation_id del mensaje en los logs mientras se procesa."""

    def wrapped(body, properties, routing_key):
        token = set_correlation_id(getattr(properties, "correlation_id", None))
        try:
            return handler(body, properties, routing_key)
        finally:
            reset_correlation_id(token)

    return wrapped


def run_consumer(queue_key: str, handler, service_name: str) -> None:
    setup_logging(service=service_name)
    logging.getLogger("warehouse.worker").info(
        "Iniciando worker", extra={"event": "worker_booting", "context": {"queue": QUEUES[queue_key].name}}
    )
    Consumer(
        url=settings.AMQP_URL,
        connection_name=f"{service_name}-{settings.WAREHOUSE_ID}",
        spec=QUEUES[queue_key],
        exchanges=EXCHANGES,
        handler=with_correlation(handler),
        declare_topology=declare_topology,
    ).run()
