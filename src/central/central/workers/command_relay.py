"""
COMMAND RELAY (Fase 6) — publica los comandos de la Saga y vigila los plazos.

    transfer_commands (outbox de la central) --publish + confirm--> central.commands --> commands.WH-00X

Igual que el relay de las bodegas (Fase 3): un comando se marca publicado SOLO
cuando RabbitMQ confirmó que lo guardó. Si RabbitMQ está caído, los comandos
esperan en la base y las transferencias se quedan en REQUESTED (no se pierden).

Cada vuelta también revisa los vencimientos (saga.check_timeouts): un paso que
no respondió a tiempo empieza su compensación.
"""

import json
import logging
import time

from platform_common.central_topology import COMMAND_EXCHANGES, declare_command_topology
from platform_common.messaging import GracefulStop, ReliablePublisher, declare_exchanges
from sqlalchemy.orm import sessionmaker

from central import models  # noqa: F401
from central.config import settings
from central.database import SessionLocal
from central.logging_config import setup_logging
from central.models import TransferCommandEntity
from central.projection import as_aware
from central.saga import check_timeouts, mark_published

logger = logging.getLogger("central.command_relay")


class CommandPublisher(ReliablePublisher):
    """Publicador con confirmación que declara la cola de cada bodega antes de enviarle algo."""

    def __init__(self, url: str, connection_name: str):
        super().__init__(url, connection_name, on_connect=self._on_connect)
        self.declared: set[str] = set()

    def _on_connect(self, channel) -> None:
        self.declared = set()
        declare_exchanges(channel, COMMAND_EXCHANGES)

    def send(self, command: TransferCommandEntity) -> None:
        channel = self._ensure_channel()
        if command.warehouse_id not in self.declared:
            # La cola se crea aunque la bodega nunca se haya conectado: el comando la espera ahí
            declare_command_topology(channel, command.warehouse_id)
            self.declared.add(command.warehouse_id)
        body = {
            "command_id": command.command_id, "command_type": command.command_type, "transfer_id": command.transfer_id,
            "warehouse_id": command.warehouse_id, "issued_at": as_aware(command.created_at).isoformat(),
            "deadline": as_aware(command.deadline).isoformat() if command.deadline else None,
            "correlation_id": command.correlation_id, "payload": command.payload,
        }
        self.publish(exchange=COMMAND_EXCHANGES.events, routing_key=command.warehouse_id, body=json.dumps(body).encode(),
                     message_id=command.command_id, correlation_id=command.correlation_id, message_type=command.command_type,
                     headers={"transfer_id": command.transfer_id})


class CommandRelay:
    def __init__(self, session_factory: sessionmaker, publisher, batch_size: int = 100):
        self.session_factory = session_factory
        self.publisher = publisher
        self.batch_size = batch_size

    def publish_pending(self) -> int:
        """Publica en orden los comandos pendientes. Se detiene en el primero que falle (conserva el orden)."""
        published = 0
        with self.session_factory() as db:
            query = (db.query(TransferCommandEntity).filter(TransferCommandEntity.published_at.is_(None))
                     .order_by(TransferCommandEntity.id).limit(self.batch_size))
            if db.bind.dialect.name == "postgresql":
                query = query.with_for_update(skip_locked=True)
            for command in query.all():
                try:
                    self.publisher.send(command)
                except Exception as error:  # noqa: BLE001 - RabbitMQ caído: se reintenta en la próxima vuelta
                    command.attempts = (command.attempts or 0) + 1
                    command.last_error = f"{type(error).__name__}: {error}"[:500]
                    db.commit()
                    raise
                mark_published(db, command)
                db.commit()
                published += 1
                logger.info("Comando publicado", extra={"event": "command_published", "context": {
                    "transfer_id": command.transfer_id, "command": command.command_type, "warehouse_id": command.warehouse_id}})
        return published

    def run_once(self) -> dict:
        published = self.publish_pending()
        with self.session_factory() as db:
            timed_out = check_timeouts(db)
        return {"published": published, "timed_out": timed_out}


def main() -> None:
    setup_logging("central-command-relay")
    stopper = GracefulStop()
    relay = CommandRelay(SessionLocal, CommandPublisher(settings.AMQP_URL, f"central-command-relay-{settings.INSTANCE_NAME}"))
    logger.info("Relay de comandos iniciado", extra={"event": "command_relay_started"})
    backoff = 1
    while not stopper.stop:
        try:
            result = relay.run_once()
            if result["timed_out"]:
                logger.warning("Pasos vencidos: se inicia su compensación", extra={"event": "saga_timeouts", "context": result})
            backoff = 1
            time.sleep(settings.SAGA_TICK_SECONDS)
        except Exception as error:  # noqa: BLE001
            logger.warning("No se pudieron publicar comandos; reintentando", extra={"event": "command_relay_error",
                           "context": {"error": str(error) or type(error).__name__, "retry_in_seconds": backoff}})
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
    relay.publisher.close()


if __name__ == "__main__":
    main()
