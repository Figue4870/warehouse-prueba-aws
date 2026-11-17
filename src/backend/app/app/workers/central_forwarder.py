"""
CENTRAL FORWARDER (Fase 7) — "store and forward" hacia la plataforma central.

  RabbitMQ de la bodega              RabbitMQ CENTRAL
  wh.central-forward  ---------->    exchange central.events
         ^              confirm      routing key "WH-001.inventory.movement.recorded"
         |
     (TODOS los eventos de la bodega)

Reglas para no perder nada:
  1. Solo hace ack del mensaje LOCAL después de que el RabbitMQ CENTRAL confirmó
     que guardó la copia (publisher confirms).
  2. Si la central está caída o no hay red: NO hace ack. Cierra la conexión, los
     mensajes vuelven a la cola local (durable, en disco) y espera para reintentar
     (1, 2, 4... hasta 30 s). Pueden pasar horas: cuando la central vuelve,
     todo lo acumulado llega solo y en orden.
  3. Si la propia bodega se reinicia, la cola local sigue en el volumen de RabbitMQ.
"""

import logging
import time

import pika
from platform_common.central_topology import CENTRAL_EXCHANGES, central_routing_key, declare_central_topology
from platform_common.correlation import reset_correlation_id, set_correlation_id
from platform_common.messaging import GracefulStop, ReliablePublisher, connect
from pika.exceptions import AMQPError

from app.config import settings
from app.events.topology import CENTRAL_FORWARD, declare_topology
from app.logging_config import setup_logging

logger = logging.getLogger("warehouse.forwarder")


class CentralUnavailable(Exception):
    """No se pudo entregar a la central: el mensaje se queda en la cola local."""


class CentralForwarder:
    def __init__(self, local_url: str, central_publisher, warehouse_id: str):
        self.local_url = local_url
        self.central = central_publisher
        self.warehouse_id = warehouse_id
        self.forwarded = 0

    def forward(self, body: bytes, properties, routing_key: str) -> None:
        """Publica UNA copia en la central (con confirmación). Lanza CentralUnavailable si no se pudo."""
        headers = dict(properties.headers or {}) if properties else {}
        headers.setdefault("warehouse_id", self.warehouse_id)
        expiration = getattr(properties, "expiration", None)
        try:
            self.central.publish(
                exchange=CENTRAL_EXCHANGES.events,
                routing_key=central_routing_key(self.warehouse_id, routing_key),
                body=body,
                message_id=getattr(properties, "message_id", None),
                correlation_id=getattr(properties, "correlation_id", None),
                message_type=getattr(properties, "type", None),
                headers=headers,
                expiration_ms=int(expiration) if expiration else None,
                persistent=getattr(properties, "delivery_mode", 2) == 2,
                mandatory=True,
            )
        except Exception as error:  # noqa: BLE001
            raise CentralUnavailable(f"{type(error).__name__}: {error}") from error
        self.forwarded += 1

    def _on_message(self, channel, method, properties, body):
        token = set_correlation_id(getattr(properties, "correlation_id", None))
        try:
            self.forward(body, properties, method.routing_key)
            channel.basic_ack(delivery_tag=method.delivery_tag)  # SOLO después de la confirmación central
        finally:
            reset_correlation_id(token)

    def run(self, stopper: GracefulStop) -> None:
        backoff = 1
        while not stopper.stop:
            local = None
            try:
                local = connect(self.local_url, f"central-forwarder-{self.warehouse_id}")
                channel = local.channel()
                declare_topology(channel)
                channel.basic_qos(prefetch_count=50)
                channel.basic_consume(queue=CENTRAL_FORWARD.name, on_message_callback=self._on_message, auto_ack=False)
                logger.info("Reenviando eventos a la central", extra={"event": "forwarder_started"})
                backoff = 1
                last_report = time.monotonic()
                while not stopper.stop:
                    local.process_data_events(time_limit=1)
                    if self.forwarded and time.monotonic() - last_report > 10:
                        logger.info("Eventos entregados a la central", extra={"event": "forwarded", "context": {"count": self.forwarded}})
                        self.forwarded = 0
                        last_report = time.monotonic()
            except CentralUnavailable as error:
                logger.warning(
                    "Central no disponible: los eventos quedan guardados en la cola local",
                    extra={"event": "central_unavailable", "context": {"error": str(error), "retry_in_seconds": backoff}},
                )
            except (AMQPError, OSError) as error:
                logger.warning(
                    "RabbitMQ local no disponible",
                    extra={"event": "rabbitmq_unavailable", "context": {"error": str(error) or type(error).__name__, "retry_in_seconds": backoff}},
                )
            finally:
                try:
                    if local is not None and local.is_open:
                        local.close()  # lo que no tuvo ack vuelve a la cola local
                except Exception:  # noqa: BLE001
                    pass
            if not stopper.stop:
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
        self.central.close()


def main() -> None:
    setup_logging(service="central-forwarder")
    if not settings.CENTRAL_AMQP_URL:
        logger.warning("CENTRAL_AMQP_URL no está configurada: esta bodega trabaja sola", extra={"event": "forwarder_disabled"})
        stopper = GracefulStop()
        while not stopper.stop:
            time.sleep(5)
        return
    publisher = ReliablePublisher(settings.CENTRAL_AMQP_URL, f"forwarder-{settings.WAREHOUSE_ID}", on_connect=declare_central_topology)
    CentralForwarder(settings.AMQP_URL, publisher, settings.WAREHOUSE_ID).run(GracefulStop())


if __name__ == "__main__":
    main()
