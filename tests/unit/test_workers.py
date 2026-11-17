"""Fase 3: lógica de los workers (sin RabbitMQ real)."""

from types import SimpleNamespace

import pytest

from platform_common.events import build_event, encode_event
from platform_common.messaging import Consumer, Exchanges, PermanentError, QueueSpec, death_count

from app.modules.alerts.models import InventoryAlertEntity
from app.modules.reports.models import ReportJobEntity
from app.workers import cache_worker, notification_worker, report_worker


def event_body(event_type, payload):
    return encode_event(build_event(event_type, "warehouse-WH-TEST", payload, warehouse_id="WH-TEST"))


# ------------------------------------------------------------ Consumer: reintentos y DLQ
class FakeChannel:
    def __init__(self):
        self.acks, self.nacks = [], []

    def basic_ack(self, delivery_tag):
        self.acks.append(delivery_tag)

    def basic_nack(self, delivery_tag, requeue):
        self.nacks.append((delivery_tag, requeue))


class FakeDlqPublisher:
    def __init__(self):
        self.messages = []

    def publish(self, **kwargs):
        self.messages.append(kwargs)


def make_consumer(handler, max_retries=2):
    consumer = Consumer("amqp://x", "test", QueueSpec("wh.test", ["#"], max_retries=max_retries),
                        Exchanges("ev", "retry", "dlq"), handler, declare_topology=lambda ch: None)
    consumer.dlq_publisher = FakeDlqPublisher()
    return consumer


def deliver(consumer, deaths=0):
    channel = FakeChannel()
    headers = {"x-death": [{"queue": "wh.test", "reason": "rejected", "count": deaths}]} if deaths else {}
    props = SimpleNamespace(headers=headers, message_id="m-1", correlation_id=None, type="X")
    consumer._on_message(channel, SimpleNamespace(delivery_tag=7, routing_key="a.b"), props, b"{}")
    return channel


def test_exito_hace_ack():
    channel = deliver(make_consumer(lambda *a: None))
    assert channel.acks == [7] and channel.nacks == []


def test_error_transitorio_va_a_reintento():
    def fail(*a):
        raise RuntimeError("BD ocupada")

    consumer = make_consumer(fail)
    channel = deliver(consumer, deaths=1)
    assert channel.nacks == [(7, False)]  # nack sin requeue -> cola .retry
    assert consumer.dlq_publisher.messages == []


def test_demasiados_fallos_van_a_la_dlq_sin_perderse():
    def fail(*a):
        raise RuntimeError("sigue fallando")

    consumer = make_consumer(fail, max_retries=2)
    channel = deliver(consumer, deaths=2)  # tercer intento
    assert channel.acks == [7]
    [dead] = consumer.dlq_publisher.messages
    assert dead["exchange"] == "dlq" and dead["routing_key"] == "wh.test"
    assert dead["headers"]["x-original-routing-key"] == "a.b"
    assert "max_retries" in dead["headers"]["x-error"]


def test_error_permanente_va_directo_a_la_dlq():
    def bad(*a):
        raise PermanentError("mensaje corrupto")

    consumer = make_consumer(bad)
    channel = deliver(consumer)
    assert channel.acks == [7] and len(consumer.dlq_publisher.messages) == 1


def test_death_count_lee_la_cabecera_de_rabbitmq():
    props = SimpleNamespace(headers={"x-death": [
        {"queue": "wh.test.retry", "reason": "expired", "count": 3},
        {"queue": "wh.test", "reason": "rejected", "count": 3},
    ]})
    assert death_count(props, "wh.test") == 3
    assert death_count(SimpleNamespace(headers=None), "wh.test") == 0


# ------------------------------------------------------------ report worker
def test_report_worker_genera_el_reporte_y_es_idempotente(client, create_product, move, session_factory):
    product = create_product(sku="REP-1", retail_price="100", supplier_cost="60")
    move(product["id"], "RECEIPT", 10)
    move(product["id"], "SHIPMENT", 3)
    job_id = client.post("/reports", json={"type": "STOCK_VALUATION"}).json()["job_id"]
    handler = report_worker.make_handler(session_factory)
    body = event_body("REPORT_REQUESTED", {"job_id": job_id})

    handler(body, None, "report.requested")
    job = client.get(f"/reports/jobs/{job_id}").json()
    assert job["status"] == "COMPLETED" and job["attempts"] == 1
    [row] = job["result"]["rows"]
    assert row["sku"] == "REP-1" and row["on_hand"] == 7 and row["retail_value"] == "700.00"

    handler(body, None, "report.requested")  # mensaje duplicado
    assert client.get(f"/reports/jobs/{job_id}").json()["attempts"] == 1


def test_report_worker_trabajo_inexistente_es_error_permanente(session_factory):
    with pytest.raises(PermanentError):
        report_worker.make_handler(session_factory)(event_body("REPORT_REQUESTED", {"job_id": "JOB-NO"}), None, "")


def test_report_worker_mensaje_corrupto_es_error_permanente(session_factory):
    with pytest.raises(PermanentError):
        report_worker.make_handler(session_factory)(b"esto no es json", None, "")


def test_reporte_de_movimientos_calcula_ventas(client, create_product, move, session_factory):
    product = create_product(sku="REP-2", retail_price="50")
    move(product["id"], "RECEIPT", 10)
    move(product["id"], "SHIPMENT", 4)
    job_id = client.post("/reports", json={"type": "MOVEMENTS_SUMMARY"}).json()["job_id"]
    report_worker.make_handler(session_factory)(event_body("REPORT_REQUESTED", {"job_id": job_id}), None, "")
    result = client.get(f"/reports/jobs/{job_id}").json()["result"]
    assert result["totals"]["units_by_type"] == {"RECEIPT": 10, "SHIPMENT": 4}
    assert result["totals"]["sales_value"] == "200.00"


# ------------------------------------------------------------ notification worker
def first_alert(session_factory):
    with session_factory() as db:
        return db.query(InventoryAlertEntity).first()


def test_notificacion_marca_la_alerta_y_no_repite(client, create_product, move, session_factory):
    product = create_product(sku="NOT-1")
    move(product["id"], "RECEIPT", 1)
    move(product["id"], "SHIPMENT", 1)  # se agota -> alerta
    alert = first_alert(session_factory)
    sent = []
    handler = notification_worker.make_handler(session_factory, webhook_url="http://webhook", sender=lambda url, msg: sent.append(msg))
    body = event_body("STOCK_DEPLETED", {"alert_id": alert.id, "sku": "NOT-1"})
    handler(body, None, "stock.depleted")
    handler(body, None, "stock.depleted")  # duplicado
    assert len(sent) == 1 and sent[0]["sku"] == "NOT-1"
    notified = first_alert(session_factory)
    assert notified.notified_at is not None and notified.notification_channel == "webhook"
    assert client.get("/alerts").json()[0]["notified_at"] is not None


def test_si_el_webhook_falla_se_reintenta(client, create_product, move, session_factory):
    product = create_product()
    move(product["id"], "RECEIPT", 1)
    move(product["id"], "SHIPMENT", 1)
    alert = first_alert(session_factory)

    def broken(url, msg):
        raise ConnectionError("webhook caído")

    handler = notification_worker.make_handler(session_factory, webhook_url="http://webhook", sender=broken)
    with pytest.raises(ConnectionError):  # Consumer lo convierte en reintento
        handler(event_body("STOCK_DEPLETED", {"alert_id": alert.id}), None, "")
    assert first_alert(session_factory).notified_at is None


# ------------------------------------------------------------ cache worker
def test_cache_worker_borra_la_clave(cache, fake_redis, monkeypatch):
    monkeypatch.setattr(cache_worker.settings, "CACHE_INVALIDATION_MODE", "async")
    fake_redis.data["warehouse:WH-TEST:inventory:9"] = "{}"
    cache_worker.make_handler(cache)(event_body("INVENTORY_MOVEMENT_RECORDED", {"product_id": 9}), None, "")
    assert "warehouse:WH-TEST:inventory:9" not in fake_redis.data


def test_cache_worker_en_modo_sync_no_hace_nada(cache, fake_redis, monkeypatch):
    monkeypatch.setattr(cache_worker.settings, "CACHE_INVALIDATION_MODE", "sync")
    fake_redis.data["warehouse:WH-TEST:inventory:9"] = "{}"
    cache_worker.make_handler(cache)(event_body("INVENTORY_MOVEMENT_RECORDED", {"product_id": 9}), None, "")
    assert "warehouse:WH-TEST:inventory:9" in fake_redis.data


def test_cache_worker_con_redis_caido_pide_reintento(cache, fake_redis, monkeypatch):
    monkeypatch.setattr(cache_worker.settings, "CACHE_INVALIDATION_MODE", "async")
    fake_redis.down = True
    with pytest.raises(cache_worker.CacheUnavailable):
        cache_worker.make_handler(cache)(event_body("INVENTORY_MOVEMENT_RECORDED", {"product_id": 9}), None, "")
