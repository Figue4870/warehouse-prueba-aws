import socket

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Configuración de la API. Cada valor se puede cambiar con una variable de
    entorno del mismo nombre (en Docker se definen en el docker-compose / .env).
    Nunca escribir contraseñas reales aquí: estos valores son solo para desarrollo.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---------- Fase 1: identidad de la bodega + PostgreSQL ----------
    WAREHOUSE_ID: str = "WH-001"  # Identificador de esta bodega autónoma
    # Fase 5: nombre de ESTA copia de la API (api-1, api-2, api-3). Si no se da, el hostname.
    INSTANCE_NAME: str = socket.gethostname()
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/warehouse_db"
    # True  -> la API crea las tablas al arrancar (útil corriendo sin Docker).
    # False -> las tablas las crea db/schema.sql (así está en Docker).
    AUTO_CREATE_TABLES: bool = True

    # ---------- Fase 2: caché con Redis (Cache-Aside) ----------
    REDIS_URL: str = "redis://localhost:6379/0"
    CACHE_ENABLED: bool = True          # False = experimento "solo PostgreSQL"
    CACHE_TTL_SECONDS: int = 30         # Tiempo de vida de cada dato en caché
    REDIS_TIMEOUT_SECONDS: float = 0.5  # Si Redis no responde en este tiempo, se ignora
    REDIS_RETRY_AFTER_SECONDS: int = 5  # Tras un fallo, cuánto esperar antes de volver a intentar Redis
    # sync  = la API borra la clave al terminar (Estrategia A, Fase 2)
    # async = la borra el cache-worker al recibir el evento (Estrategia B, Fase 3)
    # both  = las dos (la más segura; es la opción por defecto)
    CACHE_INVALIDATION_MODE: str = "both"

    # ---------- Fase 3: RabbitMQ, outbox y workers ----------
    AMQP_URL: str = "amqp://warehouse:warehouse@localhost:5672/%2F"
    OUTBOX_BATCH_SIZE: int = 100            # eventos que el relay publica por vuelta
    OUTBOX_POLL_SECONDS: float = 0.5        # cada cuánto revisa el relay si hay pendientes
    OUTBOX_RETENTION_DAYS: int = 30         # días que se guardan los eventos ya publicados
    WORKER_MAX_RETRIES: int = 5             # intentos antes de mandar un mensaje a la DLQ
    WORKER_RETRY_DELAY_MS: int = 5000       # espera entre intentos
    REPORT_SIMULATED_DELAY_SECONDS: float = 0  # >0 para "ver" que el reporte es asíncrono
    NOTIFICATION_WEBHOOK_URL: str | None = None  # opcional: URL a la que se envía cada alerta

    # ---------- Fase 7: conexión con la plataforma central ----------
    # RabbitMQ de la central. Vacío = esta bodega trabaja sola (no reenvía nada).
    CENTRAL_AMQP_URL: str | None = None
    # URL con la que la central puede llamar a ESTA bodega (sincronización y consultas en vivo).
    # Vacía = http://lb_warehouse_<WAREHOUSE_ID>:80 (nombre del balanceador en la red de Docker)
    ADVERTISED_API_URL: str | None = None
    HEARTBEAT_SECONDS: int = 15                   # cada cuánto la bodega le dice a la central "sigo viva"


settings = Settings()
