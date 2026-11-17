"""
EXPORTADOR para Athena: copia el modelo de lectura de la central a S3 como
archivos JSON por líneas, particionados por día (dt=YYYY-MM-DD).

    s3://<bucket>/movements/dt=2026-11-16/<lote>.json     <- movement_history
    s3://<bucket>/transfers/dt=2026-11-16/<lote>.json     <- transferencias terminadas (Fase 6)
    s3://<bucket>/inventory/dt=2026-11-16/snapshot-...json <- foto del inventario cada EXPORT_SNAPSHOT_SECONDS

Athena lee esos archivos directamente (no hay que cargar nada en una base):
la consulta paga solo por lo que lee, y la partición dt hace que una consulta
de "los últimos 30 días" lea solo esos 30 días.

SIN DUPLICADOS aunque el exportador se caiga a la mitad:
  1. se ARMA el lote: cada fila recibe export_batch_id (COMMIT)
  2. se SUBE el lote: archivos con nombre <lote>.json (nombre fijo)
  3. se marca uploaded_at (COMMIT)
  Si se cae entre 1 y 3, en la siguiente vuelta se vuelve a subir el MISMO
  lote con el MISMO nombre: el archivo se reemplaza, no se duplica.
"""

import json
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from central.models import (
    CatalogProductEntity,
    ExportBatchEntity,
    MovementHistoryEntity,
    TransferEntity,
    WarehouseEntity,
    WarehouseInventoryEntity,
)
from central.projection import as_aware
from central.saga import TERMINAL, public_status
from central.status import warehouse_status

logger = logging.getLogger("central.exporter")

TABLES = {name: spec for name, spec in json.loads((Path(__file__).parent / "tables.json").read_text(encoding="utf-8")).items()
          if not name.startswith("_")}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(value) -> str | None:
    """2026-11-16T20:15:00.123Z (milisegundos y UTC: lo entiende from_iso8601_timestamp de Athena)."""
    if value is None:
        return None
    value = as_aware(value).astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def day(value) -> str:
    return as_aware(value).astimezone(timezone.utc).strftime("%Y-%m-%d")


def money(value) -> str | None:
    return f"{value:.2f}" if value is not None else None


# ---------------------------------------------------------------- filas (deben coincidir con tables.json)
def movement_row(m: MovementHistoryEntity) -> dict:
    return {
        "event_id": m.event_id, "warehouse_id": m.warehouse_id, "sequence": m.sequence, "movement_id": m.movement_id,
        "sku": m.sku, "product_name": m.product_name, "category": m.category, "type": m.type, "quantity": m.quantity,
        "quantity_after": m.quantity_after, "reserved_after": m.reserved_after,
        "unit_retail_price": money(m.unit_retail_price), "unit_supplier_cost": money(m.unit_supplier_cost),
        "lot": m.lot, "reference": m.reference, "occurred_at": iso(m.occurred_at),
    }


def transfer_row(t: TransferEntity) -> dict:
    duration = (as_aware(t.finished_at) - as_aware(t.created_at)).total_seconds() if t.finished_at else None
    return {
        "transfer_id": t.transfer_id, "sku": t.sku, "source_warehouse_id": t.source_warehouse_id,
        "destination_warehouse_id": t.destination_warehouse_id, "quantity": t.quantity, "final_state": t.state,
        "status": public_status(t.state), "failure_reason": t.failure_reason, "needs_review": bool(t.needs_review),
        "created_at": iso(t.created_at), "finished_at": iso(t.finished_at),
        "duration_seconds": round(duration, 3) if duration is not None else None,
    }


DATASETS = {
    # nombre: (modelo, fila, fecha para la partición, filtro extra)
    "movements": (MovementHistoryEntity, movement_row, lambda m: m.occurred_at, None),
    "transfers": (TransferEntity, transfer_row, lambda t: t.created_at, TransferEntity.state.in_(sorted(TERMINAL))),
}


def to_jsonl(rows: list[dict]) -> bytes:
    return ("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n").encode("utf-8")


# ---------------------------------------------------------------- lotes
def _upload(db: Session, storage, batch: ExportBatchEntity, records: list) -> None:
    model, to_row, date_of, _ = DATASETS[batch.dataset]
    by_day: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        by_day[day(date_of(record))].append(to_row(record))
    keys = []
    for dt, rows in sorted(by_day.items()):
        key = f"{batch.dataset}/dt={dt}/{batch.batch_id}.json"
        storage.put(key, to_jsonl(rows))
        keys.append(key)
    batch.object_keys = keys
    batch.row_count = len(records)
    batch.uploaded_at = now_utc()
    batch.last_error = None


def export_dataset(session_factory: sessionmaker, storage, dataset: str, batch_size: int = 5000) -> int:
    """Exporta lo que falte de un dataset. Devuelve cuántas filas subió."""
    model, _, date_of, extra_filter = DATASETS[dataset]
    uploaded = 0
    with session_factory() as db:
        # 1) Lotes que quedaron a medias (el exportador se cayó): se vuelven a subir igual
        for batch in db.query(ExportBatchEntity).filter(ExportBatchEntity.dataset == dataset,
                                                        ExportBatchEntity.uploaded_at.is_(None)).all():
            records = db.query(model).filter(model.export_batch_id == batch.batch_id).all()
            _upload_or_record_error(db, storage, batch, records)
            uploaded += len(records)

        # 2) Lote nuevo con las filas que nunca se exportaron
        query = db.query(model).filter(model.export_batch_id.is_(None))
        if extra_filter is not None:
            query = query.filter(extra_filter)
        query = query.limit(batch_size)
        if db.bind.dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        records = query.all()
        if not records:
            return uploaded
        batch = ExportBatchEntity(batch_id=str(uuid.uuid4()), dataset=dataset, row_count=len(records), created_at=now_utc())
        db.add(batch)
        for record in records:
            record.export_batch_id = batch.batch_id
        db.commit()  # el lote queda "apartado" ANTES de subirlo
        _upload_or_record_error(db, storage, batch, records)
        uploaded += len(records)
    return uploaded


def _upload_or_record_error(db: Session, storage, batch: ExportBatchEntity, records: list) -> None:
    try:
        _upload(db, storage, batch, records)
        db.commit()
    except Exception as error:  # noqa: BLE001 - S3 caído: se reintenta en la próxima vuelta con el mismo lote
        db.rollback()
        batch = db.get(ExportBatchEntity, batch.batch_id)
        batch.last_error = f"{type(error).__name__}: {error}"[:500]
        db.commit()
        raise


# ---------------------------------------------------------------- foto del inventario
def export_inventory_snapshot(session_factory: sessionmaker, storage, now: datetime | None = None) -> int:
    now = now or now_utc()
    with session_factory() as db:
        warehouses = {w.warehouse_id: warehouse_status(w, now)["status"] for w in db.query(WarehouseEntity).all()}
        rows = (db.query(WarehouseInventoryEntity, CatalogProductEntity)
                .outerjoin(CatalogProductEntity, CatalogProductEntity.sku == WarehouseInventoryEntity.sku)
                .order_by(WarehouseInventoryEntity.warehouse_id, WarehouseInventoryEntity.sku).all())
        snapshot_at = iso(now)
        data = [{
            "snapshot_at": snapshot_at, "warehouse_id": inv.warehouse_id, "sku": inv.sku,
            "product_name": product.name if product else None, "category": product.category if product else None,
            "quantity": inv.quantity, "reserved_quantity": inv.reserved_quantity,
            "retail_price": money(product.retail_price) if product else None,
            "supplier_cost": money(product.supplier_cost) if product else None,
            "warehouse_status": warehouses.get(inv.warehouse_id),
        } for inv, product in rows]
        key = f"inventory/dt={day(now)}/snapshot-{now.strftime('%H%M%S')}.json"
        if data:
            storage.put(key, to_jsonl(data))
        db.add(ExportBatchEntity(batch_id=str(uuid.uuid4()), dataset="inventory", row_count=len(data),
                                 object_keys=[key] if data else [], created_at=now, uploaded_at=now))
        db.commit()
        return len(data)


def last_snapshot_at(session_factory: sessionmaker):
    with session_factory() as db:
        batch = (db.query(ExportBatchEntity).filter(ExportBatchEntity.dataset == "inventory")
                 .order_by(ExportBatchEntity.created_at.desc()).first())
        return as_aware(batch.created_at) if batch else None


class Exporter:
    def __init__(self, session_factory: sessionmaker, storage, batch_size: int = 5000, snapshot_seconds: int = 3600):
        self.session_factory = session_factory
        self.storage = storage
        self.batch_size = batch_size
        self.snapshot_seconds = snapshot_seconds

    def run_once(self, now: datetime | None = None) -> dict:
        now = now or now_utc()
        result = {name: export_dataset(self.session_factory, self.storage, name, self.batch_size) for name in DATASETS}
        last = last_snapshot_at(self.session_factory)
        if last is None or (now - last).total_seconds() >= self.snapshot_seconds:
            result["inventory"] = export_inventory_snapshot(self.session_factory, self.storage, now)
        return result


def export_status(db: Session) -> dict:
    """Para la API: cuánto se ha exportado y cuánto falta."""
    status = {}
    for name, (model, _, _, extra_filter) in DATASETS.items():
        pending = db.query(model).filter(model.export_batch_id.is_(None))
        if extra_filter is not None:
            pending = pending.filter(extra_filter)
        status[name] = {"pending_rows": pending.count(),
                        "exported_rows": db.query(model).filter(model.export_batch_id.isnot(None)).count()}
    last = (db.query(ExportBatchEntity).filter(ExportBatchEntity.uploaded_at.isnot(None))
            .order_by(ExportBatchEntity.uploaded_at.desc()).first())
    status["last_upload_at"] = as_aware(last.uploaded_at) if last else None
    errors = db.query(ExportBatchEntity).filter(ExportBatchEntity.uploaded_at.is_(None)).count()
    status["batches_waiting_retry"] = errors
    return status
