"""Cómo se ven los datos dentro de cada evento (payload)."""

from decimal import Decimal


def _money(value):
    """Dinero siempre como texto con 2 decimales: "1000.00" (nunca float)."""
    if value is None:
        return None
    return str(Decimal(str(value)).quantize(Decimal("0.01")))


def _iso(value):
    return value.isoformat() if value is not None else None


def product_payload(product) -> dict:
    return {
        "product_id": product.id,
        "sku": product.sku,
        "name": product.name,
        "description": product.description,
        "category": product.category,
        "unit": product.unit,
        "supplier": product.supplier,
        "lot": product.lot,
        "supplier_cost": _money(product.supplier_cost),
        "retail_price": _money(product.retail_price),
        "wholesale_price": _money(product.wholesale_price),
        "is_active": product.is_active,
        "product_version": product.version,
        "updated_at": _iso(product.updated_at),
    }


def movement_payload(movement, product, inventory) -> dict:
    return {
        "movement_id": movement.id,
        "product_id": product.id,
        "sku": product.sku,
        "product_name": product.name,
        "category": product.category,
        "unit": product.unit,
        "type": movement.type,
        "quantity": movement.quantity,
        "quantity_after": movement.quantity_after,
        "reserved_after": movement.reserved_after,
        "inventory_version": inventory.version,
        "lot": movement.lot,
        "reference": movement.reference,
        "supplier_cost": _money(product.supplier_cost),
        "retail_price": _money(product.retail_price),
        "created_at": _iso(movement.created_at),
    }


def alert_payload(alert, product) -> dict:
    return {
        "alert_id": alert.id,
        "alert_type": alert.type,
        "product_id": product.id,
        "sku": product.sku,
        "product_name": product.name,
        "message": alert.message,
        "movement_id": alert.movement_id,
    }
