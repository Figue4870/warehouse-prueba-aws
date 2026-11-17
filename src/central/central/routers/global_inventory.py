"""
Consultas GLOBALES. Dos formas de responder "¿cuánto hay del SKU X en toda la empresa?":

  B (la recomendada)  GET /global/inventory/{sku}
     Lee el MODELO DE LECTURA de la central. Una sola consulta local: rápida y
     disponible aunque alguna bodega esté caída. Contra: consistencia EVENTUAL
     (puede ir unos milisegundos/segundos atrás); por eso cada bodega trae "stale".

  A (para comparar)   GET /global/inventory/{sku}/live
     Pregunta EN VIVO a cada bodega por HTTP. Dato exacto de este instante, pero
     tan lento como la bodega más lenta y INCOMPLETO si alguna no responde.
"""

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from central.config import configured_api_urls, settings
from central.database import get_db
from central.models import CatalogProductEntity, CentralAlertEntity, WarehouseEntity, WarehouseInventoryEntity, WarehouseProductEntity
from central.projection import as_aware
from central.status import warehouse_status

router = APIRouter(prefix="/global", tags=["Global (CQRS)"])


def _money(value):
    return str(value) if value is not None else None


def _product_info(product: CatalogProductEntity | None) -> dict | None:
    if product is None:
        return None
    return {"sku": product.sku, "name": product.name, "category": product.category, "unit": product.unit,
            "supplier": product.supplier, "retail_price": _money(product.retail_price),
            "wholesale_price": _money(product.wholesale_price), "supplier_cost": _money(product.supplier_cost)}


@router.get("/inventory/{sku}", summary="Inventario global de un SKU (modelo de lectura, alternativa B)")
def global_inventory_by_sku(sku: str, db: Session = Depends(get_db)):
    started = time.perf_counter()
    now = datetime.now(timezone.utc)
    rows = (
        db.query(WarehouseInventoryEntity, WarehouseEntity, WarehouseProductEntity)
        .join(WarehouseEntity, WarehouseEntity.warehouse_id == WarehouseInventoryEntity.warehouse_id)
        .outerjoin(WarehouseProductEntity, (WarehouseProductEntity.warehouse_id == WarehouseInventoryEntity.warehouse_id)
                   & (WarehouseProductEntity.sku == WarehouseInventoryEntity.sku))
        .filter(WarehouseInventoryEntity.sku == sku)
        .order_by(WarehouseInventoryEntity.warehouse_id)
        .all()
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"El SKU {sku} no aparece en ninguna bodega")
    warehouses, warnings = [], []
    for inventory, warehouse, product in rows:
        state = warehouse_status(warehouse, now)
        if state["stale"]:
            warnings.append(f"{warehouse.warehouse_id}: {state['note']}")
        warehouses.append({
            "warehouse_id": warehouse.warehouse_id,
            "quantity": inventory.quantity,
            "reserved_quantity": inventory.reserved_quantity,
            "version": inventory.version,
            "is_active": product.is_active if product else True,
            "changed_in_warehouse_at": as_aware(inventory.source_updated_at),
            "known_by_central_at": as_aware(inventory.projected_at),
            "warehouse_status": state["status"],
            "stale": state["stale"],
        })
    return {
        "sku": sku,
        "product": _product_info(db.get(CatalogProductEntity, sku)),
        "total_quantity": sum(w["quantity"] for w in warehouses),
        "total_reserved": sum(w["reserved_quantity"] for w in warehouses),
        "warehouses": warehouses,
        "consistency": "eventual",
        "warnings": warnings,
        "source": "read-model",
        "query_ms": round((time.perf_counter() - started) * 1000, 2),
        "generated_at": now,
    }


@router.get("/inventory/{sku}/live", summary="Inventario de un SKU preguntando EN VIVO a cada bodega (alternativa A)")
def global_inventory_live(sku: str, db: Session = Depends(get_db)):
    started = time.perf_counter()
    targets = dict(configured_api_urls())
    for warehouse in db.query(WarehouseEntity).all():
        if warehouse.api_url and warehouse.warehouse_id not in targets:
            targets[warehouse.warehouse_id] = warehouse.api_url

    def ask(item):
        warehouse_id, url = item
        t0 = time.perf_counter()
        try:
            with httpx.Client(timeout=settings.LIVE_QUERY_TIMEOUT_SECONDS) as client:
                response = client.get(f"{url}/inventory/by-sku/{sku}")
            elapsed = round((time.perf_counter() - t0) * 1000, 1)
            if response.status_code == 404:
                return {"warehouse_id": warehouse_id, "ok": True, "found": False, "latency_ms": elapsed}
            response.raise_for_status()
            data = response.json()
            return {"warehouse_id": warehouse_id, "ok": True, "found": True, "latency_ms": elapsed,
                    "quantity": data["quantity"], "reserved_quantity": data["reserved_quantity"]}
        except Exception as error:  # noqa: BLE001 - bodega caída, timeout...
            return {"warehouse_id": warehouse_id, "ok": False, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "error": f"{type(error).__name__}: {error}"[:200]}

    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
        results = sorted(pool.map(ask, targets.items()), key=lambda r: r["warehouse_id"])
    found = [r for r in results if r.get("found")]
    failed = [r["warehouse_id"] for r in results if not r["ok"]]
    return {
        "sku": sku,
        "total_quantity": sum(r["quantity"] for r in found),
        "total_reserved": sum(r["reserved_quantity"] for r in found),
        "complete": not failed,
        "unreachable_warehouses": failed,
        "warehouses": results,
        "source": "live-fan-out",
        "query_ms": round((time.perf_counter() - started) * 1000, 2),
    }


@router.get("/inventory", summary="Inventario global de todos los SKU")
def global_inventory(limit: int = Query(200, ge=1, le=2000), offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    stale = {w.warehouse_id for w in db.query(WarehouseEntity).all() if warehouse_status(w, now)["stale"]}
    skus = [s for (s,) in db.query(WarehouseInventoryEntity.sku).distinct().order_by(WarehouseInventoryEntity.sku).offset(offset).limit(limit).all()]
    items = []
    for sku in skus:
        rows = db.query(WarehouseInventoryEntity).filter(WarehouseInventoryEntity.sku == sku).all()
        product = db.get(CatalogProductEntity, sku)
        items.append({
            "sku": sku,
            "name": product.name if product else None,
            "total_quantity": sum(r.quantity for r in rows),
            "total_reserved": sum(r.reserved_quantity for r in rows),
            "by_warehouse": {r.warehouse_id: r.quantity for r in rows},
            "stale_warehouses": sorted(r.warehouse_id for r in rows if r.warehouse_id in stale),
        })
    return {"items": items, "consistency": "eventual", "generated_at": now}


@router.get("/products", summary="Catálogo global (por SKU)")
def global_products(db: Session = Depends(get_db)):
    return [_product_info(p) for p in db.query(CatalogProductEntity).order_by(CatalogProductEntity.sku).all()]


@router.get("/alerts", summary="Alertas de todas las bodegas")
def global_alerts(limit: int = Query(50, ge=1, le=500), db: Session = Depends(get_db)):
    rows = db.query(CentralAlertEntity).order_by(CentralAlertEntity.occurred_at.desc()).limit(limit).all()
    return [{"warehouse_id": a.warehouse_id, "alert_type": a.alert_type, "sku": a.sku, "product_name": a.product_name,
             "message": a.message, "occurred_at": as_aware(a.occurred_at)} for a in rows]


@router.get("/summary", summary="Totales de la empresa")
def global_summary(db: Session = Depends(get_db)):
    totals = db.query(func.count(func.distinct(WarehouseInventoryEntity.sku)), func.coalesce(func.sum(WarehouseInventoryEntity.quantity), 0),
                      func.coalesce(func.sum(WarehouseInventoryEntity.reserved_quantity), 0)).one()
    return {"skus": totals[0], "total_quantity": int(totals[1]), "total_reserved": int(totals[2]),
            "warehouses": db.query(func.count(WarehouseEntity.warehouse_id)).scalar()}
