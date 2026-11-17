import logging
from dataclasses import dataclass, field
from typing import List, Tuple

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.cache import InventoryCache, MISS, get_inventory_cache
from app.config import settings
from app.events import types as events
from app.events.outbox import OutboxWriter
from app.events.payloads import alert_payload, movement_payload
from app.modules.alerts.alerts_service import DISCREPANCY, OUT_OF_STOCK, AlertService
from app.modules.inventory.inventory_repository import InventoryRepository
from app.modules.inventory.inventory_rules import calculate_new_balance
from app.modules.inventory.models import InventoryEntity, InventoryMovementEntity
from app.modules.products.models import ProductEntity
from app.modules.inventory.schemas import (
    InventoryResponse,
    MovementCreate,
    MovementResponse,
    MovementResult,
    MovementType,
)
from app.modules.shared.errors import BusinessRuleError

logger = logging.getLogger("warehouse.inventory")

# Fase 6: solo la Saga (command-worker) puede registrar estos tipos
TRANSFER_TYPES = {MovementType.TRANSFER_IN, MovementType.TRANSFER_OUT}


@dataclass
class AppliedMovement:
    movement: InventoryMovementEntity
    product: ProductEntity
    inventory: InventoryEntity
    alerts_generated: List[str] = field(default_factory=list)


class InventoryService:
    def __init__(self, db: Session, cache: InventoryCache | None = None):
        self.db = db
        self.repo = InventoryRepository(db)
        self.alerts = AlertService(db)
        self.outbox = OutboxWriter(db)
        self.cache = cache if cache is not None else get_inventory_cache()

    # ------------------------------------------------------------------
    # Métodos para otros servicios del monolito (ProductService)
    # ------------------------------------------------------------------
    def initialize_stock(self, product_id: int) -> None:
        """Crea el saldo en 0. No hace commit: participa en la transacción de quien llama."""
        self.repo.initialize_inventory(product_id)

    def lock_inventory(self, product_id: int) -> InventoryEntity:
        """Bloquea la fila de inventario (FOR UPDATE). No hace commit."""
        row = self.repo.get_by_product_for_update(product_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inventario no encontrado")
        return row[0]

    def invalidate_cache(self, product_id: int) -> None:
        # Estrategia A (síncrona). Con CACHE_INVALIDATION_MODE=async la hace solo el cache-worker.
        if settings.CACHE_INVALIDATION_MODE in ("sync", "both"):
            self.cache.invalidate(product_id)

    # ------------------------------------------------------------------
    # GET /inventory/{product_id}  ->  CACHE-ASIDE (Fase 2)
    # ------------------------------------------------------------------
    def get_stock(self, product_id: int) -> Tuple[dict, str]:
        """Devuelve (inventario, estado_cache) donde estado_cache es HIT, MISS o BYPASS."""
        # 1) Primero preguntar a Redis
        lookup = self.cache.get(product_id)
        if lookup.value is not None:
            return lookup.value, lookup.status  # HIT: PostgreSQL ni se entera

        # 2) MISS (o Redis apagado/caído): ir a la fuente de verdad
        inventory = self.repo.get_by_product(product_id)
        if not inventory:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inventario no encontrado")
        data = InventoryResponse.model_validate(inventory).model_dump(mode="json")

        # 3) Guardar en Redis con TTL para las próximas lecturas
        if lookup.status == MISS:
            self.cache.set(product_id, data)
        return data, lookup.status

    def get_stock_by_sku(self, sku: str) -> dict:
        """Fase 7: la central consulta una bodega por SKU (el id local cambia entre bodegas)."""
        row = self.repo.get_by_sku(sku)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"El SKU {sku} no existe en {settings.WAREHOUSE_ID}")
        inventory, product = row
        return {
            "warehouse_id": settings.WAREHOUSE_ID,
            "sku": product.sku,
            "product_id": product.id,
            "is_active": product.is_active,
            "quantity": inventory.quantity,
            "reserved_quantity": inventory.reserved_quantity,
            "version": inventory.version,
            "updated_at": inventory.updated_at.isoformat() if inventory.updated_at else None,
        }

    # ------------------------------------------------------------------
    # El corazón de un movimiento, SIN commit (lo usan la API y la Saga)
    # ------------------------------------------------------------------
    def apply_movement(
        self,
        product_id: int,
        m_type: MovementType,
        quantity: int,
        lot: str | None = None,
        reference: str | None = None,
        from_reservation: bool = False,
    ) -> AppliedMovement:
        """
        Bloquea el saldo, aplica la regla, guarda saldo + historial + alertas +
        eventos del outbox. NO hace commit: participa en la transacción de quien
        llama (la API en process_movement, o el command-worker de la Saga, que
        en la MISMA transacción guarda también el resultado del comando).
        Lanza BusinessRuleError si el movimiento rompe una regla.
        """
        m_type = MovementType(m_type)
        alerts_generated: List[str] = []

        # 1. Leer el saldo actual bloqueando la fila
        row = self.repo.get_by_product_for_update(product_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No existe inventario para el producto {product_id} en la bodega {settings.WAREHOUSE_ID}",
            )
        inventory, product = row
        if not product.is_active:
            raise BusinessRuleError("El producto está dado de baja; no admite movimientos")

        before_qty = inventory.quantity
        before_reserved = inventory.reserved_quantity

        # 2. Aplicar la regla de negocio (lanza BusinessRuleError si quedaría negativo)
        new_qty, new_reserved = calculate_new_balance(
            m_type, quantity, before_qty, before_reserved, from_reservation=from_reservation
        )

        # 3. Guardar nuevo saldo + historial
        self.repo.update_stock(inventory, new_qty, new_reserved)
        movement = self.repo.register_movement(
            product_id=product_id,
            m_type=m_type.value,
            qty=quantity,
            quantity_after=new_qty,
            reserved_after=new_reserved,
            lot=lot,
            ref=reference,
        )

        # 4. Evento del movimiento para RabbitMQ (Fase 3) — misma transacción
        self.outbox.add(events.INVENTORY_MOVEMENT_RECORDED, "inventory", product.sku,
                        movement_payload(movement, product, inventory))

        # 5. Logs automáticos que pide el PDF (misma transacción) + su evento
        if before_qty > 0 and new_qty == 0:
            alert = self.alerts.register(
                product_id,
                OUT_OF_STOCK,
                f"Producto {product_id} agotado en {settings.WAREHOUSE_ID} tras {m_type.value} "
                f"de {quantity} (reservado: {new_reserved})",
                movement.id,
            )
            self.outbox.add(events.STOCK_DEPLETED, "alert", alert.id, alert_payload(alert, product))
            alerts_generated.append(OUT_OF_STOCK)
        if m_type == MovementType.ADJUSTMENT:
            alert = self.alerts.register(
                product_id,
                DISCREPANCY,
                f"Desfase de inventario en producto {product_id}: el sistema tenía {before_qty}, "
                f"se ajustó {quantity:+d} (ahora {new_qty}). "
                f"Motivo: {reference or 'sin referencia'}",
                movement.id,
            )
            self.outbox.add(events.STOCK_DISCREPANCY, "alert", alert.id, alert_payload(alert, product))
            alerts_generated.append(DISCREPANCY)

        return AppliedMovement(movement, product, inventory, alerts_generated)

    # ------------------------------------------------------------------
    # POST /inventory/movements  ->  TRANSACCIÓN ATÓMICA (Fase 1)
    # ------------------------------------------------------------------
    def process_movement(self, movement_in: MovementCreate) -> MovementResult:
        """
        Todo lo que pasa aquí ocurre en UNA transacción:
            BEGIN
              SELECT ... FOR UPDATE      (bloquear el saldo del producto)
              UPDATE inventory           (nuevo saldo)
              INSERT inventory_movements (historial)
              INSERT inventory_alerts    (solo si se agotó o fue un ajuste)
              INSERT outbox_events       (Fase 3: los eventos, en la MISMA transacción)
            COMMIT                       (o ROLLBACK si algo falla)
        Y DESPUÉS del COMMIT: borrar la clave de Redis (invalidación).
        """
        product_id = movement_in.product_id
        m_type = MovementType(movement_in.type)
        if m_type in TRANSFER_TYPES:
            # Fase 6: una transferencia "a mano" (salida aquí sin la entrada allá) descuadraría
            # el inventario de la empresa. Solo la Saga de la central registra estos movimientos.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Las transferencias entre bodegas se piden en la central: POST /transfers (Fase 6, Saga). "
                       "La Saga registra TRANSFER_OUT y TRANSFER_IN en las dos bodegas.",
            )
        try:
            applied = self.apply_movement(product_id, m_type, movement_in.quantity, movement_in.lot, movement_in.reference)
            movement, alerts_generated = applied.movement, applied.alerts_generated
            new_qty, new_reserved = movement.quantity_after, movement.reserved_after

            # 6. Confirmar TODO junto
            self.db.commit()

        except BusinessRuleError as error:
            self.db.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=error.message)
        except HTTPException:
            self.db.rollback()
            raise
        except IntegrityError:
            # Ej.: el CHECK (quantity >= 0) de PostgreSQL rechazó el cambio
            self.db.rollback()
            logger.exception("La base de datos rechazó el movimiento", extra={"event": "movement_integrity_error"})
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="La base de datos rechazó la operación")
        except OperationalError:
            # PostgreSQL caído: main.py lo convierte en HTTP 503
            self.db.rollback()
            raise
        except Exception:
            self.db.rollback()
            logger.exception("Error inesperado procesando movimiento", extra={"event": "movement_error"})
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error interno procesando el movimiento")

        # 7. Invalidación síncrona del caché (Estrategia A) — SIEMPRE después del COMMIT
        self.invalidate_cache(product_id)

        self.db.refresh(movement)
        context = {
            "product_id": product_id,
            "movement_id": movement.id,
            "type": m_type.value,
            "quantity": movement_in.quantity,
            "quantity_after": new_qty,
            "reserved_after": new_reserved,
        }
        logger.info("Movimiento registrado", extra={"event": "inventory_movement_registered", "context": context})
        if OUT_OF_STOCK in alerts_generated:
            logger.warning("Producto agotado", extra={"event": "out_of_stock", "context": context})
        if DISCREPANCY in alerts_generated:
            logger.warning("Desfase de inventario registrado", extra={"event": "inventory_discrepancy", "context": context})

        result = MovementResponse.model_validate(movement).model_dump()
        return MovementResult(**result, alerts_generated=alerts_generated)

    # ------------------------------------------------------------------
    def get_movements(self, product_id: int, limit: int, offset: int):
        if self.repo.get_by_product(product_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inventario no encontrado")
        return self.repo.get_movements_by_product(product_id, limit, offset)
