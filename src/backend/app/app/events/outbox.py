from sqlalchemy.orm import Session

from platform_common.correlation import get_correlation_id
from platform_common.events import build_event, new_event_id, routing_key_for

from app.config import settings
from app.events.models import OutboxEventEntity


def event_source() -> str:
    return f"warehouse-{settings.WAREHOUSE_ID}"


class OutboxWriter:
    """Agrega eventos al outbox. NO hace commit: el evento se confirma junto con el cambio de negocio."""

    def __init__(self, db: Session):
        self.db = db

    def add(self, event_type: str, aggregate_type: str, aggregate_id, payload: dict) -> OutboxEventEntity:
        event = OutboxEventEntity(
            event_id=new_event_id(),
            event_type=event_type,
            event_version=1,
            routing_key=routing_key_for(event_type),
            aggregate_type=aggregate_type,
            aggregate_id=str(aggregate_id),
            payload={"warehouse_id": settings.WAREHOUSE_ID, **payload},
            correlation_id=get_correlation_id(),
        )
        self.db.add(event)
        return event


def envelope_from_row(row: OutboxEventEntity) -> dict:
    """Convierte una fila del outbox en el sobre (envelope) que viaja por RabbitMQ."""
    return build_event(
        event_type=row.event_type,
        source=event_source(),
        payload=row.payload,
        warehouse_id=settings.WAREHOUSE_ID,
        correlation_id=row.correlation_id,
        sequence=row.sequence,
        event_id=row.event_id,
        occurred_at=row.occurred_at.isoformat() if row.occurred_at else None,
    )
