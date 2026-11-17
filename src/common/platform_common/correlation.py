"""
correlation_id: un identificador que acompaña a una operación por TODOS los
componentes (HTTP -> API -> outbox -> RabbitMQ -> worker -> logs).
Con él se puede buscar en los logs todo lo que pasó por una sola petición.

Se guarda en una ContextVar: cada request / cada mensaje tiene su propio valor,
aunque se atiendan muchos al mismo tiempo.
"""

import contextvars
import uuid
from datetime import datetime, timezone
from typing import Optional

_correlation_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("correlation_id", default=None)


def new_correlation_id() -> str:
    """Formato del README: COR-20260928-1A2B3C4D"""
    return f"COR-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"


def get_correlation_id() -> Optional[str]:
    return _correlation_id.get()


def set_correlation_id(value: Optional[str]) -> contextvars.Token:
    return _correlation_id.set(value)


def reset_correlation_id(token: contextvars.Token) -> None:
    _correlation_id.reset(token)
