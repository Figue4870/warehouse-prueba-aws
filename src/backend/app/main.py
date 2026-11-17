import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from platform_common.correlation import new_correlation_id, reset_correlation_id, set_correlation_id

from app.config import settings
from app.database import Base, engine
from app.logging_config import setup_logging
from app.modules.alerts.alerts_controller import router as alerts_controller
from app.modules.health.health_controller import router as health_controller
from app.modules.inventory.inventory_controller import router as inventory_controller
from app.modules.products.products_controller import router as products_controller
from app.modules.reports.reports_controller import router as reports_controller
from app.modules.sync.sync_controller import router as sync_controller
from app.modules.transfers.transfers_controller import router as transfers_controller

logger = logging.getLogger("warehouse.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    if settings.AUTO_CREATE_TABLES:
        # Solo para correr sin Docker. En Docker las tablas las crea db/schema.sql
        Base.metadata.create_all(bind=engine)
    logger.info(
        "API de bodega iniciada",
        extra={
            "event": "api_started",
            "context": {"cache_enabled": settings.CACHE_ENABLED, "cache_ttl_seconds": settings.CACHE_TTL_SECONDS},
        },
    )
    yield


app = FastAPI(
    title=f"Warehouse Cloud Platform — Bodega {settings.WAREHOUSE_ID}",
    description=(
        "Fase 1: API + PostgreSQL (transacciones, inventario nunca negativo, historial).\n\n"
        "Fase 2: Redis Cache-Aside en GET /inventory/{product_id} (cabecera X-Cache: HIT/MISS/BYPASS).\n\n"
        "Fase 3: eventos con Transactional Outbox + RabbitMQ, workers y reportes asíncronos (POST /reports -> 202).\n\n"
        "Fase 5: varias instancias idénticas (stateless) detrás de un balanceador; cabecera X-Instance.\n\n"
        "Fase 6: las transferencias entre bodegas las orquesta la central (Saga); el command-worker ejecuta cada paso "
        "aquí y /transfer-operations muestra lo que se hizo.\n\n"
        "Fase 7: los eventos se reenvían a la central; /sync permite a la central recuperar huecos."
    ),
    version="7.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    """Fase 3: cada petición lleva un correlation_id (se respeta el que mande el cliente)."""
    correlation_id = request.headers.get("X-Correlation-ID") or new_correlation_id()
    token = set_correlation_id(correlation_id)
    try:
        response = await call_next(request)
    finally:
        reset_correlation_id(token)
    response.headers["X-Correlation-ID"] = correlation_id
    # Fase 5: qué instancia de la API atendió (para ver el balanceo de carga)
    response.headers["X-Instance"] = settings.INSTANCE_NAME
    return response


@app.exception_handler(OperationalError)
async def database_unavailable_handler(request: Request, exc: OperationalError):
    # PostgreSQL caído o inalcanzable: 503 = "servicio no disponible, intenta más tarde"
    logger.error("Base de datos no disponible", extra={"event": "database_unavailable", "context": {"path": request.url.path}})
    return JSONResponse(status_code=503, content={"detail": "Base de datos no disponible, intenta más tarde"})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Nunca devolver detalles internos (trazas, SQL) al cliente: solo al log.
    logger.exception("Error no controlado", extra={"event": "unhandled_error", "context": {"path": request.url.path}})
    return JSONResponse(status_code=500, content={"detail": "Error interno del servidor"})


app.include_router(health_controller)
app.include_router(products_controller)
app.include_router(inventory_controller)
app.include_router(alerts_controller)
app.include_router(reports_controller)
app.include_router(sync_controller)
app.include_router(transfers_controller)
