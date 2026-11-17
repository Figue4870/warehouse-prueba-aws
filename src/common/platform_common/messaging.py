"""
RabbitMQ CONFIABLE (sin pérdida de mensajes).

Tres garantías que implementa este módulo:

1) Publicar con CONFIRMACIÓN (publisher confirms)
   basic_publish no "dispara y olvida": espera a que RabbitMQ diga
   "lo guardé" (ack). Si RabbitMQ no confirma, se lanza una excepción y quien
   publica sabe que debe reintentar. Los mensajes son PERSISTENTES
   (delivery_mode=2) y las colas DURABLES: sobreviven a un reinicio del broker.

2) Consumir con ACK MANUAL
   El worker solo hace "ack" cuando TERMINÓ su trabajo. Si se cae a la mitad,
   RabbitMQ vuelve a entregar el mensaje a otro worker (o al mismo cuando vuelva).

3) Reintentos y Dead Letter Queue (DLQ)
       cola  --error-->  cola.retry (espera N s)  --vuelve-->  cola
       ... después de max_retries fallos ...       --> cola.dlq (se guarda para revisión)
   Un error PERMANENTE (mensaje corrupto, dato imposible) va directo a la DLQ.
   Nada se descarta: lo que no se pudo procesar queda en la DLQ y se puede
   reprocesar con scripts/dlq.py.
"""

import logging
import signal
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import pika
from pika.exceptions import AMQPError

logger = logging.getLogger("platform.messaging")


class PermanentError(Exception):
    """Error que NO se arregla reintentando (mensaje inválido, dato inexistente...). Va a la DLQ."""


@dataclass
class QueueSpec:
    name: str
    bindings: list[str]
    retry_delay_ms: int = 5000
    max_retries: int = 5
    with_retry: bool = True  # False = cola simple sin DLX (ej. la cola de reenvío a la central)
    arguments: dict = field(default_factory=dict)

    @property
    def retry_queue(self) -> str:
        return f"{self.name}.retry"

    @property
    def dlq(self) -> str:
        return f"{self.name}.dlq"


@dataclass
class Exchanges:
    events: str
    retry: str
    dlq: str


# ---------------------------------------------------------------- conexión
def connection_parameters(url: str, connection_name: str) -> pika.URLParameters:
    params = pika.URLParameters(url)
    params.heartbeat = 30
    params.blocked_connection_timeout = 60
    params.connection_attempts = 1
    params.socket_timeout = 5
    params.client_properties = {"connection_name": connection_name}
    return params


def connect(url: str, connection_name: str) -> pika.BlockingConnection:
    return pika.BlockingConnection(connection_parameters(url, connection_name))


# ---------------------------------------------------------------- topología
def declare_exchanges(channel, exchanges: Exchanges) -> None:
    channel.exchange_declare(exchange=exchanges.events, exchange_type="topic", durable=True)
    channel.exchange_declare(exchange=exchanges.retry, exchange_type="direct", durable=True)
    channel.exchange_declare(exchange=exchanges.dlq, exchange_type="direct", durable=True)


def declare_queue(channel, spec: QueueSpec, exchanges: Exchanges) -> None:
    """Declara la cola principal y, si aplica, su cola de reintento y su DLQ."""
    args = dict(spec.arguments)
    if spec.with_retry:
        # Cuando el worker hace nack(requeue=False), el mensaje "muere" y va al exchange de reintento
        args.update({"x-dead-letter-exchange": exchanges.retry, "x-dead-letter-routing-key": spec.name})
    channel.queue_declare(queue=spec.name, durable=True, arguments=args)
    for binding in spec.bindings:
        channel.queue_bind(queue=spec.name, exchange=exchanges.events, routing_key=binding)

    if spec.with_retry:
        # cola.retry: el mensaje espera retry_delay_ms y luego vuelve a la cola principal
        channel.queue_declare(
            queue=spec.retry_queue,
            durable=True,
            arguments={
                "x-message-ttl": spec.retry_delay_ms,
                "x-dead-letter-exchange": "",
                "x-dead-letter-routing-key": spec.name,
            },
        )
        channel.queue_bind(queue=spec.retry_queue, exchange=exchanges.retry, routing_key=spec.name)
        # cola.dlq: aquí terminan los mensajes que fallaron demasiadas veces
        channel.queue_declare(queue=spec.dlq, durable=True)
        channel.queue_bind(queue=spec.dlq, exchange=exchanges.dlq, routing_key=spec.name)


def death_count(properties, queue_name: str) -> int:
    """Cuántas veces este mensaje fue rechazado por `queue_name` (cabecera x-death de RabbitMQ)."""
    headers = (properties.headers or {}) if properties else {}
    for death in headers.get("x-death", []) or []:
        if death.get("queue") == queue_name and death.get("reason") == "rejected":
            return int(death.get("count", 0))
    return 0


# ---------------------------------------------------------------- publicar
class ReliablePublisher:
    """
    Publica con publisher confirms. publish() solo retorna cuando RabbitMQ
    confirmó que guardó el mensaje; si no, lanza una excepción.
    """

    def __init__(self, url: str, connection_name: str, on_connect: Optional[Callable] = None):
        self.url = url
        self.connection_name = connection_name
        self.on_connect = on_connect  # ej. declarar la topología al conectar
        self._connection = None
        self._channel = None

    def _ensure_channel(self):
        if self._channel is not None and self._channel.is_open and self._connection.is_open:
            return self._channel
        self.close()
        self._connection = connect(self.url, self.connection_name)
        self._channel = self._connection.channel()
        self._channel.confirm_delivery()
        if self.on_connect:
            self.on_connect(self._channel)
        logger.info("Conectado a RabbitMQ", extra={"event": "rabbitmq_connected", "context": {"connection": self.connection_name}})
        return self._channel

    def publish(
        self,
        exchange: str,
        routing_key: str,
        body: bytes,
        message_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        message_type: Optional[str] = None,
        headers: Optional[dict] = None,
        expiration_ms: Optional[int] = None,
        persistent: bool = True,
        mandatory: bool = True,
    ) -> None:
        properties = pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2 if persistent else 1,
            message_id=message_id,
            correlation_id=correlation_id,
            type=message_type,
            headers=headers or {},
            timestamp=int(time.time()),
            expiration=str(expiration_ms) if expiration_ms else None,
        )
        channel = self._ensure_channel()
        try:
            # Con confirm_delivery(), esta llamada ESPERA el ack del broker.
            # mandatory=True: si ninguna cola recibe el mensaje -> UnroutableError (no se pierde en silencio)
            channel.basic_publish(exchange=exchange, routing_key=routing_key, body=body, properties=properties, mandatory=mandatory)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        try:
            if self._connection is not None and self._connection.is_open:
                self._connection.close()
        except Exception:  # noqa: BLE001 - cerrar nunca debe fallar
            pass
        self._connection = None
        self._channel = None


# ---------------------------------------------------------------- consumir
class GracefulStop:
    """Atiende SIGTERM/SIGINT (docker stop, Ctrl+C) para terminar el mensaje actual y salir limpio."""

    def __init__(self) -> None:
        self.stop = False
        signal.signal(signal.SIGTERM, self._handler)
        signal.signal(signal.SIGINT, self._handler)

    def _handler(self, *_):
        self.stop = True


class Consumer:
    """
    Worker genérico: consume una cola con ack manual, reintentos y DLQ.

    handler(body, properties, routing_key) -> None
      - termina bien           -> ack
      - lanza PermanentError   -> se copia a la DLQ y ack
      - lanza otra excepción   -> nack -> cola.retry -> vuelve en retry_delay_ms
                                  (después de max_retries -> DLQ)
    """

    def __init__(
        self,
        url: str,
        connection_name: str,
        spec: QueueSpec,
        exchanges: Exchanges,
        handler: Callable,
        declare_topology: Callable,
        prefetch: int = 10,
    ):
        self.url = url
        self.connection_name = connection_name
        self.spec = spec
        self.exchanges = exchanges
        self.handler = handler
        self.declare_topology = declare_topology
        self.prefetch = prefetch
        self.dlq_publisher = ReliablePublisher(url, f"{connection_name}-dlq")

    def _send_to_dlq(self, body: bytes, properties, routing_key: str, reason: str) -> None:
        headers = dict(properties.headers or {}) if properties else {}
        headers.pop("x-death", None)
        headers.update({"x-error": reason[:500], "x-original-routing-key": routing_key, "x-original-queue": self.spec.name})
        self.dlq_publisher.publish(
            exchange=self.exchanges.dlq,
            routing_key=self.spec.name,
            body=body,
            message_id=getattr(properties, "message_id", None),
            correlation_id=getattr(properties, "correlation_id", None),
            message_type=getattr(properties, "type", None),
            headers=headers,
        )
        logger.error(
            "Mensaje enviado a la DLQ",
            extra={"event": "message_dead_lettered", "context": {"queue": self.spec.name, "reason": reason[:300], "message_id": getattr(properties, "message_id", None)}},
        )

    def _on_message(self, channel, method, properties, body):
        try:
            self.handler(body, properties, method.routing_key)
            channel.basic_ack(delivery_tag=method.delivery_tag)
        except PermanentError as error:
            self._send_to_dlq(body, properties, method.routing_key, f"permanent: {error}")
            channel.basic_ack(delivery_tag=method.delivery_tag)
        except Exception as error:  # noqa: BLE001 - cualquier otro error se reintenta
            attempts = death_count(properties, self.spec.name) + 1
            if not self.spec.with_retry:
                # Sin cola de reintento: se devuelve a la cola y se reintenta en la siguiente vuelta
                logger.warning("Error procesando; se reintentará", extra={"event": "message_requeued", "context": {"queue": self.spec.name, "error": str(error)}})
                channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                time.sleep(1)
                return
            if attempts > self.spec.max_retries:
                self._send_to_dlq(body, properties, method.routing_key, f"max_retries ({self.spec.max_retries}): {error}")
                channel.basic_ack(delivery_tag=method.delivery_tag)
            else:
                logger.warning(
                    "Error procesando mensaje; irá a la cola de reintento",
                    extra={"event": "message_retry", "context": {"queue": self.spec.name, "attempt": attempts, "max_retries": self.spec.max_retries, "error": str(error)}},
                )
                channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    def run(self, stopper: Optional[GracefulStop] = None) -> None:
        stopper = stopper or GracefulStop()
        backoff = 1
        while not stopper.stop:
            connection = None
            try:
                connection = connect(self.url, self.connection_name)
                channel = connection.channel()
                self.declare_topology(channel)
                channel.basic_qos(prefetch_count=self.prefetch)
                channel.basic_consume(queue=self.spec.name, on_message_callback=self._on_message, auto_ack=False)
                logger.info("Worker escuchando", extra={"event": "worker_started", "context": {"queue": self.spec.name}})
                backoff = 1
                while not stopper.stop:
                    connection.process_data_events(time_limit=1)
            except (AMQPError, OSError) as error:
                logger.warning(
                    "Sin conexión con RabbitMQ; reintentando",
                    extra={"event": "rabbitmq_unavailable", "context": {"queue": self.spec.name, "error": str(error) or type(error).__name__, "retry_in_seconds": backoff}},
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
            finally:
                try:
                    if connection is not None and connection.is_open:
                        connection.close()
                except Exception:  # noqa: BLE001
                    pass
        self.dlq_publisher.close()
        logger.info("Worker detenido", extra={"event": "worker_stopped", "context": {"queue": self.spec.name}})
