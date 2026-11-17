import socket

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5440/central_db"
    AUTO_CREATE_TABLES: bool = True               # en Docker = false (lo hace db/central_schema.sql)
    AMQP_URL: str = "amqp://platform:platform@localhost:5680/%2F"   # RabbitMQ CENTRAL
    INSTANCE_NAME: str = socket.gethostname()

    # Una bodega sin heartbeat hace más de esto se marca OFFLINE ("datos posiblemente desactualizados")
    ONLINE_AFTER_SECONDS: int = 45
    # Cada cuánto el reconciliador revisa huecos de secuencia y bodegas nuevas
    RECONCILE_SECONDS: int = 15
    # Cuánto esperar antes de considerar que un hueco NO se va a llenar solo (y pedirlo por HTTP)
    GAP_GRACE_SECONDS: int = 20
    SYNC_HTTP_TIMEOUT_SECONDS: float = 5.0
    SYNC_PAGE_SIZE: int = 500
    LIVE_QUERY_TIMEOUT_SECONDS: float = 2.0       # consulta "en vivo" a las bodegas (alternativa A)
    # Opcional: "WH-001=http://10.0.1.10:8001,WH-002=..." (si no, se aprenden de los heartbeats)
    WAREHOUSE_API_URLS: str = ""

    WORKER_MAX_RETRIES: int = 5
    WORKER_RETRY_DELAY_MS: int = 5000

    # ---------- Fase 6: Saga de transferencias ----------
    SAGA_STEP_TIMEOUT_SECONDS: int = 60   # cuánto puede tardar una bodega en reservar / recibir
    SAGA_GRACE_SECONDS: int = 5           # margen extra antes de compensar (relojes, red)
    SAGA_TICK_SECONDS: float = 1.0        # cada cuánto el relay de comandos publica y revisa vencimientos
    SAGA_ALLOW_SIMULATION: bool = True    # permitir "simulate_failure" en POST /transfers (experimentos de la guía)

    # ---------- Reportes con Athena (exportación a S3) ----------
    EXPORT_S3_BUCKET: str = ""            # vacío = se exporta a la carpeta EXPORT_DIR (desarrollo local)
    EXPORT_DIR: str = "exports"
    EXPORT_INTERVAL_SECONDS: int = 60
    EXPORT_BATCH_SIZE: int = 5000
    EXPORT_SNAPSHOT_SECONDS: int = 3600   # foto del inventario (tabla inventory de Athena)
    ATHENA_DATABASE: str = "warehouse_analytics"
    ATHENA_WORKGROUP: str = ""            # vacío = Athena no configurado (solo en AWS)
    ATHENA_MAX_ROWS: int = 1000
    AWS_DEFAULT_REGION: str = "us-east-1"


settings = Settings()


def configured_api_urls() -> dict[str, str]:
    urls = {}
    for item in filter(None, (part.strip() for part in settings.WAREHOUSE_API_URLS.split(","))):
        warehouse_id, url = item.split("=", 1)
        urls[warehouse_id.strip()] = url.strip().rstrip("/")
    return urls
