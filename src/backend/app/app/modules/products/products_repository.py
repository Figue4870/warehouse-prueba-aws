from typing import List, Optional

from sqlalchemy.orm import Session

from app.modules.products.models import ProductEntity
from app.modules.products.schemas import ProductCreate


class ProductRepository:
    """Acceso a la tabla products. No hace commit: eso lo decide el servicio."""

    def __init__(self, db: Session):
        self.db = db

    def create(self, product_in: ProductCreate) -> ProductEntity:
        product = ProductEntity(**product_in.model_dump(), is_active=True, version=1)
        self.db.add(product)
        self.db.flush()  # Envía el INSERT para obtener el ID, sin confirmar la transacción
        return product

    def get_by_id(self, product_id: int) -> Optional[ProductEntity]:
        return self.db.get(ProductEntity, product_id)

    def get_by_sku(self, sku: str) -> Optional[ProductEntity]:
        return self.db.query(ProductEntity).filter(ProductEntity.sku == sku).first()

    def get_all(self, skip: int, limit: int, include_inactive: bool = False) -> List[ProductEntity]:
        query = self.db.query(ProductEntity)
        if not include_inactive:
            query = query.filter(ProductEntity.is_active.is_(True))
        return query.order_by(ProductEntity.id).offset(skip).limit(limit).all()

    def update(self, product: ProductEntity, changes: dict) -> ProductEntity:
        for field, value in changes.items():
            setattr(product, field, value)
        self.db.add(product)
        return product
