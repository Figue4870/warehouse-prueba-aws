"""
FASE 6 — SAGA ORQUESTADA de transferencias entre bodegas.

No existe una transacción SQL entre la base de WH-001 y la de WH-002. La Saga
divide la transferencia en PASOS LOCALES (cada uno es una transacción en UNA
bodega) y, si algo falla después de un paso, ejecuta su COMPENSACIÓN.

Camino feliz (30 unidades de WH-001 a WH-002):

    REQUESTED ──(RESERVE enviado)──> RESERVING_SOURCE ──(origen reservó)──> SOURCE_RESERVED
        ──(RECEIVE enviado)──> IN_TRANSIT ──(destino recibió)──> DESTINATION_RECEIVED
        ──(COMMIT: el origen despacha lo reservado)──> COMPLETED

    WH-001: 100 disp. ─RESERVE─> 70 disp. + 30 reservadas ─COMMIT─> 70
    WH-002:  20 disp. ─────────────RECEIVE─> 50

Fallas y compensaciones:

    RESERVING_SOURCE  origen rechaza (sin stock...)          -> FAILED (no había nada que deshacer)
    RESERVING_SOURCE  origen no responde a tiempo            -> COMPENSATING (RELEASE al origen)
    IN_TRANSIT        destino rechaza                        -> COMPENSATING (RELEASE al origen)
    IN_TRANSIT        destino no responde a tiempo           -> COMPENSATING (CANCEL_RECEIVE al destino)
                         destino: "no recibí nada"          ->   RELEASE al origen
                         destino: "ya lo había recibido"    ->   DESTINATION_RECEIVED (se sigue)
    COMPENSATING      origen liberó la reserva               -> RESERVATION_RELEASED

Reglas para no perder ni duplicar unidades:
  * Cada comando se guarda en un OUTBOX de la central (transfer_commands) en la
    misma transacción que el cambio de estado; un relay lo publica.
  * Las bodegas responden por su outbox (no se pierden respuestas).
  * Nunca se compensa "a ciegas": si el destino no contestó, primero se le
    pide CANCEL_RECEIVE y se espera su respuesta (si estaba caído, la espera
    dura hasta que vuelva). Así nunca quedan unidades en las dos bodegas.
  * Las unidades quedan RESERVADAS en el origen hasta el final: no se pueden
    vender dos veces y la compensación es solo liberarlas.
  * Respuestas repetidas o que llegan tarde se ignoran (transfer_history.event_id
    es único y cada respuesta solo vale en ciertos estados).
"""

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from platform_common import commands as c
from platform_common.correlation import get_correlation_id
from platform_common.events import EventEnvelope
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from central.config import settings
from central.models import (
    CatalogProductEntity,
    TransferCommandEntity,
    TransferEntity,
    TransferHistoryEntity,
    WarehouseEntity,
)
from central.projection import as_aware

logger = logging.getLogger("central.saga")

# ---------------------------------------------------------------- estados
REQUESTED = "REQUESTED"
RESERVING_SOURCE = "RESERVING_SOURCE"
SOURCE_RESERVED = "SOURCE_RESERVED"
IN_TRANSIT = "IN_TRANSIT"
DESTINATION_RECEIVED = "DESTINATION_RECEIVED"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
COMPENSATING = "COMPENSATING"
RESERVATION_RELEASED = "RESERVATION_RELEASED"

STATES = (REQUESTED, RESERVING_SOURCE, SOURCE_RESERVED, IN_TRANSIT, DESTINATION_RECEIVED, COMPLETED,
          FAILED, COMPENSATING, RESERVATION_RELEASED)
TERMINAL = {COMPLETED, FAILED, RESERVATION_RELEASED}

NOTES = {
    REQUESTED: "Aceptada. El comando RESERVE espera en la central para salir hacia el origen",
    RESERVING_SOURCE: "Se pidió al origen apartar las unidades",
    SOURCE_RESERVED: "El origen apartó las unidades (no se pueden vender)",
    IN_TRANSIT: "Unidades reservadas en el origen; se pidió al destino recibirlas",
    DESTINATION_RECEIVED: "El destino ya sumó las unidades; se pidió al origen despachar lo reservado",
    COMPLETED: "Transferencia terminada: salió del origen y entró al destino",
    FAILED: "No se hizo nada: no había nada que compensar",
    COMPENSATING: "Deshaciendo lo hecho (compensación)",
    RESERVATION_RELEASED: "Compensada: el origen liberó la reserva; el inventario quedó como al principio",
}

APPLIED, IGNORED, DUPLICATE, UNKNOWN = "applied", "ignored", "duplicate", "unknown"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class SagaError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class TransferRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    sku: str = Field(..., min_length=1, max_length=64)
    source_warehouse_id: str = Field(..., min_length=1, max_length=20)
    destination_warehouse_id: str = Field(..., min_length=1, max_length=20)
    quantity: int = Field(..., gt=0)
    timeout_seconds: Optional[int] = Field(None, ge=5, le=3600, description="Plazo de cada paso (por defecto SAGA_STEP_TIMEOUT_SECONDS)")
    simulate_failure: Optional[Literal["SOURCE_REJECTS", "DESTINATION_REJECTS"]] = Field(
        None, description="Solo para los experimentos de la guía: hace que el origen o el destino rechacen su paso")

    @model_validator(mode="after")
    def different_warehouses(self):
        if self.source_warehouse_id == self.destination_warehouse_id:
            raise ValueError("El origen y el destino deben ser bodegas distintas")
        return self

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------- ayudas internas
def _history(db: Session, transfer: TransferEntity, from_state, to_state, detail=None, event: EventEnvelope | None = None):
    db.add(TransferHistoryEntity(
        transfer_id=transfer.transfer_id, from_state=from_state, to_state=to_state, detail=detail,
        event_id=event.event_id if event else None, event_type=event.event_type if event else None,
        warehouse_id=event.warehouse_id if event else None, created_at=now_utc(),
    ))


def _move(db: Session, transfer: TransferEntity, to_state: str, detail: str | None = None, event: EventEnvelope | None = None):
    from_state = transfer.state
    transfer.state = to_state
    transfer.note = detail or NOTES[to_state]
    transfer.updated_at = now_utc()
    if to_state in TERMINAL:
        transfer.finished_at = transfer.updated_at
        transfer.compensation_step = None
    _history(db, transfer, from_state, to_state, transfer.note, event)
    logger.info("Transferencia cambió de estado", extra={"event": "transfer_state_changed", "context": {
        "transfer_id": transfer.transfer_id, "from": from_state, "to": to_state, "detail": transfer.note}})


def _issue(db: Session, transfer: TransferEntity, command_type: str, warehouse_id: str,
           deadline: datetime | None = None, **extra) -> TransferCommandEntity:
    """Guarda el comando en el outbox de la central (lo publica el relay de comandos)."""
    command = TransferCommandEntity(
        command_id=str(uuid.uuid4()), transfer_id=transfer.transfer_id, warehouse_id=warehouse_id,
        command_type=command_type, deadline=deadline, correlation_id=transfer.correlation_id, created_at=now_utc(),
        payload={"sku": transfer.sku, "quantity": transfer.quantity,
                 "source_warehouse_id": transfer.source_warehouse_id,
                 "destination_warehouse_id": transfer.destination_warehouse_id, **extra},
    )
    db.add(command)
    return command


def _catalog_info(db: Session, sku: str) -> dict:
    """Datos del producto para que el destino lo cree si no lo tiene."""
    product = db.get(CatalogProductEntity, sku)
    if product is None:
        return {}
    money = lambda v: str(v) if v is not None else None  # noqa: E731
    return {"name": product.name, "description": product.description, "category": product.category, "unit": product.unit,
            "supplier": product.supplier, "supplier_cost": money(product.supplier_cost),
            "retail_price": money(product.retail_price), "wholesale_price": money(product.wholesale_price)}


def _start_release(db: Session, transfer: TransferEntity, detail: str, event: EventEnvelope | None = None):
    """Compensación del origen: liberar la reserva."""
    if transfer.state != COMPENSATING:
        _move(db, transfer, COMPENSATING, detail, event)
    else:
        transfer.note = detail
        _history(db, transfer, COMPENSATING, COMPENSATING, detail, event)
    transfer.compensation_step = c.RELEASE
    _issue(db, transfer, c.RELEASE, transfer.source_warehouse_id)


def _send_receive(db: Session, transfer: TransferEntity):
    transfer.receive_deadline = now_utc() + timedelta(seconds=transfer.timeout_seconds)
    extra = {"product": _catalog_info(db, transfer.sku)}
    if transfer.simulate_failure == c.SIMULATE_DESTINATION_REJECTS:
        extra["simulate_failure"] = c.SIMULATE_DESTINATION_REJECTS
    _issue(db, transfer, c.RECEIVE, transfer.destination_warehouse_id, deadline=transfer.receive_deadline, **extra)
    _move(db, transfer, IN_TRANSIT)


def _send_commit(db: Session, transfer: TransferEntity, event: EventEnvelope, detail: str | None = None):
    transfer.compensation_step = None
    _move(db, transfer, DESTINATION_RECEIVED, detail, event)
    _issue(db, transfer, c.COMMIT, transfer.source_warehouse_id)


# ---------------------------------------------------------------- 1) crear (POST /transfers)
def create_transfer(db: Session, request: TransferRequest, idempotency_key: str | None = None) -> tuple[TransferEntity, bool]:
    """Devuelve (transferencia, creada). Con la misma Idempotency-Key devuelve la que ya existía."""
    fingerprint = request.fingerprint()
    if idempotency_key:
        existing = db.query(TransferEntity).filter(TransferEntity.idempotency_key == idempotency_key).first()
        if existing is not None:
            if existing.request_hash != fingerprint:
                raise SagaError(409, "Esa Idempotency-Key ya se usó con OTROS datos")
            return existing, False

    for warehouse_id in (request.source_warehouse_id, request.destination_warehouse_id):
        if db.get(WarehouseEntity, warehouse_id) is None:
            raise SagaError(404, f"La central no conoce la bodega {warehouse_id} (¿está encendida y conectada?)")
    if request.simulate_failure and not settings.SAGA_ALLOW_SIMULATION:
        raise SagaError(422, "Las fallas simuladas están desactivadas (SAGA_ALLOW_SIMULATION=false)")

    now = now_utc()
    timeout = request.timeout_seconds or settings.SAGA_STEP_TIMEOUT_SECONDS
    transfer = TransferEntity(
        idempotency_key=idempotency_key, request_hash=fingerprint, sku=request.sku,
        source_warehouse_id=request.source_warehouse_id, destination_warehouse_id=request.destination_warehouse_id,
        quantity=request.quantity, state=REQUESTED, note=NOTES[REQUESTED], timeout_seconds=timeout,
        reserve_deadline=now + timedelta(seconds=timeout), simulate_failure=request.simulate_failure,
        correlation_id=get_correlation_id(), created_at=now, updated_at=now,
    )
    try:
        db.add(transfer)
        db.flush()
        transfer.transfer_id = f"TR-{transfer.id:06d}"
        _history(db, transfer, None, REQUESTED, NOTES[REQUESTED])
        extra = {"simulate_failure": c.SIMULATE_SOURCE_REJECTS} if request.simulate_failure == c.SIMULATE_SOURCE_REJECTS else {}
        _issue(db, transfer, c.RESERVE, transfer.source_warehouse_id, deadline=transfer.reserve_deadline, **extra)
        db.commit()
    except IntegrityError:
        # Dos POST con la misma Idempotency-Key al mismo tiempo: la UNIQUE dejó pasar uno
        db.rollback()
        existing = db.query(TransferEntity).filter(TransferEntity.idempotency_key == idempotency_key).first()
        if existing is None:
            raise
        return existing, False
    db.refresh(transfer)
    logger.info("Transferencia solicitada", extra={"event": "transfer_requested", "context": {
        "transfer_id": transfer.transfer_id, "sku": transfer.sku, "quantity": transfer.quantity,
        "source": transfer.source_warehouse_id, "destination": transfer.destination_warehouse_id}})
    return transfer, True


# ---------------------------------------------------------------- 2) el relay publicó un comando
def mark_published(db: Session, command: TransferCommandEntity) -> None:
    command.published_at = now_utc()
    if command.command_type == c.RESERVE:
        # FOR UPDATE: si justo llega la respuesta del origen, no pisar su estado con uno más viejo
        transfer = (db.query(TransferEntity).filter(TransferEntity.transfer_id == command.transfer_id)
                    .with_for_update().first())
        if transfer is not None and transfer.state == REQUESTED:
            _move(db, transfer, RESERVING_SOURCE)


# ---------------------------------------------------------------- 3) llegó una respuesta de una bodega
SOURCE_REPLIES = {c.TRANSFER_SOURCE_RESERVED, c.TRANSFER_SOURCE_REJECTED, c.TRANSFER_SOURCE_COMMITTED,
                  c.TRANSFER_SOURCE_COMMIT_FAILED, c.TRANSFER_SOURCE_RELEASED}


def handle_reply(db: Session, event: EventEnvelope) -> str:
    """Avanza la máquina de estados. NO hace commit (lo hace quien llama)."""
    if event.event_type not in c.REPLY_TYPES:
        return IGNORED
    transfer_id = event.payload.get("transfer_id")
    transfer = (db.query(TransferEntity).filter(TransferEntity.transfer_id == transfer_id)
                .with_for_update().first())  # una respuesta a la vez por transferencia
    if transfer is None:
        logger.warning("Respuesta de una transferencia desconocida", extra={"event": "transfer_unknown_reply",
                       "context": {"transfer_id": transfer_id, "event_type": event.event_type}})
        return UNKNOWN
    if db.query(TransferHistoryEntity).filter(TransferHistoryEntity.event_id == event.event_id).first() is not None:
        return DUPLICATE
    expected = transfer.source_warehouse_id if event.event_type in SOURCE_REPLIES else transfer.destination_warehouse_id
    if event.warehouse_id != expected:
        return _ignore(db, transfer, event, f"Respuesta de {event.warehouse_id}, se esperaba de {expected}")

    p, state, kind = event.payload, transfer.state, event.event_type
    reason = p.get("reason")
    awaiting_destination = state == COMPENSATING and transfer.compensation_step == c.CANCEL_RECEIVE
    awaiting_release = state == COMPENSATING and transfer.compensation_step == c.RELEASE

    if kind == c.TRANSFER_SOURCE_RESERVED and state in (REQUESTED, RESERVING_SOURCE):
        _move(db, transfer, SOURCE_RESERVED, event=event)
        _send_receive(db, transfer)
    elif kind == c.TRANSFER_SOURCE_REJECTED and state in (REQUESTED, RESERVING_SOURCE):
        transfer.failure_reason = reason
        _move(db, transfer, FAILED, f"El origen rechazó la reserva: {reason}", event)
    elif kind == c.TRANSFER_DESTINATION_RECEIVED and (state == IN_TRANSIT or awaiting_destination):
        _send_commit(db, transfer, event)
    elif kind == c.TRANSFER_DESTINATION_REJECTED and (state == IN_TRANSIT or awaiting_destination):
        transfer.failure_reason = reason
        _start_release(db, transfer, f"El destino rechazó: {reason}. Se libera la reserva del origen", event)
    elif kind == c.TRANSFER_DESTINATION_CANCELLED and awaiting_destination:
        if p.get("cancelled"):
            _start_release(db, transfer, "El destino confirmó que no recibió nada. Se libera la reserva del origen", event)
        else:
            _send_commit(db, transfer, event, "El destino ya había recibido las unidades: la transferencia sigue (COMMIT)")
    elif kind == c.TRANSFER_SOURCE_COMMITTED and state == DESTINATION_RECEIVED:
        _move(db, transfer, COMPLETED, event=event)
    elif kind == c.TRANSFER_SOURCE_COMMIT_FAILED and state == DESTINATION_RECEIVED:
        transfer.needs_review = True
        transfer.failure_reason = reason
        transfer.note = f"REQUIERE REVISIÓN: el destino recibió pero el origen no pudo despachar ({reason})"
        _history(db, transfer, state, state, transfer.note, event)
    elif kind == c.TRANSFER_SOURCE_RELEASED and awaiting_release:
        released = int(p.get("released_quantity") or 0)
        if p.get("outcome") == "REJECTED":
            transfer.needs_review = True
        final = RESERVATION_RELEASED if released > 0 else FAILED
        detail = (f"El origen liberó {released} unidades" if released > 0
                  else f"El origen no tenía nada reservado ({reason or 'nunca alcanzó a reservar'})")
        _move(db, transfer, final, detail, event)
    else:
        return _ignore(db, transfer, event, f"{kind} no cambia nada en el estado {state}")
    return APPLIED


def _ignore(db: Session, transfer: TransferEntity, event: EventEnvelope, detail: str) -> str:
    """Respuestas tardías o fuera de lugar: se registran (para el historial) pero no cambian el estado."""
    _history(db, transfer, transfer.state, transfer.state, f"Ignorada: {detail}", event)
    logger.info("Respuesta ignorada", extra={"event": "transfer_reply_ignored", "context": {
        "transfer_id": transfer.transfer_id, "event_type": event.event_type, "detail": detail}})
    return IGNORED


# ---------------------------------------------------------------- 4) vencimientos (timeouts)
def check_timeouts(db: Session, now: datetime | None = None) -> int:
    """Pasos que no respondieron a tiempo -> se empieza a compensar. Hace commit."""
    now = now or now_utc()
    grace = timedelta(seconds=settings.SAGA_GRACE_SECONDS)
    changed = 0
    query = db.query(TransferEntity).filter(TransferEntity.state.in_((REQUESTED, RESERVING_SOURCE, IN_TRANSIT)))
    if db.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    for transfer in query.all():
        if transfer.state in (REQUESTED, RESERVING_SOURCE) and now > as_aware(transfer.reserve_deadline) + grace:
            transfer.failure_reason = "El origen no respondió a tiempo"
            # RELEASE es seguro aunque el origen nunca haya reservado: deja una marca y un RESERVE atrasado se rechaza
            _start_release(db, transfer, f"El origen {transfer.source_warehouse_id} no respondió en {transfer.timeout_seconds} s: "
                                         "se le pide liberar lo que haya reservado")
            changed += 1
        elif transfer.state == IN_TRANSIT and transfer.receive_deadline and now > as_aware(transfer.receive_deadline) + grace:
            transfer.failure_reason = "El destino no respondió a tiempo"
            detail = (f"El destino {transfer.destination_warehouse_id} no respondió en {transfer.timeout_seconds} s: se le pide "
                      "CANCELAR la recepción y se espera su respuesta antes de liberar la reserva (nunca a ciegas)")
            _move(db, transfer, COMPENSATING, detail)
            transfer.compensation_step = c.CANCEL_RECEIVE
            _issue(db, transfer, c.CANCEL_RECEIVE, transfer.destination_warehouse_id)
            changed += 1
    db.commit()
    return changed


# ---------------------------------------------------------------- 5) para la API
def public_status(state: str) -> str:
    if state == COMPLETED:
        return "COMPLETED"
    if state in (FAILED, RESERVATION_RELEASED):
        return "FAILED"
    return "PROCESSING"


def transfer_summary(transfer: TransferEntity) -> dict:
    return {
        "transfer_id": transfer.transfer_id,
        "status": public_status(transfer.state),
        "state": transfer.state,
        "note": transfer.note,
        "sku": transfer.sku,
        "quantity": transfer.quantity,
        "source_warehouse_id": transfer.source_warehouse_id,
        "destination_warehouse_id": transfer.destination_warehouse_id,
        "failure_reason": transfer.failure_reason,
        "needs_review": transfer.needs_review,
        "compensation_step": transfer.compensation_step,
        "timeout_seconds": transfer.timeout_seconds,
        "simulate_failure": transfer.simulate_failure,
        "created_at": as_aware(transfer.created_at),
        "updated_at": as_aware(transfer.updated_at),
        "finished_at": as_aware(transfer.finished_at),
        "status_url": f"/transfers/{transfer.transfer_id}",
    }


def transfer_detail(db: Session, transfer: TransferEntity) -> dict:
    history = (db.query(TransferHistoryEntity).filter(TransferHistoryEntity.transfer_id == transfer.transfer_id)
               .order_by(TransferHistoryEntity.id).all())
    commands = (db.query(TransferCommandEntity).filter(TransferCommandEntity.transfer_id == transfer.transfer_id)
                .order_by(TransferCommandEntity.id).all())
    return {
        **transfer_summary(transfer),
        "history": [{"at": as_aware(h.created_at), "from": h.from_state, "to": h.to_state, "detail": h.detail,
                     "event_type": h.event_type, "warehouse_id": h.warehouse_id} for h in history],
        "commands": [{"command_type": cmd.command_type, "warehouse_id": cmd.warehouse_id, "created_at": as_aware(cmd.created_at),
                      "published_at": as_aware(cmd.published_at), "deadline": as_aware(cmd.deadline),
                      "attempts": cmd.attempts, "last_error": cmd.last_error} for cmd in commands],
    }
