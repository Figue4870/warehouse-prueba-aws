"""Qué tan confiable es la información de cada bodega en este momento."""

from datetime import datetime

from central.config import settings
from central.models import WarehouseEntity
from central.projection import as_aware

ONLINE, OFFLINE, UNKNOWN = "ONLINE", "OFFLINE", "UNKNOWN"


def warehouse_status(warehouse: WarehouseEntity, now: datetime) -> dict:
    heartbeat = as_aware(warehouse.last_heartbeat_at)
    seconds_since_contact = round((now - heartbeat).total_seconds(), 1) if heartbeat else None
    if heartbeat is None:
        status = UNKNOWN
    elif seconds_since_contact <= settings.ONLINE_AFTER_SECONDS:
        status = ONLINE
    else:
        status = OFFLINE
    lag = max(warehouse.reported_sequence or 0, warehouse.max_received_sequence or 0) - (warehouse.watermark_sequence or 0)
    stale = status != ONLINE or lag > 0
    if status == OFFLINE:
        note = f"Sin contacto hace {int(seconds_since_contact)} s: sus datos pueden estar desactualizados"
    elif status == UNKNOWN:
        note = "La bodega aún no ha enviado heartbeats"
    elif lag > 0:
        note = f"Faltan {lag} evento(s) por recibir (se recuperarán solos)"
    else:
        note = "Al día"
    return {
        "warehouse_id": warehouse.warehouse_id,
        "status": status,
        "stale": stale,
        "note": note,
        "seconds_since_contact": seconds_since_contact,
        "last_heartbeat_at": heartbeat,
        "last_event_at": as_aware(warehouse.last_event_at),
        "reported_sequence": warehouse.reported_sequence,
        "watermark_sequence": warehouse.watermark_sequence,
        "max_received_sequence": warehouse.max_received_sequence,
        "events_missing": max(lag, 0),
        "outbox_pending": warehouse.outbox_pending,
        "api_url": warehouse.api_url,
        "last_sync": {
            "at": as_aware(warehouse.last_sync_at),
            "kind": warehouse.last_sync_kind,
            "status": warehouse.last_sync_status,
            "error": warehouse.last_sync_error,
        },
    }
