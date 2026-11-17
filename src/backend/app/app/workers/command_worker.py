"""
COMMAND WORKER (Fase 6) — ejecuta en ESTA bodega los pasos de la Saga de transferencias.

    RabbitMQ CENTRAL                              esta bodega
    commands.WH-00X  --(RESERVE, RECEIVE...)-->   command-worker  --> PostgreSQL (movimiento + operación + respuesta en el outbox)

Se conecta al RabbitMQ de la CENTRAL (CENTRAL_AMQP_URL), a la cola de comandos
de su bodega. Ack manual: solo hace ack después del COMMIT local. Si la bodega
se cae a mitad de un comando, RabbitMQ lo vuelve a entregar y la idempotencia
(transfer_operations) evita hacerlo dos veces.

Si la central (o la red) no está, los comandos esperan en la central; esta
bodega sigue vendiendo normalmente.
"""

import logging
import time

from platform_common.central_topology import COMMAND_EXCHANGES, command_queue, declare_command_topology
from platform_common.commands import InvalidCommandError, parse_command
from platform_common.correlation import reset_correlation_id, set_correlation_id
from platform_common.messaging import Consumer, GracefulStop, PermanentError

import app.all_models  # noqa: F401
from app.config import settings
from app.database import SessionLocal
from app.logging_config import setup_logging
from app.modules.transfers.transfer_commands import TransferCommandService

logger = logging.getLogger("warehouse.command_worker")


def make_handler(session_factory, warehouse_id: str, cache=None):
    def handle(body, properties, routing_key):
        try:
            command = parse_command(body)
        except InvalidCommandError as error:
            raise PermanentError(str(error)) from error
        if command.warehouse_id != warehouse_id:
            raise PermanentError(f"El comando es para {command.warehouse_id}, no para {warehouse_id}")
        token = set_correlation_id(command.correlation_id)
        try:
            with session_factory() as db:
                TransferCommandService(db, cache).handle(command)
        finally:
            reset_correlation_id(token)

    return handle


def main() -> None:
    setup_logging(service="command-worker")
    if not settings.CENTRAL_AMQP_URL:
        logger.error("CENTRAL_AMQP_URL vacío: sin central no hay transferencias", extra={"event": "command_worker_disabled"})
        stopper = GracefulStop()
        while not stopper.stop:  # queda inactivo (en vez de reiniciarse en bucle en Docker)
            time.sleep(1)
        return
    spec = command_queue(settings.WAREHOUSE_ID)
    logger.info("Esperando comandos de la central", extra={"event": "command_worker_started", "context": {"queue": spec.name}})
    Consumer(
        url=settings.CENTRAL_AMQP_URL,
        connection_name=f"command-worker-{settings.WAREHOUSE_ID}",
        spec=spec,
        exchanges=COMMAND_EXCHANGES,
        handler=make_handler(SessionLocal, settings.WAREHOUSE_ID),
        declare_topology=lambda channel: declare_command_topology(channel, settings.WAREHOUSE_ID),
        prefetch=1,  # un comando a la vez: más fácil de seguir en los logs y conserva el orden
    ).run()


if __name__ == "__main__":
    main()
