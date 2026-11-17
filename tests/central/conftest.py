"""Pruebas de la plataforma CENTRAL (Fase 7): SQLite en memoria, sin Docker ni RabbitMQ."""

import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["AUTO_CREATE_TABLES"] = "false"

import itertools  # noqa: E402
import uuid  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from platform_common.events import EventEnvelope, build_event  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from central import models  # noqa: E402,F401
from central.api import app  # noqa: E402
from central.database import Base, get_db  # noqa: E402


@pytest.fixture
def central_sf():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    yield sessionmaker(autocommit=False, autoflush=False, bind=engine)
    engine.dispose()


@pytest.fixture
def central_client(central_sf):
    def _get_db():
        db = central_sf()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


class EventFactory:
    """Arma eventos como los que publica una bodega (con secuencia sin huecos)."""

    def __init__(self, warehouse_id="WH-001"):
        self.warehouse_id = warehouse_id
        self.seq = itertools.count(1)
        self.versions = {}

    def make(self, event_type, payload, sequence=None, occurred_at=None):
        return EventEnvelope.model_validate(build_event(
            event_type, f"warehouse-{self.warehouse_id}", {"warehouse_id": self.warehouse_id, **payload},
            warehouse_id=self.warehouse_id, sequence=sequence if sequence is not None else next(self.seq),
            event_id=str(uuid.uuid4()), occurred_at=(occurred_at or datetime.now(timezone.utc)).isoformat()))

    def product(self, sku="SKU-1", name="Casco", version=1, active=True, retail="100.00", event_type="PRODUCT_CREATED", **kw):
        return self.make(event_type, {"product_id": 1, "sku": sku, "name": name, "category": "EPP", "unit": "U",
                                      "retail_price": retail, "supplier_cost": "60.00", "is_active": active, "product_version": version}, **kw)

    def movement(self, sku="SKU-1", m_type="RECEIPT", quantity=10, after=10, reserved=0, version=None, **kw):
        version = version if version is not None else self.versions.get(sku, 0) + 1
        self.versions[sku] = max(self.versions.get(sku, 0), version)
        return self.make("INVENTORY_MOVEMENT_RECORDED", {
            "movement_id": version, "product_id": 1, "sku": sku, "product_name": "Casco", "category": "EPP", "unit": "U",
            "type": m_type, "quantity": quantity, "quantity_after": after, "reserved_after": reserved,
            "inventory_version": version, "retail_price": "100.00", "supplier_cost": "60.00",
            "created_at": datetime.now(timezone.utc).isoformat()}, **kw)

    def heartbeat(self, last_sequence, api_url="http://wh-001", outbox_pending=0):
        return self.make("WAREHOUSE_HEARTBEAT", {"last_sequence": last_sequence, "api_url": api_url, "outbox_pending": outbox_pending}, sequence=0)


@pytest.fixture
def wh1():
    return EventFactory("WH-001")


@pytest.fixture
def wh2():
    return EventFactory("WH-002")


@pytest.fixture
def apply(central_sf):
    """apply(evento1, evento2, ...) -> lista de resultados (applied / duplicate / heartbeat)."""
    from central.projection import apply_event

    def _apply(*events, source="stream"):
        results = []
        for event in events:
            with central_sf() as db:
                results.append(apply_event(db, event, source))
                db.commit()
        return results

    return _apply


@pytest.fixture
def get_row(central_sf):
    def _get(model, key):
        with central_sf() as db:
            row = db.get(model, key)
            if row is not None:
                db.expunge(row)
            return row

    return _get


def minutes_ago(minutes):
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


@pytest.fixture
def ago():
    return minutes_ago
