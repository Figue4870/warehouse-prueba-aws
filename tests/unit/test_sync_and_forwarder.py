"""Fase 7 (lado bodega): endpoints de sincronización, heartbeat y forwarder."""

from types import SimpleNamespace

import pytest

from platform_common.events import parse_event

from app.config import settings
from app.workers.central_forwarder import CentralForwarder, CentralUnavailable
from app.workers.relay import OutboxRelay


class RecordingPublisher:
    def __init__(self, fail=False):
        self.published, self.fail = [], fail

    def publish(self, **kwargs):
        if self.fail:
            raise ConnectionError("central caída")
        self.published.append(kwargs)


def publish_all(session_factory):
    OutboxRelay(session_factory, RecordingPublisher()).run_once()


def test_sync_events_devuelve_lo_posterior_a_una_secuencia(client, create_product, move, session_factory):
    product = create_product(sku="SYN-1")
    move(product["id"], "RECEIPT", 5)
    move(product["id"], "SHIPMENT", 2)
    publish_all(session_factory)
    data = client.get("/sync/events", params={"after_sequence": 1}).json()
    assert data["last_sequence"] == 3 and data["oldest_available_sequence"] == 1
    assert [e["sequence"] for e in data["events"]] == [2, 3]
    assert data["events"][1]["payload"]["quantity_after"] == 3


def test_snapshot_trae_productos_saldos_y_versiones(client, create_product, move, session_factory):
    product = create_product(sku="SYN-2", retail_price="10")
    move(product["id"], "RECEIPT", 7)
    publish_all(session_factory)
    snap = client.get("/sync/snapshot").json()
    [item] = snap["products"]
    assert item["sku"] == "SYN-2" and item["quantity"] == 7 and item["inventory_version"] == 1
    assert snap["last_sequence"] == 2 and snap["warehouse_id"] == "WH-TEST"


def test_inventario_por_sku(client, create_product, move):
    product = create_product(sku="SYN-3")
    move(product["id"], "RECEIPT", 4)
    data = client.get("/inventory/by-sku/SYN-3").json()
    assert data["quantity"] == 4 and data["product_id"] == product["id"]
    assert client.get("/inventory/by-sku/NO-EXISTE").status_code == 404


def test_heartbeat_reporta_la_ultima_secuencia_publicada(create_product, session_factory, monkeypatch):
    monkeypatch.setattr(settings, "ADVERTISED_API_URL", "http://10.0.0.9:8001")
    create_product()
    publisher = RecordingPublisher()
    relay = OutboxRelay(session_factory, publisher)
    relay.run_once()
    relay.send_heartbeat()
    beat = publisher.published[-1]
    event = parse_event(beat["body"])
    assert event.event_type == "WAREHOUSE_HEARTBEAT" and beat["routing_key"] == "warehouse.heartbeat"
    assert event.payload["last_sequence"] == 1 and event.payload["api_url"] == "http://10.0.0.9:8001"
    assert beat["expiration_ms"] == 60_000 and beat["persistent"] is False  # un heartbeat viejo no sirve


class FakeChannel:
    def __init__(self):
        self.acks = []

    def basic_ack(self, delivery_tag):
        self.acks.append(delivery_tag)


def message(routing_key="inventory.movement.recorded"):
    props = SimpleNamespace(headers={"sequence": 5}, message_id="evt-1", correlation_id="COR-1", type="X", expiration=None, delivery_mode=2)
    return SimpleNamespace(delivery_tag=9, routing_key=routing_key), props


def test_forwarder_reenvia_y_solo_despues_hace_ack():
    central = RecordingPublisher()
    channel = FakeChannel()
    method, props = message()
    CentralForwarder("amqp://local", central, "WH-001")._on_message(channel, method, props, b"{}")
    [sent] = central.published
    assert sent["routing_key"] == "WH-001.inventory.movement.recorded" and sent["message_id"] == "evt-1"
    assert sent["headers"]["warehouse_id"] == "WH-001"
    assert channel.acks == [9]


def test_si_la_central_esta_caida_no_hay_ack_y_el_mensaje_se_queda():
    channel = FakeChannel()
    method, props = message()
    with pytest.raises(CentralUnavailable):
        CentralForwarder("amqp://local", RecordingPublisher(fail=True), "WH-001")._on_message(channel, method, props, b"{}")
    assert channel.acks == []  # sin ack: RabbitMQ lo conserva en la cola local
