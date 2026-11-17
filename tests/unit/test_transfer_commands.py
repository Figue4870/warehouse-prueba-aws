"""Fase 6 (lado bodega): cómo el command-worker ejecuta cada paso de la Saga."""

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from platform_common import commands as c
from platform_common.commands import CommandEnvelope
from platform_common.messaging import PermanentError

from app.events.models import OutboxEventEntity
from app.modules.transfers.transfer_commands import DONE, DUPLICATE, NOOP, REJECTED, TransferCommandService
from app.workers.command_worker import make_handler


def command(command_type, transfer_id="TR-000001", deadline_in=60, **payload):
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=deadline_in)).isoformat() if deadline_in is not None else None
    return CommandEnvelope(command_id=str(uuid.uuid4()), command_type=command_type, transfer_id=transfer_id,
                           warehouse_id="WH-TEST", issued_at=datetime.now(timezone.utc).isoformat(), deadline=deadline,
                           payload=payload)


@pytest.fixture
def run(session_factory, cache):
    def _run(cmd):
        with session_factory() as db:
            return TransferCommandService(db, cache).handle(cmd)
    return _run


@pytest.fixture
def stock(client):
    def _stock(product_id):
        data = client.get(f"/inventory/{product_id}").json()
        return data["quantity"], data["reserved_quantity"]
    return _stock


@pytest.fixture
def replies(session_factory):
    def _replies():
        with session_factory() as db:
            return [(e.event_type, e.payload) for e in db.query(OutboxEventEntity).order_by(OutboxEventEntity.id)
                    if e.event_type.startswith("TRANSFER_")]
    return _replies


@pytest.fixture
def product(create_product, move):
    item = create_product(sku="P-001", name="Casco", retail_price="32000")
    move(item["id"], "RECEIPT", 100)
    return item


def test_camino_feliz_en_el_origen_reserva_y_despacha(run, product, stock, replies):
    assert run(command(c.RESERVE, sku="P-001", quantity=30)) == DONE
    assert stock(product["id"]) == (70, 30)          # apartadas: ya no se pueden vender
    assert run(command(c.COMMIT, deadline_in=None, sku="P-001")) == DONE
    assert stock(product["id"]) == (70, 0)           # salieron de la reserva, no del disponible
    assert [t for t, _ in replies()] == [c.TRANSFER_SOURCE_RESERVED, c.TRANSFER_SOURCE_COMMITTED]
    assert replies()[0][1]["reserved_after"] == 30


def test_comando_repetido_no_reserva_dos_veces(run, product, stock, replies):
    cmd = command(c.RESERVE, sku="P-001", quantity=30)
    assert run(cmd) == DONE
    assert run(cmd) == DUPLICATE
    # incluso con otro command_id: la llave es (transfer_id, paso)
    assert run(command(c.RESERVE, sku="P-001", quantity=30)) == DUPLICATE
    assert stock(product["id"]) == (70, 30)
    assert len(replies()) == 1


def test_sin_stock_se_rechaza_sin_tocar_nada(run, product, stock, replies):
    assert run(command(c.RESERVE, sku="P-001", quantity=500)) == REJECTED
    assert stock(product["id"]) == (100, 0)
    [(kind, payload)] = replies()
    assert kind == c.TRANSFER_SOURCE_REJECTED and "insuficiente" in payload["reason"]


def test_comando_vencido_se_rechaza(run, product, stock):
    assert run(command(c.RESERVE, deadline_in=-5, sku="P-001", quantity=10)) == REJECTED
    assert stock(product["id"]) == (100, 0)


def test_compensacion_libera_la_reserva(run, product, stock, replies):
    run(command(c.RESERVE, sku="P-001", quantity=30))
    assert run(command(c.RELEASE, deadline_in=None)) == DONE
    assert stock(product["id"]) == (100, 0)
    assert replies()[-1][1]["released_quantity"] == 30


def test_release_antes_que_reserve_deja_marca(run, product, stock, replies):
    # La compensación llegó primero (el RESERVE estaba atrasado): no hay nada que liberar...
    assert run(command(c.RELEASE, deadline_in=None)) == NOOP
    # ...y el RESERVE atrasado ya no se ejecuta: las unidades nunca quedan "atrapadas"
    assert run(command(c.RESERVE, sku="P-001", quantity=30)) == REJECTED
    assert stock(product["id"]) == (100, 0)
    assert "cancelada" in replies()[-1][1]["reason"]


def test_no_se_libera_lo_ya_despachado(run, product, stock):
    run(command(c.RESERVE, sku="P-001", quantity=30))
    run(command(c.COMMIT, deadline_in=None))
    assert run(command(c.RELEASE, deadline_in=None)) == REJECTED
    assert stock(product["id"]) == (70, 0)


def test_destino_recibe_y_crea_el_producto_si_no_existe(run, client, replies):
    info = {"name": "Guantes", "unit": "PAR", "category": "EPP", "retail_price": "15000.00"}
    assert run(command(c.RECEIVE, sku="G-777", quantity=12, product=info)) == DONE
    data = client.get("/inventory/by-sku/G-777").json()
    assert data["quantity"] == 12
    assert client.get(f"/products/{data['product_id']}").json()["retail_price"] == "15000.00"
    assert replies()[-1][0] == c.TRANSFER_DESTINATION_RECEIVED


def test_destino_sin_datos_del_producto_rechaza(run):
    assert run(command(c.RECEIVE, sku="NO-EXISTE", quantity=1)) == REJECTED


def test_cancelar_recepcion_antes_de_recibir(run, product, stock, replies):
    assert run(command(c.CANCEL_RECEIVE, deadline_in=None)) == DONE
    assert replies()[-1][1]["cancelled"] is True
    assert run(command(c.RECEIVE, sku="P-001", quantity=5)) == REJECTED   # llegó tarde: ya no
    assert stock(product["id"]) == (100, 0)


def test_cancelar_despues_de_recibir_no_deshace(run, product, stock, replies):
    run(command(c.RECEIVE, sku="P-001", quantity=5))
    assert run(command(c.CANCEL_RECEIVE, deadline_in=None)) == NOOP
    assert replies()[-1][1]["cancelled"] is False
    assert stock(product["id"]) == (105, 0)


def test_falla_simulada_en_destino(run, product, stock):
    assert run(command(c.RECEIVE, sku="P-001", quantity=5, simulate_failure=c.SIMULATE_DESTINATION_REJECTS)) == REJECTED
    assert stock(product["id"]) == (100, 0)


def test_la_api_no_acepta_transferencias_manuales(product, move):
    response = move(product["id"], "TRANSFER_OUT", 5)
    assert response.status_code == 409 and "POST /transfers" in response.json()["detail"]
    assert move(product["id"], "TRANSFER_IN", 5).status_code == 409


def test_operaciones_visibles_por_api(run, product, client):
    run(command(c.RESERVE, transfer_id="TR-000009", sku="P-001", quantity=3))
    [op] = client.get("/transfer-operations", params={"transfer_id": "TR-000009"}).json()
    assert op["step"] == "RESERVE" and op["outcome"] == "DONE" and op["quantity"] == 3


def test_worker_rechaza_comandos_de_otra_bodega_o_corruptos(session_factory, cache):
    handle = make_handler(session_factory, "WH-TEST", cache)
    other = command(c.RESERVE, sku="P-001", quantity=1).model_copy(update={"warehouse_id": "WH-999"})
    with pytest.raises(PermanentError):
        handle(other.model_dump_json().encode(), None, "WH-999")
    with pytest.raises(PermanentError):
        handle(b"esto no es json", None, "WH-TEST")
    with pytest.raises(PermanentError):
        handle(json.dumps({**command(c.RESERVE).model_dump(), "command_type": "BORRAR_TODO"}).encode(), None, "WH-TEST")
