from sqlalchemy import JSON, BigInteger, Column, DateTime, Integer, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base
from app.modules.shared.utils import schemas_utils

# JSONB en PostgreSQL, JSON en SQLite (pruebas unitarias)
JsonType = JSON().with_variant(JSONB(), "postgresql")
# BIGSERIAL en PostgreSQL; en SQLite solo INTEGER PRIMARY KEY se autoincrementa
BigId = BigInteger().with_variant(Integer, "sqlite")


class OutboxEventEntity(Base):
    """
    TRANSACTIONAL OUTBOX (Fase 3).

    Cada cambio importante (movimiento, producto, alerta, reporte) escribe aquí
    un evento DENTRO DE LA MISMA TRANSACCIÓN que el cambio. Así es imposible que
    el inventario cambie y el evento se pierda (o al revés): o se guardan los dos,
    o ninguno.

    Luego el RELAY (app/workers/relay.py) lee los pendientes y los publica en
    RabbitMQ. Si RabbitMQ (o la central) está caído, los eventos simplemente
    esperan aquí, guardados en PostgreSQL, hasta que se puedan enviar.
    """

    __tablename__ = "outbox_events"

    id = Column(BigId, primary_key=True)
    event_id = Column(String(36), nullable=False, unique=True)
    event_type = Column(String(60), nullable=False)
    event_version = Column(SmallInteger, nullable=False, default=1)
    routing_key = Column(String(120), nullable=False)
    aggregate_type = Column(String(40), nullable=False)
    aggregate_id = Column(String(80), nullable=False)
    payload = Column(JsonType, nullable=False)
    correlation_id = Column(String(80))
    occurred_at = Column(DateTime(timezone=True), nullable=False, default=schemas_utils.current_utc_time)
    sequence = Column(BigInteger, unique=True)  # lo asigna el relay: 1, 2, 3... sin huecos
    published_at = Column(DateTime(timezone=True))  # NULL = pendiente de publicar
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(Text)
