from typing import List

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.cache import InventoryCache, get_inventory_cache
from app.database import get_db
from app.modules.products.products_service import ProductService
from app.modules.products.schemas import ProductCreate, ProductResponse, ProductUpdate

router = APIRouter(prefix="/products", tags=["Products"])


@router.post("", response_model=ProductResponse, status_code=status.HTTP_201_CREATED,
             summary="Crear producto (y su inventario en 0)")
def create_product(payload: ProductCreate, db: Session = Depends(get_db),
                   cache: InventoryCache = Depends(get_inventory_cache)):
    return ProductService(db, cache).create_product_with_inventory(payload)


@router.get("", response_model=List[ProductResponse], summary="Listar productos")
def get_products(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    include_inactive: bool = Query(False, description="Incluir productos dados de baja"),
    db: Session = Depends(get_db),
    cache: InventoryCache = Depends(get_inventory_cache),
):
    return ProductService(db, cache).list_products(skip, limit, include_inactive)


@router.get("/{product_id}", response_model=ProductResponse, summary="Consultar un producto")
def get_product(product_id: int, db: Session = Depends(get_db),
                cache: InventoryCache = Depends(get_inventory_cache)):
    return ProductService(db, cache).get_product(product_id)


@router.put("/{product_id}", response_model=ProductResponse, summary="Actualizar datos de un producto (no el SKU)")
def update_product(product_id: int, payload: ProductUpdate, db: Session = Depends(get_db),
                   cache: InventoryCache = Depends(get_inventory_cache)):
    return ProductService(db, cache).update_product(product_id, payload)


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Dar de baja un producto (borrado lógico)")
def delete_product(product_id: int, db: Session = Depends(get_db),
                   cache: InventoryCache = Depends(get_inventory_cache)):
    ProductService(db, cache).delete_product(product_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
