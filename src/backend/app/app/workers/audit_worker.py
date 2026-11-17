"""
AUDIT WORKER — deja un rastro de auditoría de cada evento de negocio.

Escribe una línea JSON por evento (event="audit_event") con su tipo, id,
secuencia y correlation_id. Así se puede reconstruir "qué pasó y en qué orden"
buscando en los logs, sin tocar la base operacional.
"""

import logging

from app.workers.base import parse_or_permanent, run_consumer

logger = logging.getLogger("warehouse.audit")


def handle(body, properties, routing_key):
    event = parse_or_permanent(body)
    logger.info(
        "Evento auditado",
        extra={
            "event": "audit_event",
            "context": {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "sequence": event.sequence,
                "aggregate": event.payload.get("sku") or event.payload.get("job_id"),
                "occurred_at": event.occurred_at,
            },
        },
    )


def main() -> None:
    run_consumer("audit", handle, "audit-worker")


if __name__ == "__main__":
    main()
