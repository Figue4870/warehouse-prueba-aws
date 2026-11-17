"""
Configuración de las PRUEBAS UNITARIAS.

No necesitan Docker:
  * PostgreSQL se reemplaza por SQLite en memoria (se crea vacía en cada prueba).
  * Redis se reemplaza por FakeRedis (un diccionario de Python).
Así se prueban las reglas de negocio, la transacción y la lógica del caché
de forma rápida y repetible.
"""

import os

# Estas variables deben quedar listas ANTES de importar la aplicación.
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["AUTO_CREATE_TABLES"] = "false"
os.environ["WAREHOUSE_ID"] = "WH-TEST"
os.environ["CACHE_ENABLED"] = "true"

import pytest  # noqa: E402
import redis  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.cache import InventoryCache, get_inventory_cache  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from main import app  # noqa: E402


class FakeRedis:
    """Imitación mínima de Redis. Con `down = True` simula que Redis se cayó."""

    def __init__(self):
        self.data = {}
        self.ttls = {}
        self.down = False
        self.calls = 0

    def _check(self):
        self.calls += 1
        if self.down:
            raise redis.ConnectionError("Redis caído (simulado)")

    def get(self, key):
        self._check()
        return self.data.get(key)

    def set(self, key, value, ex=None):
        self._check()
        self.data[key] = value
        self.ttls[key] = ex

    def delete(self, *keys):
        self._check()
        removed = 0
        for key in keys:
            if key in self.data:
                removed += 1
                self.data.pop(key)
                self.ttls.pop(key, None)
        return removed

    def incr(self, key):
        self._check()
        self.data[key] = str(int(self.data.get(key, 0)) + 1)
        return int(self.data[key])

    def ping(self):
        self._check()
        return True

    def scan_iter(self, match=None, count=None):
        self._check()
        prefix = match.rstrip("*") if match else ""
        return [k for k in list(self.data) if k.startswith(prefix)]


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(eng, "connect")
    def _enable_foreign_keys(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture
def cache(fake_redis):
    return InventoryCache(fake_redis, warehouse_id="WH-TEST", ttl_seconds=30, enabled=True, retry_after_seconds=5)


@pytest.fixture
def client(session_factory, cache):
    def _get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_inventory_cache] = lambda: cache
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# ---------- ayudas para escribir pruebas cortas ----------
@pytest.fixture
def create_product(client):
    counter = {"n": 0}

    def _create(**overrides):
        counter["n"] += 1
        payload = {"sku": f"TEST-{counter['n']:03d}", "name": "Producto de prueba", "unit": "UNIDAD"}
        payload.update(overrides)
        response = client.post("/products", json=payload)
        assert response.status_code == 201, response.text
        return response.json()

    return _create


@pytest.fixture
def move(client):
    def _move(product_id, m_type, quantity, **extra):
        return client.post(
            "/inventory/movements",
            json={"product_id": product_id, "type": m_type, "quantity": quantity, **extra},
        )

    return _move
