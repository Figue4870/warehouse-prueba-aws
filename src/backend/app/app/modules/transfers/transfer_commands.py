"""
Fase 6 — cómo una BODEGA ejecuta los comandos de la Saga de transferencias.

La central (orquestador) manda comandos; la bodega los ejecuta en UNA
transacción local que incluye:

    1. el movimiento de inventario (RESERVATION, TRANSFER_OUT, TRANSFER_IN o RELEASE)
    2. la fila en transfer_operations   (idempotencia: el mismo paso no se hace dos veces)
    3. la RESPUESTA para la central en el outbox (evento TRANSFER_*)

Si algo falla, no queda NADA (ni movimiento ni respuesta) y el comando se
reintenta. Si sale bien, la respuesta viaja por el mismo camino sin pérdida
de la Fase 7 (outbox -> relay -> forwarder -> central).

    Comando          Bodega    Qué hace                                       Responde
    RESERVE          origen    aparta las unidades (RESERVATION)              SOURCE_RESERVED / SOURCE_REJECTED
    RECEIVE          destino   suma las unidades (TRANSFER_IN)                DESTINATION_RECEIVED / DESTINATION_REJECTED
    COMMIT           origen    despacha lo reservado (TRANSFER_OUT)           SOURCE_COMMITTED / SOURCE_COMMIT_FAILED
    RELEASE          origen    COMPENSACIÓN: libera la reserva (RELEASE)      SOURCE_RELEASED
    CANCEL_RECEIVE   destino   COMPENSACIÓN: "si no la recibiste, ya no"     DESTINATION_CANCELLED
"""

import logging
from decimal import Decimal, InvalidOperation
from typing import Optional

from platform_common import commands as c
from platform_common.commands import CommandEnvelope
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.cache import InventoryCache
from app.config import settings
from app.events import types as events
from app.events.outbox import OutboxWriter
from app.events.payloads import product_payload
from app.modules.inventory.inventory_repository import InventoryRepository
from app.modules.inventory.inventory_service import InventoryService
from app.modules.inventory.schemas import MovementType
from app.modules.products.products_repository import ProductRepository
from app.modules.products.schemas import ProductCreate
from app.modules.shared.errors import BusinessRuleError
from app.modules.transfers.models import TransferOperationEntity

logger = logging.getLogger("warehouse.transfers")

DONE, REJECTED, NOOP, DUPLICATE = "DONE", "REJECTED", "NOOP", "DUPLICATE"


class TransferCommandService:
    def __init__(self, db: Session, cache: InventoryCache | None = None):
        self.db = db
        self.inventory = InventoryService(db, cache)
        self.inventory_repo = InventoryRepository(db)
        self.products = ProductRepository(db)
        self.outbox = OutboxWriter(db)
        self._touched_product: Optional[int] = None

    # ---------------------------------------------------------------- entrada
    def handle(self, command: CommandEnvelope) -> str:
        """Ejecuta el comando UNA sola vez. Devuelve DONE, REJECTED, NOOP o DUPLICATE."""
        if self._operation(command.transfer_id, command.command_type) is not None:
            logger.info("Comando repetido: ya se había hecho este paso", extra={"event": "transfer_command_duplicate",
                        "context": {"transfer_id": command.transfer_id, "step": command.command_type}})
            return DUPLICATE
        handler = {
            c.RESERVE: self._reserve,
            c.RECEIVE: self._receive,
            c.COMMIT: self._commit,
            c.RELEASE: self._release,
            c.CANCEL_RECEIVE: self._cancel_receive,
        }[command.command_type]
        try:
            outcome = handler(command)
            self.db.commit()
        except IntegrityError:
            # Dos copias del mismo comando a la vez: la UNIQUE (transfer_id, step) dejó pasar solo una
            self.db.rollback()
            if self._operation(command.transfer_id, command.command_type) is not None:
                return DUPLICATE
            raise
        except Exception:
            self.db.rollback()
            raise
        if self._touched_product:
            self.inventory.invalidate_cache(self._touched_product)
        logger.info("Paso de transferencia ejecutado", extra={"event": "transfer_command_done", "context": {
            "transfer_id": command.transfer_id, "step": command.command_type, "outcome": outcome}})
        return outcome

    # ---------------------------------------------------------------- ayudas
    def _operation(self, transfer_id: str, step: str) -> Optional[TransferOperationEntity]:
        return (self.db.query(TransferOperationEntity)
                .filter(TransferOperationEntity.transfer_id == transfer_id, TransferOperationEntity.step == step)
                .first())

    def _record(self, command: CommandEnvelope, outcome: str, reply_type: str, reason: str | None = None,
                product_id: int | None = None, quantity: int | None = None, movement=None, **extra) -> str:
        """Guarda el resultado del paso y la respuesta para la central (misma transacción)."""
        self.db.add(TransferOperationEntity(
            transfer_id=command.transfer_id, step=command.command_type, outcome=outcome,
            sku=command.payload.get("sku"), product_id=product_id, quantity=quantity, reason=reason,
            movement_id=movement.id if movement is not None else None, command_id=command.command_id,
        ))
        self.outbox.add(reply_type, "transfer", command.transfer_id, {
            "transfer_id": command.transfer_id,
            "command_id": command.command_id,
            "step": command.command_type,
            "outcome": outcome,
            "reason": reason,
            "sku": command.payload.get("sku"),
            "quantity": quantity if quantity is not None else command.payload.get("quantity"),
            "product_id": product_id,
            "movement_id": movement.id if movement is not None else None,
            **extra,
        })
        return outcome

    def _reject(self, command: CommandEnvelope, reply_type: str, reason: str, **extra) -> str:
        logger.warning("Paso de transferencia rechazado", extra={"event": "transfer_command_rejected", "context": {
            "transfer_id": command.transfer_id, "step": command.command_type, "reason": reason}})
        return self._record(command, REJECTED, reply_type, reason=reason, **extra)

    def _move(self, product_id: int, m_type: MovementType, quantity: int, transfer_id: str, from_reservation=False):
        applied = self.inventory.apply_movement(product_id, m_type, quantity, reference=transfer_id,
                                                from_reservation=from_reservation)
        self._touched_product = product_id
        return applied

    @staticmethod
    def _quantity(command: CommandEnvelope) -> int:
        quantity = int(command.payload.get("quantity") or 0)
        if quantity <= 0:
            raise BusinessRuleError("La cantidad debe ser mayor que 0")
        return quantity

    # ---------------------------------------------------------------- ORIGEN
    def _reserve(self, command: CommandEnvelope) -> str:
        reply = c.TRANSFER_SOURCE_REJECTED
        if self._operation(command.transfer_id, c.RELEASE) is not None:
            return self._reject(command, reply, "La transferencia ya fue cancelada (la compensación llegó antes)")
        if command.deadline_passed():
            return self._reject(command, reply, f"Comando vencido: debía ejecutarse antes de {command.deadline}")
        if command.payload.get("simulate_failure") == c.SIMULATE_SOURCE_REJECTS:
            return self._reject(command, reply, "Falla simulada en el origen (experimento)")
        row = self.inventory_repo.get_by_sku(command.payload.get("sku", ""))
        if row is None:
            return self._reject(command, reply, f"El SKU {command.payload.get('sku')} no existe en {settings.WAREHOUSE_ID}")
        _, product = row
        try:
            quantity = self._quantity(command)
            applied = self._move(product.id, MovementType.RESERVATION, quantity, command.transfer_id)
        except BusinessRuleError as error:
            return self._reject(command, reply, error.message, product_id=product.id)
        return self._record(command, DONE, c.TRANSFER_SOURCE_RESERVED, product_id=product.id, quantity=quantity,
                            movement=applied.movement, available_after=applied.inventory.quantity,
                            reserved_after=applied.inventory.reserved_quantity)

    def _commit(self, command: CommandEnvelope) -> str:
        reply = c.TRANSFER_SOURCE_COMMIT_FAILED
        reserve = self._operation(command.transfer_id, c.RESERVE)
        if reserve is None or reserve.outcome != DONE:
            return self._reject(command, reply, "No hay una reserva hecha para esta transferencia")
        if self._operation(command.transfer_id, c.RELEASE) is not None:
            return self._reject(command, reply, "La reserva de esta transferencia ya se liberó")
        try:
            applied = self._move(reserve.product_id, MovementType.TRANSFER_OUT, reserve.quantity, command.transfer_id,
                                 from_reservation=True)
        except BusinessRuleError as error:
            return self._reject(command, reply, error.message, product_id=reserve.product_id)
        return self._record(command, DONE, c.TRANSFER_SOURCE_COMMITTED, product_id=reserve.product_id,
                            quantity=reserve.quantity, movement=applied.movement,
                            available_after=applied.inventory.quantity, reserved_after=applied.inventory.reserved_quantity)

    def _release(self, command: CommandEnvelope) -> str:
        reply = c.TRANSFER_SOURCE_RELEASED
        commit = self._operation(command.transfer_id, c.COMMIT)
        if commit is not None and commit.outcome == DONE:
            return self._reject(command, reply, "Las unidades ya se despacharon: no hay reserva que liberar",
                                released_quantity=0)
        reserve = self._operation(command.transfer_id, c.RESERVE)
        if reserve is None or reserve.outcome != DONE:
            # Nunca se reservó (o se rechazó): no hay nada que liberar, pero queda la MARCA
            # para que un RESERVE atrasado de esta transferencia ya no se ejecute.
            return self._record(command, NOOP, reply, reason="No se había reservado nada", released_quantity=0)
        try:
            applied = self._move(reserve.product_id, MovementType.RELEASE, reserve.quantity, command.transfer_id)
        except BusinessRuleError as error:
            return self._reject(command, reply, error.message, product_id=reserve.product_id, released_quantity=0)
        return self._record(command, DONE, reply, product_id=reserve.product_id, quantity=reserve.quantity,
                            movement=applied.movement, released_quantity=reserve.quantity,
                            available_after=applied.inventory.quantity, reserved_after=applied.inventory.reserved_quantity)

    # ---------------------------------------------------------------- DESTINO
    def _receive(self, command: CommandEnvelope) -> str:
        reply = c.TRANSFER_DESTINATION_REJECTED
        if self._operation(command.transfer_id, c.CANCEL_RECEIVE) is not None:
            return self._reject(command, reply, "La transferencia ya fue cancelada (la compensación llegó antes)")
        if command.deadline_passed():
            return self._reject(command, reply, f"Comando vencido: debía ejecutarse antes de {command.deadline}")
        if command.payload.get("simulate_failure") == c.SIMULATE_DESTINATION_REJECTS:
            return self._reject(command, reply, "Falla simulada en el destino (experimento)")
        sku = command.payload.get("sku", "")
        product = self.products.get_by_sku(sku)
        if product is None:
            product = self._create_product_from_catalog(sku, command.payload.get("product") or {})
            if product is None:
                return self._reject(command, reply, f"El SKU {sku} no existe en {settings.WAREHOUSE_ID} y la central no envió sus datos")
        if not product.is_active:
            return self._reject(command, reply, f"El SKU {sku} está dado de baja en {settings.WAREHOUSE_ID}", product_id=product.id)
        try:
            quantity = self._quantity(command)
            applied = self._move(product.id, MovementType.TRANSFER_IN, quantity, command.transfer_id)
        except BusinessRuleError as error:
            return self._reject(command, reply, error.message, product_id=product.id)
        return self._record(command, DONE, c.TRANSFER_DESTINATION_RECEIVED, product_id=product.id, quantity=quantity,
                            movement=applied.movement, available_after=applied.inventory.quantity)

    def _cancel_receive(self, command: CommandEnvelope) -> str:
        reply = c.TRANSFER_DESTINATION_CANCELLED
        receive = self._operation(command.transfer_id, c.RECEIVE)
        if receive is not None and receive.outcome == DONE:
            # Ya se habían recibido: NO se deshace. La central seguirá con la transferencia (COMMIT en el origen).
            return self._record(command, NOOP, reply, reason="Las unidades ya se habían recibido", cancelled=False,
                                product_id=receive.product_id, quantity=receive.quantity)
        return self._record(command, DONE, reply, cancelled=True)

    def _create_product_from_catalog(self, sku: str, info: dict):
        """La bodega destino no tenía el producto: lo crea con los datos del catálogo central."""
        if not info.get("name"):
            return None

        def money(value):
            try:
                return Decimal(str(value)) if value not in (None, "") else None
            except InvalidOperation:
                return None

        product = self.products.create(ProductCreate(
            sku=sku, name=info["name"], unit=info.get("unit") or "UNIDAD", description=info.get("description"),
            category=info.get("category"), supplier=info.get("supplier"), supplier_cost=money(info.get("supplier_cost")),
            retail_price=money(info.get("retail_price")), wholesale_price=money(info.get("wholesale_price")),
        ))
        self.inventory.initialize_stock(product.id)
        self.db.flush()
        self.outbox.add(events.PRODUCT_CREATED, "product", product.sku, product_payload(product))
        logger.info("Producto creado por una transferencia", extra={"event": "product_created_by_transfer",
                    "context": {"sku": sku, "product_id": product.id}})
        return product


def operations_for(db: Session, transfer_id: str | None = None, limit: int = 100):
    query = db.query(TransferOperationEntity)
    if transfer_id:
        query = query.filter(TransferOperationEntity.transfer_id == transfer_id)
    return query.order_by(TransferOperationEntity.id.desc()).limit(limit).all()
