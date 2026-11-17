"""
SINCRONIZACIÓN central <-> bodegas: la red de seguridad para que NADA se pierda.

El camino normal de los datos es RabbitMQ (bodega -> forwarder -> central). Este
módulo cubre lo que puede fallar en ese camino:

  CATCH-UP  Si a la central le falta un evento (hay un hueco entre el watermark y
            la última secuencia que reporta la bodega, y no se llena solo en
            GAP_GRACE_SECONDS), le pide a la bodega los eventos que faltan con
            GET /sync/events?after_sequence=<watermark>. Cada evento se aplica con la
            MISMA función idempotente que usan los mensajes de RabbitMQ.

  SNAPSHOT  La primera vez que la central conoce una bodega (o si su base se
            perdió), pide la foto completa con GET /sync/snapshot. Sirve para datos
            que existían antes de que hubiera eventos (por ejemplo, seed.sql).
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx
from platform_common.events import EventEnvelope
from sqlalchemy.orm import Session, sessionmaker

from central.config import configured_api_urls, settings
from central.models import SyncRunEntity, WarehouseEntity
from central.projection import APPLIED, apply_event, apply_snapshot, as_aware, get_or_create_warehouse, now_utc

logger = logging.getLogger("central.sync")


class WarehouseClient:
    """Llama por HTTP a la API de una bodega (a través de su balanceador)."""

    def __init__(self, timeout: float = settings.SYNC_HTTP_TIMEOUT_SECONDS, transport=None):
        self.timeout = timeout
        self.transport = transport  # en las pruebas se reemplaza por uno falso

    def _get(self, url: str, params=None) -> dict:
        with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            return response.json()

    def events_after(self, api_url: str, after_sequence: int, limit: int) -> dict:
        return self._get(f"{api_url}/sync/events", {"after_sequence": after_sequence, "limit": limit})

    def snapshot(self, api_url: str) -> dict:
        return self._get(f"{api_url}/sync/snapshot")


def api_url_for(warehouse: WarehouseEntity) -> str | None:
    return configured_api_urls().get(warehouse.warehouse_id) or warehouse.api_url


def _finish(session_factory: sessionmaker, warehouse_id: str, kind: str, status: str, applied: int, detail: str | None, started: datetime):
    with session_factory() as db:
        db.add(SyncRunEntity(warehouse_id=warehouse_id, kind=kind, status=status, events_applied=applied,
                             detail=(detail or "")[:2000], started_at=started, finished_at=now_utc()))
        warehouse = get_or_create_warehouse(db, warehouse_id)
        warehouse.last_sync_at = now_utc()
        warehouse.last_sync_kind = kind
        warehouse.last_sync_status = status
        warehouse.last_sync_error = detail if status == "ERROR" else None
        db.commit()


def catch_up(session_factory: sessionmaker, warehouse_id: str, client: WarehouseClient) -> int:
    """Pide a la bodega los eventos desde el watermark y los aplica. Devuelve cuántos aplicó."""
    started = now_utc()
    applied = 0
    try:
        with session_factory() as db:
            warehouse = get_or_create_warehouse(db, warehouse_id)
            api_url = api_url_for(warehouse)
            after = warehouse.watermark_sequence
            db.commit()
        if not api_url:
            raise RuntimeError("No se conoce la URL de la bodega (aún no envía heartbeats)")
        while True:
            page = client.events_after(api_url, after, settings.SYNC_PAGE_SIZE)
            oldest = page.get("oldest_available_sequence")
            if oldest and oldest > after + 1:
                # Los eventos que faltan ya se borraron del outbox (más viejos que la retención):
                # el inventario se recupera con un snapshot; el historial de esos días queda incompleto.
                logger.warning("Eventos ya no disponibles en la bodega; se usará snapshot",
                               extra={"event": "sync_history_gap", "context": {"warehouse_id": warehouse_id, "from": after + 1, "to": oldest - 1}})
                snapshot_sync(session_factory, warehouse_id, client)
                with session_factory() as db:
                    warehouse = get_or_create_warehouse(db, warehouse_id, lock=True)
                    warehouse.watermark_sequence = max(warehouse.watermark_sequence, oldest - 1)
                    db.commit()
                after = oldest - 1
            events = page.get("events", [])
            if not events:
                break
            for raw in events:
                with session_factory() as db:
                    if apply_event(db, EventEnvelope.model_validate(raw), source="pull") == APPLIED:
                        applied += 1
                    db.commit()
                after = max(after, raw.get("sequence") or after)
            if len(events) < settings.SYNC_PAGE_SIZE:
                break
        _finish(session_factory, warehouse_id, "CATCH_UP", "OK", applied, None, started)
        if applied:
            logger.info("Huecos recuperados desde la bodega",
                        extra={"event": "sync_catch_up", "context": {"warehouse_id": warehouse_id, "events_applied": applied}})
        return applied
    except Exception as error:  # noqa: BLE001 - la bodega puede estar caída: se reintenta en la próxima vuelta
        _finish(session_factory, warehouse_id, "CATCH_UP", "ERROR", applied, f"{type(error).__name__}: {error}", started)
        logger.warning("No se pudo sincronizar con la bodega",
                       extra={"event": "sync_failed", "context": {"warehouse_id": warehouse_id, "error": str(error)}})
        return applied


def snapshot_sync(session_factory: sessionmaker, warehouse_id: str, client: WarehouseClient) -> int:
    started = now_utc()
    try:
        with session_factory() as db:
            api_url = api_url_for(get_or_create_warehouse(db, warehouse_id))
            db.commit()
        if not api_url:
            raise RuntimeError("No se conoce la URL de la bodega")
        snapshot = client.snapshot(api_url)
        with session_factory() as db:
            changed = apply_snapshot(db, warehouse_id, snapshot)
            db.commit()
        _finish(session_factory, warehouse_id, "SNAPSHOT", "OK", changed, f"{len(snapshot.get('products', []))} productos", started)
        logger.info("Snapshot aplicado", extra={"event": "sync_snapshot", "context": {"warehouse_id": warehouse_id, "changed": changed}})
        return changed
    except Exception as error:  # noqa: BLE001
        _finish(session_factory, warehouse_id, "SNAPSHOT", "ERROR", 0, f"{type(error).__name__}: {error}", started)
        logger.warning("No se pudo tomar el snapshot", extra={"event": "sync_failed", "context": {"warehouse_id": warehouse_id, "error": str(error)}})
        return 0


def needs_catch_up(warehouse: WarehouseEntity, now: datetime) -> bool:
    behind = max(warehouse.reported_sequence or 0, warehouse.max_received_sequence or 0) > (warehouse.watermark_sequence or 0)
    waited = now - as_aware(warehouse.watermark_updated_at) > timedelta(seconds=settings.GAP_GRACE_SECONDS)
    return behind and waited


def reconcile_once(session_factory: sessionmaker, client: WarehouseClient, now: datetime | None = None) -> list[str]:
    """Una vuelta del reconciliador. Devuelve lo que hizo (para logs y pruebas)."""
    now = now or datetime.now(timezone.utc)
    actions = []
    with session_factory() as db:
        for warehouse_id in configured_api_urls():
            get_or_create_warehouse(db, warehouse_id)
        db.commit()
        warehouses = db.query(WarehouseEntity).order_by(WarehouseEntity.warehouse_id).all()
        db.expunge_all()
    for warehouse in warehouses:
        if api_url_for(warehouse) is None:
            continue
        if warehouse.last_snapshot_at is None:
            snapshot_sync(session_factory, warehouse.warehouse_id, client)
            actions.append(f"snapshot:{warehouse.warehouse_id}")
        if needs_catch_up(warehouse, now):
            catch_up(session_factory, warehouse.warehouse_id, client)
            actions.append(f"catch_up:{warehouse.warehouse_id}")
    return actions


