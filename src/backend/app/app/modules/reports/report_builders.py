"""
Cálculo de cada reporte. Lo ejecuta el report-worker (NUNCA la API), porque un
reporte puede tardar: la API solo responde 202 y el trabajo pesado ocurre aparte.
"""

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.modules.inventory.models import InventoryEntity, InventoryMovementEntity
from app.modules.products.models import ProductEntity
from app.modules.shared.errors import BusinessRuleError

OUTBOUND = {"SHIPMENT", "TRANSFER_OUT"}


def _money(value) -> str:
    return str(Decimal(value or 0).quantize(Decimal("0.01")))


def movements_summary(db: Session, params: dict) -> dict:
    today = datetime.now(timezone.utc).date()
    date_from = datetime.fromisoformat(params["date_from"]).date() if params.get("date_from") else today - timedelta(days=30)
    date_to = datetime.fromisoformat(params["date_to"]).date() if params.get("date_to") else today
    start = datetime.combine(date_from, time.min, tzinfo=timezone.utc)
    end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=timezone.utc)

    rows = (
        db.query(
            ProductEntity.sku,
            ProductEntity.name,
            ProductEntity.retail_price,
            InventoryMovementEntity.type,
            func.count(InventoryMovementEntity.id),
            func.sum(InventoryMovementEntity.quantity),
        )
        .join(ProductEntity, ProductEntity.id == InventoryMovementEntity.product_id)
        .filter(
            InventoryMovementEntity.warehouse_id == settings.WAREHOUSE_ID,
            InventoryMovementEntity.created_at >= start,
            InventoryMovementEntity.created_at < end,
        )
        .group_by(ProductEntity.sku, ProductEntity.name, ProductEntity.retail_price, InventoryMovementEntity.type)
        .order_by(ProductEntity.sku, InventoryMovementEntity.type)
        .all()
    )
    result_rows, totals = [], {}
    sales_value = Decimal(0)
    for sku, name, retail_price, m_type, count, units in rows:
        units = int(units or 0)
        value = Decimal(retail_price or 0) * units if m_type == "SHIPMENT" else Decimal(0)
        sales_value += value
        totals[m_type] = totals.get(m_type, 0) + units
        result_rows.append({"sku": sku, "name": name, "type": m_type, "movements": int(count), "units": units, "sales_value": _money(value)})
    return {
        "columns": ["sku", "name", "type", "movements", "units", "sales_value"],
        "rows": result_rows,
        "totals": {"units_by_type": totals, "sales_value": _money(sales_value)},
        "range": {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()},
    }


def stock_valuation(db: Session, params: dict) -> dict:
    rows = (
        db.query(ProductEntity, InventoryEntity)
        .join(InventoryEntity, InventoryEntity.product_id == ProductEntity.id)
        .filter(ProductEntity.is_active.is_(True), InventoryEntity.warehouse_id == settings.WAREHOUSE_ID)
        .order_by(ProductEntity.sku)
        .all()
    )
    result_rows = []
    total_cost = total_retail = Decimal(0)
    for product, inventory in rows:
        on_hand = inventory.quantity + inventory.reserved_quantity
        cost_value = Decimal(product.supplier_cost or 0) * on_hand
        retail_value = Decimal(product.retail_price or 0) * on_hand
        total_cost += cost_value
        total_retail += retail_value
        result_rows.append({
            "sku": product.sku, "name": product.name, "available": inventory.quantity,
            "reserved": inventory.reserved_quantity, "on_hand": on_hand,
            "supplier_cost": _money(product.supplier_cost), "retail_price": _money(product.retail_price),
            "cost_value": _money(cost_value), "retail_value": _money(retail_value),
        })
    return {
        "columns": ["sku", "name", "available", "reserved", "on_hand", "supplier_cost", "retail_price", "cost_value", "retail_value"],
        "rows": result_rows,
        "totals": {"cost_value": _money(total_cost), "retail_value": _money(total_retail), "products": len(result_rows)},
    }


def low_stock(db: Session, params: dict) -> dict:
    threshold = int(params.get("threshold", 5))
    rows = (
        db.query(ProductEntity, InventoryEntity)
        .join(InventoryEntity, InventoryEntity.product_id == ProductEntity.id)
        .filter(ProductEntity.is_active.is_(True), InventoryEntity.warehouse_id == settings.WAREHOUSE_ID,
                InventoryEntity.quantity <= threshold)
        .order_by(InventoryEntity.quantity, ProductEntity.sku)
        .all()
    )
    return {
        "columns": ["sku", "name", "available", "reserved", "supplier"],
        "rows": [{"sku": p.sku, "name": p.name, "available": i.quantity, "reserved": i.reserved_quantity, "supplier": p.supplier} for p, i in rows],
        "totals": {"products": len(rows), "threshold": threshold},
    }


BUILDERS = {
    "MOVEMENTS_SUMMARY": movements_summary,
    "STOCK_VALUATION": stock_valuation,
    "LOW_STOCK": low_stock,
}


def build_report(db: Session, report_type: str, params: dict) -> dict:
    builder = BUILDERS.get(report_type)
    if builder is None:
        raise BusinessRuleError(f"Tipo de reporte desconocido: {report_type}")
    data = builder(db, params or {})
    data.update({
        "report_type": report_type,
        "warehouse_id": settings.WAREHOUSE_ID,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    })
    return data
