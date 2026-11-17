from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.cache import InventoryCache, get_inventory_cache
from app.config import settings
from app.database import get_db, sql_query_counter
from app.events.models import OutboxEventEntity
from app.modules.shared.utils.schemas_utils import current_utc_time

router = APIRouter(tags=["System"])


@router.get("/health", summary="Estado de la API y sus dependencias")
def health_check(response: Response, db: Session = Depends(get_db),
                 cache: InventoryCache = Depends(get_inventory_cache)):
    """
    - PostgreSQL caído  -> status "error" y HTTP 503 (la bodega no puede operar).
    - Redis caído       -> status "ok" y HTTP 200, con redis = "down": la API sigue
                           funcionando (más lenta) porque lee directo de PostgreSQL.
    """
    try:
        db.execute(text("SELECT 1"))
        database = "ok"
    except Exception:
        database = "down"
    redis_status = cache.status()  # ok | down | disabled

    outbox_pending = None
    if database == "ok":
        outbox_pending = db.query(func.count(OutboxEventEntity.id)).filter(OutboxEventEntity.published_at.is_(None)).scalar()

    healthy = database == "ok"
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ok" if healthy else "error",
        "warehouse_id": settings.WAREHOUSE_ID,
        "instance": settings.INSTANCE_NAME,  # Fase 5: qué copia de la API respondió
        "database": database,
        "redis": redis_status,
        # Fase 3: eventos aún no entregados a RabbitMQ (si crece, RabbitMQ o el relay están caídos)
        "outbox_pending": outbox_pending,
    }


def outbox_stats(db: Session) -> dict:
    pending = db.query(func.count(OutboxEventEntity.id), func.min(OutboxEventEntity.occurred_at)).filter(
        OutboxEventEntity.published_at.is_(None)
    ).one()
    oldest = pending[1]
    if oldest is not None and oldest.tzinfo is None:  # SQLite devuelve fechas sin zona
        oldest = oldest.replace(tzinfo=current_utc_time().tzinfo)
    return {
        "pending": pending[0],
        "oldest_pending_seconds": round((current_utc_time() - oldest).total_seconds(), 1) if oldest else 0,
        "last_sequence": db.query(func.max(OutboxEventEntity.sequence)).scalar() or 0,
        "published_total": db.query(func.count(OutboxEventEntity.id)).filter(OutboxEventEntity.published_at.isnot(None)).scalar(),
    }


@router.get("/metrics", summary="Métricas: caché (Fase 2) y outbox de eventos (Fase 3)")
def metrics(cache: InventoryCache = Depends(get_inventory_cache), db: Session = Depends(get_db)):
    return {
        "warehouse_id": settings.WAREHOUSE_ID,
        "instance": settings.INSTANCE_NAME,
        # Consultas SQL que ha ejecutado ESTE proceso desde el arranque/reset
        "sql_queries_total": sql_query_counter.value,
        "cache": cache.stats(),
        "outbox": outbox_stats(db),
    }


@router.post("/metrics/reset", summary="Poner en cero los contadores antes de un experimento")
def reset_metrics(cache: InventoryCache = Depends(get_inventory_cache), db: Session = Depends(get_db)):
    cache.reset_stats()
    result = metrics(cache, db)
    sql_query_counter.reset()  # al final, para no contar las consultas de este mismo endpoint
    result["sql_queries_total"] = 0
    return result
