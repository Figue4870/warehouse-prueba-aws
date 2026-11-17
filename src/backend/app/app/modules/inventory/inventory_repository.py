from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from app.config import settings
from app.modules.inventory.models import InventoryEntity, InventoryMovementEntity
from app.modules.products.models import ProductEntity


class InventoryRepository:
    """Acceso a las tablas inventory e inventory_movements. No hace commit."""

    def __init__(self, db: Session):
        self.db = db

    def initialize_inventory(self, product_id: int) -> InventoryEntity:
        inventory = InventoryEntity(
            product_id=product_id,
            warehouse_id=settings.WAREHOUSE_ID,
            quantity=0,
            reserved_quantity=0,
        )
        self.db.add(inventory)
        return inventory

    def get_by_product(self, product_id: int) -> Optional[InventoryEntity]:
        """Lectura normal (sin bloqueo). La usa GET /inventory/{id}."""
        return (
            self.db.query(InventoryEntity)
            .filter(
                InventoryEntity.product_id == product_id,
                InventoryEntity.warehouse_id == settings.WAREHOUSE_ID,
            )
            .first()
        )

    def get_by_sku(self, sku: str) -> Optional[Tuple[InventoryEntity, ProductEntity]]:
        row = (
            self.db.query(InventoryEntity, ProductEntity)
            .join(ProductEntity, ProductEntity.id == InventoryEntity.product_id)
            .filter(ProductEntity.sku == sku, InventoryEntity.warehouse_id == settings.WAREHOUSE_ID)
            .first()
        )
        return (row[0], row[1]) if row else None

    def get_by_product_for_update(self, product_id: int) -> Optional[Tuple[InventoryEntity, ProductEntity]]:
        """
        Lectura CON BLOQUEO DE FILA (SELECT ... FOR UPDATE).

        Si dos salidas llegan al mismo tiempo para el mismo producto, la segunda
        espera a que la primera haga COMMIT o ROLLBACK antes de leer el saldo.
        Así nunca se venden las mismas unidades dos veces.

        Devuelve (inventario, producto). Se bloquea también la fila del
        producto para que nadie lo dé de baja mientras se mueve su stock.
        """
        row = (
            self.db.query(InventoryEntity, ProductEntity)
            .join(ProductEntity, ProductEntity.id == InventoryEntity.product_id)
            .filter(
                InventoryEntity.product_id == product_id,
                InventoryEntity.warehouse_id == settings.WAREHOUSE_ID,
            )
            .with_for_update()
            .first()
        )
        if row is None:
            return None
        return row[0], row[1]

    def update_stock(self, inventory: InventoryEntity, quantity: int, reserved_quantity: int) -> None:
        inventory.quantity = quantity
        inventory.reserved_quantity = reserved_quantity
        inventory.version = (inventory.version or 0) + 1  # Fase 3: versión para sincronizar
        self.db.add(inventory)

    def register_movement(
        self,
        product_id: int,
        m_type: str,
        qty: int,
        quantity_after: int,
        reserved_after: int,
        lot: Optional[str],
        ref: Optional[str],
    ) -> InventoryMovementEntity:
        movement = InventoryMovementEntity(
            product_id=product_id,
            warehouse_id=settings.WAREHOUSE_ID,
            type=m_type,
            quantity=qty,
            quantity_after=quantity_after,
            reserved_after=reserved_after,
            lot=lot,
            reference=ref,
        )
        self.db.add(movement)
        self.db.flush()  # para conocer movement.id (lo usan las alertas)
        return movement

    def get_movements_by_product(self, product_id: int, limit: int, offset: int) -> List[InventoryMovementEntity]:
        return (
            self.db.query(InventoryMovementEntity)
            .filter(
                InventoryMovementEntity.product_id == product_id,
                InventoryMovementEntity.warehouse_id == settings.WAREHOUSE_ID,
            )
            .order_by(InventoryMovementEntity.created_at.desc(), InventoryMovementEntity.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
