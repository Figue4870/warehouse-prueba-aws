"""
OUTBOX RELAY: lleva los eventos del outbox (PostgreSQL) a RabbitMQ.

Garantías:
  * Un evento solo se marca como publicado DESPUÉS de que RabbitMQ confirmó
    que lo guardó (publisher confirm). Si RabbitMQ está caído, el evento sigue
    pendiente en PostgreSQL y se reintenta: NO SE PIERDE.
  * Si el relay se cae justo después de publicar y antes de marcar, al volver
    publicará el evento otra vez. Por eso los consumidores son IDEMPOTENTES
    (ignoran un event_id que ya procesaron).
  * Le pone a cada evento un número de secuencia (1, 2, 3...) SIN HUECOS.
    La central usa esa secuencia para detectar si le falta algún evento.
  * Un advisory lock de PostgreSQL asegura que, aunque haya dos relays
    corriendo, solo uno publica a la vez (el otro queda de reserva).
"""

import logging
import time
from datetime import datetime, timedelta, timezone

from platform_common.events import build_event, encode_event, new_event_id
from platform_common.messaging import GracefulStop, ReliablePublisher
from sqlalchemy import func, text
from sqlalchemy.orm import Session, sessionmaker

import app.all_models  # noqa: F401 - registra todos los modelos (ver all_models.py)
from app.config import settings
from app.database import SessionLocal
from app.events.models import OutboxEventEntity
from app.events.outbox import envelope_from_row
from app.events import types as events
from app.events.outbox import event_source
from app.events.topology import EXCHANGES, central_enabled, declare_topology
from app.logging_config import setup_logging

logger = logging.getLogger("warehouse.relay")

RELAY_LOCK_KEY = 4242001  # número arbitrario para el advisory lock


def _lock(db: Session) -> None:
    if db.bind.dialect.name == "postgresql":
        # Bloqueo a nivel de transacción: se libera solo en COMMIT/ROLLBACK
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": RELAY_LOCK_KEY})


class OutboxRelay:
    def __init__(self, session_factory: sessionmaker, publisher, batch_size: int = 100):
        self.session_factory = session_factory
        self.publisher = publisher
        self.batch_size = batch_size

    def assign_sequences(self) -> int:
        """Paso 1 (transacción propia): numerar los pendientes en orden de llegada."""
        with self.session_factory() as db:
            _lock(db)
            pending = (
                db.query(OutboxEventEntity)
                .filter(OutboxEventEntity.sequence.is_(None))
                .order_by(OutboxEventEntity.id)
                .limit(self.batch_size)
                .with_for_update()
                .all()
            )
            if not pending:
                db.rollback()
                return 0
            next_sequence = (db.query(func.max(OutboxEventEntity.sequence)).scalar() or 0) + 1
            for row in pending:
                row.sequence = next_sequence
                next_sequence += 1
            db.commit()
            return len(pending)

    def publish_pending(self) -> int:
        """Paso 2: publicar en orden de secuencia y marcar SOLO lo confirmado."""
        published = 0
        with self.session_factory() as db:
            _lock(db)
            rows = (
                db.query(OutboxEventEntity)
                .filter(OutboxEventEntity.published_at.is_(None), OutboxEventEntity.sequence.isnot(None))
                .order_by(OutboxEventEntity.sequence)
                .limit(self.batch_size)
                .with_for_update()
                .all()
            )
            for row in rows:
                row.attempts = (row.attempts or 0) + 1
                try:
                    self.publisher.publish(
                        exchange=EXCHANGES.events,
                        routing_key=row.routing_key,
                        body=encode_event(envelope_from_row(row)),
                        message_id=row.event_id,
                        correlation_id=row.correlation_id,
                        message_type=row.event_type,
                        headers={"warehouse_id": settings.WAREHOUSE_ID, "sequence": row.sequence, "event_version": row.event_version},
                    )
                except Exception as error:  # noqa: BLE001 - RabbitMQ caído, red, etc.
                    row.last_error = f"{type(error).__name__}: {error}"[:500]
                    db.commit()  # guarda el intento fallido y lo publicado hasta ahora
                    raise
                row.published_at = datetime.now(timezone.utc)
                row.last_error = None
                published += 1
            db.commit()
        return published

    def run_once(self) -> int:
        self.assign_sequences()
        return self.publish_pending()

    def heartbeat_payload(self) -> dict:
        with self.session_factory() as db:
            last_sequence = (
                db.query(func.max(OutboxEventEntity.sequence)).filter(OutboxEventEntity.published_at.isnot(None)).scalar() or 0
            )
            pending = db.query(func.count(OutboxEventEntity.id)).filter(OutboxEventEntity.published_at.is_(None)).scalar()
        return {
            "last_sequence": last_sequence,   # la central compara esto con lo que ya recibió
            "outbox_pending": pending,
            "api_url": settings.ADVERTISED_API_URL or f"http://lb_warehouse_{settings.WAREHOUSE_ID}:80",
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }

    def send_heartbeat(self) -> None:
        """
        Fase 7: "sigo viva y he publicado hasta la secuencia N". NO pasa por el outbox:
        un heartbeat viejo no sirve, por eso expira a los 60 s si no se entrega.
        """
        event = build_event(events.WAREHOUSE_HEARTBEAT, event_source(), self.heartbeat_payload(), warehouse_id=settings.WAREHOUSE_ID)
        self.publisher.publish(
            exchange=EXCHANGES.events,
            routing_key="warehouse.heartbeat",
            body=encode_event(event),
            message_id=new_event_id(),
            message_type=events.WAREHOUSE_HEARTBEAT,
            headers={"warehouse_id": settings.WAREHOUSE_ID},
            expiration_ms=60_000,
            persistent=False,
            mandatory=False,
        )

    def cleanup(self, retention_days: int) -> int:
        """Borra eventos YA publicados más viejos que retention_days (los pendientes nunca se borran)."""
        limit = datetime.now(timezone.utc) - timedelta(days=retention_days)
        with self.session_factory() as db:
            deleted = (
                db.query(OutboxEventEntity)
                .filter(OutboxEventEntity.published_at.isnot(None), OutboxEventEntity.published_at < limit)
                .delete(synchronize_session=False)
            )
            db.commit()
        return deleted


def main() -> None:
    setup_logging(service="outbox-relay")
    stopper = GracefulStop()
    publisher = ReliablePublisher(settings.AMQP_URL, f"outbox-relay-{settings.WAREHOUSE_ID}", on_connect=declare_topology)
    relay = OutboxRelay(SessionLocal, publisher, settings.OUTBOX_BATCH_SIZE)
    logger.info("Relay iniciado", extra={"event": "relay_started"})
    backoff = 1.0
    last_cleanup = 0.0
    last_heartbeat = 0.0
    while not stopper.stop:
        try:
            published = relay.run_once()
            backoff = 1.0
            if central_enabled() and time.monotonic() - last_heartbeat >= settings.HEARTBEAT_SECONDS:
                relay.send_heartbeat()
                last_heartbeat = time.monotonic()
            if published:
                logger.info("Eventos publicados", extra={"event": "outbox_published", "context": {"count": published}})
            if time.monotonic() - last_cleanup > 3600:
                relay.cleanup(settings.OUTBOX_RETENTION_DAYS)
                last_cleanup = time.monotonic()
            if published < settings.OUTBOX_BATCH_SIZE:
                time.sleep(settings.OUTBOX_POLL_SECONDS)
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "No se pudo publicar; los eventos siguen guardados en el outbox",
                extra={"event": "outbox_publish_failed", "context": {"error": f"{type(error).__name__}: {error}", "retry_in_seconds": backoff}},
            )
            publisher.close()
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
    publisher.close()
    logger.info("Relay detenido", extra={"event": "relay_stopped"})


if __name__ == "__main__":
    main()
