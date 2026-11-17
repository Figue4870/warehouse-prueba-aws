import json
import logging
import sys
from datetime import datetime, timezone

from platform_common.correlation import get_correlation_id

from app.config import settings


class JsonFormatter(logging.Formatter):
    """
    Escribe cada log como una línea JSON (log estructurado).
    Así se puede buscar/filtrar por campos: event, product_id, warehouse_id...
    Ejemplo:
    {"timestamp": "...", "level": "WARNING", "event": "out_of_stock", "product_id": 3, ...}
    """

    service = "warehouse-api"

    def format(self, record: logging.LogRecord) -> str:
        data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "warehouse_id": settings.WAREHOUSE_ID,
            "logger": record.name,
            "message": record.getMessage(),
        }
        correlation_id = get_correlation_id()
        if correlation_id:
            data["correlation_id"] = correlation_id
        event = getattr(record, "event", None)
        if event:
            data["event"] = event
        context = getattr(record, "context", None)
        if isinstance(context, dict):
            data.update(context)
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False, default=str)


def setup_logging(level: int = logging.INFO, service: str = "warehouse-api") -> None:
    JsonFormatter.service = service
    # "warehouse" = código de la bodega; "platform" = librería compartida (RabbitMQ)
    for name in ("warehouse", "platform"):
        logger = logging.getLogger(name)
        if logger.handlers:  # evita duplicar handlers si se llama dos veces
            continue
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = False
