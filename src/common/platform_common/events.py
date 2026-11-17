"""
Contrato de eventos (README, sección 27).

Todo mensaje que viaja por RabbitMQ tiene este "sobre":

{
  "event_id": "7b1c...",                  <- único: sirve para idempotencia
  "event_type": "INVENTORY_MOVEMENT_RECORDED",
  "event_version": 1,                     <- para poder evolucionar el formato
  "occurred_at": "2026-11-17T15:00:00Z",
  "correlation_id": "COR-20261117-1A2B3C4D",
  "source": "warehouse-WH-001",
  "warehouse_id": "WH-001",
  "sequence": 152,                        <- orden por bodega, SIN huecos (lo pone el relay)
  "payload": { ... datos del evento ... }
}

La routing key se deriva del tipo: INVENTORY_MOVEMENT_RECORDED -> inventory.movement.recorded
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError

CURRENT_EVENT_VERSION = 1


class InvalidEventError(Exception):
    """El mensaje no cumple el contrato. Es un error PERMANENTE (no se reintenta)."""


class EventEnvelope(BaseModel):
    event_id: str = Field(..., min_length=1)
    event_type: str = Field(..., min_length=1)
    event_version: int = 1
    occurred_at: str
    correlation_id: Optional[str] = None
    source: str
    warehouse_id: Optional[str] = None
    sequence: Optional[int] = None
    payload: dict[str, Any] = Field(default_factory=dict)


def routing_key_for(event_type: str) -> str:
    return event_type.lower().replace("_", ".")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_event_id() -> str:
    return str(uuid.uuid4())


def build_event(
    event_type: str,
    source: str,
    payload: dict,
    warehouse_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    sequence: Optional[int] = None,
    event_id: Optional[str] = None,
    occurred_at: Optional[str] = None,
) -> dict:
    return {
        "event_id": event_id or new_event_id(),
        "event_type": event_type,
        "event_version": CURRENT_EVENT_VERSION,
        "occurred_at": occurred_at or utc_now_iso(),
        "correlation_id": correlation_id,
        "source": source,
        "warehouse_id": warehouse_id,
        "sequence": sequence,
        "payload": payload,
    }


def encode_event(event: dict) -> bytes:
    return json.dumps(event, ensure_ascii=False, default=str).encode("utf-8")


def parse_event(body: bytes) -> EventEnvelope:
    try:
        return EventEnvelope.model_validate(json.loads(body))
    except (ValueError, ValidationError) as error:
        raise InvalidEventError(f"Evento inválido: {error}") from error
