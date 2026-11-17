import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from platform_common.correlation import new_correlation_id, reset_correlation_id, set_correlation_id
from sqlalchemy.exc import OperationalError

from central import models  # noqa: F401 - registra las tablas
from central.config import settings
from central.database import Base, engine
from central.logging_config import setup_logging
from central.routers import athena, dashboard, global_inventory, health, reports, sync, transfers, warehouses

logger = logging.getLogger("central.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging("central-api")
    if settings.AUTO_CREATE_TABLES:
        Base.metadata.create_all(bind=engine)
    logger.info("API central iniciada", extra={"event": "central_api_started"})
    yield


app = FastAPI(
    title="Warehouse Cloud Platform — Plataforma Central",
    description=(
        "Fase 7: visión consolidada de todas las bodegas con CQRS. Las bodegas son dueñas de su inventario; "
        "la central mantiene un MODELO DE LECTURA alimentado por sus eventos (consistencia eventual).\n\n"
        "Compara `/global/inventory/{sku}` (modelo de lectura) con `/global/inventory/{sku}/live` (consulta en vivo a cada bodega).\n\n"
        "Fase 6: transferencias entre bodegas con una Saga orquestada (`POST /transfers`).\n\n"
        "Reportes: `/reports/*` (modelo de lectura) y `/reports/athena/*` (Athena sobre S3). Tablero: `/dashboard`."
    ),
    version="7.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    correlation_id = request.headers.get("X-Correlation-ID") or new_correlation_id()
    token = set_correlation_id(correlation_id)
    try:
        response = await call_next(request)
    finally:
        reset_correlation_id(token)
    response.headers["X-Correlation-ID"] = correlation_id
    response.headers["X-Instance"] = settings.INSTANCE_NAME
    return response


@app.exception_handler(OperationalError)
async def database_unavailable(request: Request, exc: OperationalError):
    logger.error("Base de datos central no disponible", extra={"event": "database_unavailable"})
    return JSONResponse(status_code=503, content={"detail": "Base de datos central no disponible"})


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


app.include_router(health.router)
app.include_router(warehouses.router)
app.include_router(global_inventory.router)
app.include_router(sync.router)
app.include_router(reports.router)
app.include_router(transfers.router)
app.include_router(athena.router)
app.include_router(dashboard.router)
