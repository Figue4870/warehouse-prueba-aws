import logging

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.cache import InventoryCache
from app.events import types as events
from app.events.outbox import OutboxWriter
from app.events.payloads import product_payload
from app.modules.inventory.inventory_service import InventoryService
from app.modules.products.products_repository import ProductRepository
from app.modules.products.schemas import ProductCreate, ProductUpdate

logger = logging.getLogger("warehouse.products")


class ProductService:
    def __init__(self, db: Session, cache: InventoryCache | None = None):
        self.db = db
        self.product_repo = ProductRepository(db)
        # Dependencia hacia el SERVICIO del módulo vecino (no hacia su repositorio)
        self.inventory_service = InventoryService(db, cache)
        self.outbox = OutboxWriter(db)

    def create_product_with_inventory(self, product_in: ProductCreate):
        """Crea el producto y su saldo inicial (0) en la MISMA transacción."""
        if self.product_repo.get_by_sku(product_in.sku):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Ya existe un producto con SKU {product_in.sku}")
        try:
            product = self.product_repo.create(product_in)
            self.inventory_service.initialize_stock(product.id)
            # Fase 3: evento en la misma transacción (si el commit falla, el evento tampoco existe)
            self.outbox.add(events.PRODUCT_CREATED, "product", product.sku, product_payload(product))
            self.db.commit()
        except IntegrityError:
            # Dos requests crearon el mismo SKU a la vez: la restricción UNIQUE de la BD lo detiene
            self.db.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Ya existe un producto con SKU {product_in.sku}")
        except OperationalError:
            self.db.rollback()
            raise  # main.py responde 503
        except Exception:
            self.db.rollback()
            logger.exception("Error creando producto", extra={"event": "product_create_error"})
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error interno creando el producto")
        self.db.refresh(product)
        logger.info("Producto creado", extra={"event": "product_created", "context": {"product_id": product.id, "sku": product.sku}})
        return product

    def get_product(self, product_id: int):
        product = self.product_repo.get_by_id(product_id)
        if not product:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Producto no encontrado")
        return product

    def list_products(self, skip: int, limit: int, include_inactive: bool = False):
        return self.product_repo.get_all(skip, limit, include_inactive)

    def update_product(self, product_id: int, changes_in: ProductUpdate):
        product = self.get_product(product_id)
        if not product.is_active:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="El producto está dado de baja")
        changes = changes_in.model_dump(exclude_unset=True)
        for required in ("name", "unit"):
            if required in changes and changes[required] is None:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"{required} no puede quedar vacío")
        try:
            self.product_repo.update(product, changes)
            product.version = (product.version or 1) + 1
            self.db.flush()  # aplica updated_at antes de armar el evento
            self.outbox.add(events.PRODUCT_UPDATED, "product", product.sku, product_payload(product))
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="La base de datos rechazó la actualización")
        self.db.refresh(product)
        return product

    def delete_product(self, product_id: int) -> None:
        """
        BORRADO LÓGICO: no se elimina la fila, se marca is_active = False.
        ¿Por qué? Porque el historial de movimientos (compras/ventas) apunta a
        este producto y el PDF exige conservarlo para reportes.
        Solo se permite si no quedan unidades disponibles ni reservadas.
        """
        product = self.get_product(product_id)
        if not product.is_active:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="El producto ya estaba dado de baja")
        try:
            inventory = self.inventory_service.lock_inventory(product_id)  # FOR UPDATE
            if inventory.quantity > 0 or inventory.reserved_quantity > 0:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "No se puede dar de baja un producto con existencias. "
                        f"Disponible: {inventory.quantity}, reservado: {inventory.reserved_quantity}"
                    ),
                )
            product.is_active = False
            product.version = (product.version or 1) + 1
            self.db.flush()
            self.outbox.add(events.PRODUCT_DEACTIVATED, "product", product.sku, product_payload(product))
            self.db.commit()
        except HTTPException:
            self.db.rollback()
            raise
        self.inventory_service.invalidate_cache(product_id)
        logger.info("Producto dado de baja", extra={"event": "product_deactivated", "context": {"product_id": product_id}})
