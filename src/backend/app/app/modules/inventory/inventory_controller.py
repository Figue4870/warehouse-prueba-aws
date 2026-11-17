from typing import List

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.cache import InventoryCache, get_inventory_cache
from app.database import get_db
from app.modules.inventory.inventory_service import InventoryService
from app.modules.inventory.schemas import InventoryResponse, MovementCreate, MovementResponse, MovementResult

router = APIRouter(prefix="/inventory", tags=["Inventory"])


@router.get("/by-sku/{sku}", summary="Saldo por SKU, leído en vivo de PostgreSQL (Fase 7: lo usa la consulta 'live' de la central)")
def get_inventory_by_sku(sku: str, db: Session = Depends(get_db),
                         cache: InventoryCache = Depends(get_inventory_cache)):
    return InventoryService(db, cache).get_stock_by_sku(sku)


@router.get("/{product_id}", response_model=InventoryResponse,
            summary="Consultar saldo de un producto (usa caché Redis)")
def get_inventory(product_id: int, response: Response, db: Session = Depends(get_db),
                  cache: InventoryCache = Depends(get_inventory_cache)):
    data, cache_status = InventoryService(db, cache).get_stock(product_id)
    # Cabecera para VER el caché en acción: HIT, MISS o BYPASS
    response.headers["X-Cache"] = cache_status
    return data


@router.post("/movements", response_model=MovementResult, status_code=status.HTTP_201_CREATED,
             summary="Registrar entrada, salida, ajuste, reserva...")
def create_movement(payload: MovementCreate, db: Session = Depends(get_db),
                    cache: InventoryCache = Depends(get_inventory_cache)):
    # El controlador solo delega: reglas, transacción e invalidación viven en el servicio.
    return InventoryService(db, cache).process_movement(payload)


@router.get("/{product_id}/movements", response_model=List[MovementResponse],
            summary="Historial de movimientos (más reciente primero)")
def get_movements(
    product_id: int,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    cache: InventoryCache = Depends(get_inventory_cache),
):
    return InventoryService(db, cache).get_movements(product_id, limit, offset)
