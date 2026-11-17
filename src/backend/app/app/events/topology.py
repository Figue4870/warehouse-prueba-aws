"""
Topología de RabbitMQ DENTRO de una bodega (Fase 3).

                         exchange "warehouse.events" (topic)
                                       |
   +-------------------+---------------+-----------------+------------------+
   |                   |                                 |                  |
 wh.cache-invalidation wh.notifications           wh.reports           wh.audit
 inventory.movement.*  stock.depleted             report.requested     product.#, inventory.#,
 product.deactivated   stock.discrepancy                               stock.#, report.#
   |                   |                                 |                  |
 cache-worker       notification-worker            report-worker       audit-worker

 Fase 7: + wh.central-forward (#) -> central-forwarder -> RabbitMQ CENTRAL
   Cola SIN reintentos ni DLQ: si la central no responde, los mensajes simplemente
   ESPERAN aquí (store-and-forward) hasta que vuelva. Solo existe si CENTRAL_AMQP_URL
   está configurada.

Cada cola tiene su ".retry" (espera y reintenta) y su ".dlq" (mensajes que
fallaron demasiadas veces). Todas son DURABLES: si RabbitMQ se reinicia o un
worker está apagado, los mensajes esperan en la cola.
"""

from platform_common.messaging import Exchanges, QueueSpec, declare_exchanges, declare_queue

from app.config import settings

EXCHANGES = Exchanges(events="warehouse.events", retry="warehouse.retry", dlq="warehouse.dlq")


def _spec(name: str, bindings: list[str], max_retries: int | None = None) -> QueueSpec:
    return QueueSpec(
        name=name,
        bindings=bindings,
        retry_delay_ms=settings.WORKER_RETRY_DELAY_MS,
        max_retries=settings.WORKER_MAX_RETRIES if max_retries is None else max_retries,
    )


QUEUES = {
    "cache": _spec("wh.cache-invalidation", ["inventory.movement.recorded", "product.deactivated"]),
    "notifications": _spec("wh.notifications", ["stock.depleted", "stock.discrepancy"]),
    "reports": _spec("wh.reports", ["report.requested"], max_retries=3),
    "audit": _spec("wh.audit", ["product.#", "inventory.#", "stock.#", "report.#", "transfer.#"]),
}


CENTRAL_FORWARD = QueueSpec(name="wh.central-forward", bindings=["#"], with_retry=False)


def central_enabled() -> bool:
    return bool(settings.CENTRAL_AMQP_URL)


def declare_topology(channel) -> None:
    """Idempotente: declarar algo que ya existe no lo cambia. La llaman el relay y todos los workers."""
    declare_exchanges(channel, EXCHANGES)
    for spec in QUEUES.values():
        declare_queue(channel, spec, EXCHANGES)
    if central_enabled():
        declare_queue(channel, CENTRAL_FORWARD, EXCHANGES)
