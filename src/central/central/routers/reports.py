"""
Reportes globales desde el modelo de lectura (CQRS): consultas que serían
lentas o imposibles sobre las bases de cada bodega, aquí son un SELECT local.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from central.database import get_db
from central.models import CatalogProductEntity, MovementHistoryEntity, WarehouseInventoryEntity

router = APIRouter(prefix="/reports", tags=["Reports (globales)"])


def _since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _money(value) -> str:
    return str(Decimal(value or 0).quantize(Decimal("0.01")))


@router.get("/stock-by-warehouse", summary="Stock de cada SKU en cada bodega y su valor")
def stock_by_warehouse(db: Session = Depends(get_db)):
    rows = (
        db.query(WarehouseInventoryEntity, CatalogProductEntity)
        .outerjoin(CatalogProductEntity, CatalogProductEntity.sku == WarehouseInventoryEntity.sku)
        .order_by(WarehouseInventoryEntity.sku, WarehouseInventoryEntity.warehouse_id)
        .all()
    )
    by_sku: dict[str, dict] = {}
    warehouses = sorted({inv.warehouse_id for inv, _ in rows})
    for inv, product in rows:
        item = by_sku.setdefault(inv.sku, {"sku": inv.sku, "name": product.name if product else None,
                                           "retail_price": _money(product.retail_price if product else 0),
                                           "by_warehouse": {}, "total_on_hand": 0})
        on_hand = inv.quantity + inv.reserved_quantity
        item["by_warehouse"][inv.warehouse_id] = on_hand
        item["total_on_hand"] += on_hand
    for item in by_sku.values():
        item["retail_value"] = _money(Decimal(item["retail_price"]) * item["total_on_hand"])
    return {"warehouses": warehouses, "rows": list(by_sku.values())}


@router.get("/sales-by-day", summary="Ventas (SHIPMENT) por día y bodega")
def sales_by_day(days: int = Query(30, ge=1, le=365), db: Session = Depends(get_db)):
    day = func.date(MovementHistoryEntity.occurred_at)
    rows = (
        db.query(day, MovementHistoryEntity.warehouse_id, func.sum(MovementHistoryEntity.quantity),
                 func.sum(MovementHistoryEntity.quantity * MovementHistoryEntity.unit_retail_price))
        .filter(MovementHistoryEntity.type == "SHIPMENT", MovementHistoryEntity.occurred_at >= _since(days))
        .group_by(day, MovementHistoryEntity.warehouse_id)
        .order_by(day, MovementHistoryEntity.warehouse_id)
        .all()
    )
    return [{"day": str(d), "warehouse_id": w, "units": int(u or 0), "sales_value": _money(v)} for d, w, u, v in rows]


@router.get("/top-products", summary="Productos más vendidos")
def top_products(days: int = Query(30, ge=1, le=365), limit: int = Query(10, ge=1, le=100), db: Session = Depends(get_db)):
    rows = (
        db.query(MovementHistoryEntity.sku, func.max(MovementHistoryEntity.product_name), func.sum(MovementHistoryEntity.quantity),
                 func.sum(MovementHistoryEntity.quantity * MovementHistoryEntity.unit_retail_price))
        .filter(MovementHistoryEntity.type == "SHIPMENT", MovementHistoryEntity.occurred_at >= _since(days))
        .group_by(MovementHistoryEntity.sku)
        .order_by(func.sum(MovementHistoryEntity.quantity).desc())
        .limit(limit)
        .all()
    )
    return [{"sku": s, "name": n, "units_sold": int(u or 0), "sales_value": _money(v)} for s, n, u, v in rows]


@router.get("/movements-by-type", summary="Movimientos por bodega y tipo")
def movements_by_type(days: int = Query(30, ge=1, le=365), db: Session = Depends(get_db)):
    rows = (
        db.query(MovementHistoryEntity.warehouse_id, MovementHistoryEntity.type, func.count(), func.sum(MovementHistoryEntity.quantity))
        .filter(MovementHistoryEntity.occurred_at >= _since(days))
        .group_by(MovementHistoryEntity.warehouse_id, MovementHistoryEntity.type)
        .order_by(MovementHistoryEntity.warehouse_id, MovementHistoryEntity.type)
        .all()
    )
    return [{"warehouse_id": w, "type": t, "movements": int(c), "units": int(u or 0)} for w, t, c, u in rows]
