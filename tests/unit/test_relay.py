"""Fase 3: el relay publica el outbox SIN perder ni desordenar eventos."""

from datetime import datetime, timedelta, timezone

import pytest

from platform_common.events import parse_event

from app.events.models import OutboxEventEntity
from app.workers.relay import OutboxRelay


class FakePublisher:
    """Imita RabbitMQ. fail_on = número de publicación (1, 2, 3...) que debe fallar."""

    def __init__(self, fail_on=None):
        self.published = []
        self.fail_on = set(fail_on or [])
        self.calls = 0

    def publish(self, **kwargs):
        self.calls += 1
        if self.calls in self.fail_on:
            raise ConnectionError("RabbitMQ caído (simulado)")
        self.published.append(kwargs)


def outbox(session_factory):
    with session_factory() as db:
        return db.query(OutboxEventEntity).order_by(OutboxEventEntity.id).all()


@pytest.fixture
def five_events(client, create_product, move):
    product = create_product()           # 1 evento
    for _ in range(4):
        move(product["id"], "RECEIPT", 1)  # 4 eventos
    return product


def test_publica_en_orden_con_secuencia_sin_huecos(five_events, session_factory):
    publisher = FakePublisher()
    assert OutboxRelay(session_factory, publisher).run_once() == 5
    sequences = [parse_event(p["body"]).sequence for p in publisher.published]
    assert sequences == [1, 2, 3, 4, 5]
    assert all(row.published_at is not None for row in outbox(session_factory))
    first = parse_event(publisher.published[0]["body"])
    assert first.event_type == "PRODUCT_CREATED" and first.source == "warehouse-WH-TEST"
    assert publisher.published[0]["routing_key"] == "product.created"
    assert publisher.published[0]["message_id"] == first.event_id  # message_id = event_id (idempotencia)


def test_si_rabbitmq_falla_nada_se_pierde_ni_se_desordena(five_events, session_factory):
    publisher = FakePublisher(fail_on=[3])
    relay = OutboxRelay(session_factory, publisher)
    with pytest.raises(ConnectionError):
        relay.run_once()
    rows = outbox(session_factory)
    assert [r.published_at is not None for r in rows] == [True, True, False, False, False]
    assert rows[2].last_error.startswith("ConnectionError") and rows[2].attempts == 1

    # RabbitMQ "vuelve": se publica el resto, con las MISMAS secuencias y en orden
    assert relay.run_once() == 3
    assert [parse_event(p["body"]).sequence for p in publisher.published] == [1, 2, 3, 4, 5]
    assert all(r.published_at is not None for r in outbox(session_factory))


def test_la_secuencia_continua_entre_vueltas(five_events, client, move, session_factory):
    publisher = FakePublisher()
    relay = OutboxRelay(session_factory, publisher)
    relay.run_once()
    move(five_events["id"], "RECEIPT", 1)
    relay.run_once()
    assert parse_event(publisher.published[-1]["body"]).sequence == 6


def test_lotes_de_tamano_limitado(five_events, session_factory):
    publisher = FakePublisher()
    relay = OutboxRelay(session_factory, publisher, batch_size=2)
    assert relay.run_once() == 2
    assert relay.run_once() == 2
    assert relay.run_once() == 1
    assert relay.run_once() == 0


def test_limpieza_solo_borra_publicados_viejos(five_events, session_factory):
    relay = OutboxRelay(session_factory, FakePublisher(fail_on=[5]))
    with pytest.raises(ConnectionError):
        relay.run_once()  # 4 publicados, 1 pendiente
    with session_factory() as db:
        for row in db.query(OutboxEventEntity).all():
            if row.published_at is not None:
                row.published_at = datetime.now(timezone.utc) - timedelta(days=40)
        db.commit()
    assert relay.cleanup(retention_days=30) == 4
    remaining = outbox(session_factory)
    assert len(remaining) == 1 and remaining[0].published_at is None  # el pendiente NUNCA se borra
