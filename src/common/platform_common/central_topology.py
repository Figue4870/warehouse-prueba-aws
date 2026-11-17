"""
Topología del RabbitMQ CENTRAL (Fase 7). La usan:
  - el central-forwarder de cada bodega (publica aquí los eventos de su bodega), y
  - los workers de la plataforma central (los consumen).

Las dos partes declaran lo mismo (es idempotente), así que da igual quién
arranque primero: los mensajes nunca llegan a un exchange sin cola.

  exchange "central.events" (topic)
     routing key = "<WAREHOUSE_ID>.<routing key original>"
       ej. "WH-002.inventory.movement.recorded"
     |
     +-- central.projection  (#)              -> projector: construye el modelo de lectura (CQRS)
     +-- central.saga        (*.transfer.#)   -> orquestador de transferencias (Fase 6)
"""

from platform_common.messaging import Exchanges, QueueSpec, declare_exchanges, declare_queue

CENTRAL_EXCHANGES = Exchanges(events="central.events", retry="central.retry", dlq="central.dlq")

PROJECTION_QUEUE = QueueSpec(name="central.projection", bindings=["#"], retry_delay_ms=5000, max_retries=5)
SAGA_QUEUE = QueueSpec(name="central.saga", bindings=["*.transfer.#"], retry_delay_ms=5000, max_retries=5)


def central_routing_key(warehouse_id: str, routing_key: str) -> str:
    return f"{warehouse_id}.{routing_key}"


def declare_central_topology(channel) -> None:
    """Declara las DOS colas que leen de central.events: así ningún evento llega sin su cola."""
    declare_exchanges(channel, CENTRAL_EXCHANGES)
    declare_queue(channel, PROJECTION_QUEUE, CENTRAL_EXCHANGES)
    declare_queue(channel, SAGA_QUEUE, CENTRAL_EXCHANGES)


# =====================================================================
# Fase 6: COMANDOS de la Saga (central -> bodegas) y RESPUESTAS (bodegas -> central)
#
#   exchange "central.commands" (topic)
#     routing key = "<WAREHOUSE_ID>"   ej. "WH-002"
#     |
#     +-- commands.WH-001  -> command-worker de WH-001 (se conecta al RabbitMQ CENTRAL)
#     +-- commands.WH-002  -> command-worker de WH-002 ...
#
#   Las respuestas NO usan un camino nuevo: la bodega las escribe en su OUTBOX
#   (misma transacción que el movimiento) y viajan como cualquier evento
#   (relay -> forwarder -> central.events). La central las recibe en:
#
#     central.saga  (*.transfer.#)  -> orquestador de la Saga
#
#   Una cola de comandos por bodega: si una bodega está caída, sus comandos la
#   ESPERAN en la central (durables) y las demás bodegas no se enteran.
# =====================================================================

COMMAND_EXCHANGES = Exchanges(events="central.commands", retry="central.commands.retry", dlq="central.commands.dlq")

# Los dos lados (central y bodega) declaran la cola de comandos con EXACTAMENTE
# los mismos argumentos (si no, RabbitMQ responde PRECONDITION_FAILED): por eso
# los valores están fijos aquí y no en la configuración de cada uno.
COMMAND_RETRY_DELAY_MS = 5000
COMMAND_MAX_RETRIES = 10


def command_queue(warehouse_id: str) -> QueueSpec:
    return QueueSpec(name=f"commands.{warehouse_id}", bindings=[warehouse_id],
                     retry_delay_ms=COMMAND_RETRY_DELAY_MS, max_retries=COMMAND_MAX_RETRIES)


def declare_command_topology(channel, warehouse_id: str) -> None:
    declare_exchanges(channel, COMMAND_EXCHANGES)
    declare_queue(channel, command_queue(warehouse_id), COMMAND_EXCHANGES)
