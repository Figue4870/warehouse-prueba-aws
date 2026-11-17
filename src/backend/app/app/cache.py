"""
FASE 2 — Caché de inventario con Redis usando el patrón CACHE-ASIDE.

Idea general (la API es quien "maneja" el caché, Redis no sabe nada de PostgreSQL):

    GET /inventory/5
        1. Buscar en Redis la clave  warehouse:WH-001:inventory:5
        2. ¿Está?  -> HIT  -> responder con ese valor (no se toca PostgreSQL)
        3. ¿No está? -> MISS -> leer PostgreSQL -> guardar en Redis por 30 s -> responder

    POST /inventory/movements (escritura)
        1. Actualizar PostgreSQL (transacción + COMMIT)
        2. BORRAR la clave en Redis (invalidación síncrona, "Estrategia A")
           -> la próxima lectura será MISS y traerá el dato nuevo.

Reglas de seguridad:
    * PostgreSQL es la fuente de verdad. Redis es solo una copia rápida.
    * Si Redis está caído, la API sigue funcionando leyendo PostgreSQL (BYPASS).
    * Tras un error de Redis, se deja de intentar durante unos segundos
      (REDIS_RETRY_AFTER_SECONDS) para no hacer esperar a cada request.
    * El TTL (30 s) es la red de seguridad: aunque una invalidación fallara,
      el dato viejo desaparece solo en máximo 30 s.
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import redis

from app.config import settings

logger = logging.getLogger("warehouse.cache")

HIT = "HIT"        # el dato vino de Redis
MISS = "MISS"      # no estaba en Redis: se leyó de PostgreSQL y se guardó en Redis
BYPASS = "BYPASS"  # no se usó Redis (caché apagado o Redis caído): se leyó de PostgreSQL


@dataclass
class CacheLookup:
    value: Optional[dict]
    status: str


class InventoryCache:
    def __init__(
        self,
        client: Any,
        warehouse_id: str,
        ttl_seconds: int = 30,
        enabled: bool = True,
        retry_after_seconds: int = 5,
    ) -> None:
        self.client = client
        self.warehouse_id = warehouse_id
        self.ttl_seconds = ttl_seconds
        self.enabled = enabled
        self.retry_after_seconds = retry_after_seconds
        self._unavailable_until = 0.0

    # ------------------------------------------------------------------ claves
    def key(self, product_id: int) -> str:
        # La clave incluye la bodega para que WH-001/producto 5 y WH-002/producto 5
        # nunca choquen, aunque en el futuro compartieran un mismo Redis.
        return f"warehouse:{self.warehouse_id}:inventory:{product_id}"

    def _stats_key(self, name: str) -> str:
        return f"warehouse:{self.warehouse_id}:stats:cache_{name}"

    # ------------------------------------------------------- disponibilidad
    @property
    def usable(self) -> bool:
        return self.enabled and time.monotonic() >= self._unavailable_until

    def _mark_unavailable(self, operation: str, error: Exception) -> None:
        self._unavailable_until = time.monotonic() + self.retry_after_seconds
        logger.warning(
            "Redis no disponible; se usará PostgreSQL directamente",
            extra={
                "event": "redis_unavailable",
                "context": {
                    "operation": operation,
                    "error": str(error),
                    "retry_after_seconds": self.retry_after_seconds,
                },
            },
        )

    # --------------------------------------------------------- operaciones
    def get(self, product_id: int) -> CacheLookup:
        if not self.usable:
            return CacheLookup(None, BYPASS)
        try:
            raw = self.client.get(self.key(product_id))
            if raw is None:
                self.client.incr(self._stats_key("misses"))
                return CacheLookup(None, MISS)
            self.client.incr(self._stats_key("hits"))
            return CacheLookup(json.loads(raw), HIT)
        except redis.RedisError as error:
            self._mark_unavailable("get", error)
            return CacheLookup(None, BYPASS)

    def set(self, product_id: int, value: dict) -> None:
        if not self.usable:
            return
        try:
            # ex = TTL en segundos: Redis borra la clave sola cuando expira
            self.client.set(self.key(product_id), json.dumps(value), ex=self.ttl_seconds)
        except redis.RedisError as error:
            self._mark_unavailable("set", error)

    def invalidate(self, product_id: int) -> bool:
        """
        Borra la copia en caché de un producto. Se llama DESPUÉS del COMMIT.
        Se intenta aunque Redis haya fallado hace poco: es mejor intentar borrar
        un dato viejo que dejarlo vivo. Si falla, el TTL lo limpiará.
        """
        if not self.enabled:
            return False
        try:
            self.client.delete(self.key(product_id))
            return True
        except redis.RedisError as error:
            self._mark_unavailable("invalidate", error)
            return False

    # ----------------------------------------------------- salud y métricas
    def status(self) -> str:
        if not self.enabled:
            return "disabled"
        try:
            self.client.ping()
            self._unavailable_until = 0.0  # Redis volvió: usarlo de nuevo de inmediato
            return "ok"
        except redis.RedisError as error:
            self._mark_unavailable("ping", error)
            return "down"

    def stats(self) -> dict:
        data = {
            "enabled": self.enabled,
            "status": self.status(),
            "ttl_seconds": self.ttl_seconds,
            "key_example": self.key(1),
            "hits": None,
            "misses": None,
            "hit_ratio": None,
            "cached_keys": None,
        }
        if data["status"] != "ok":
            return data
        try:
            hits = int(self.client.get(self._stats_key("hits")) or 0)
            misses = int(self.client.get(self._stats_key("misses")) or 0)
            pattern = f"warehouse:{self.warehouse_id}:inventory:*"
            data.update(
                hits=hits,
                misses=misses,
                hit_ratio=round(hits / (hits + misses), 4) if (hits + misses) else None,
                cached_keys=sum(1 for _ in self.client.scan_iter(match=pattern, count=500)),
            )
        except redis.RedisError as error:
            self._mark_unavailable("stats", error)
        return data

    def reset_stats(self) -> None:
        if not self.enabled:
            return
        try:
            self.client.delete(self._stats_key("hits"), self._stats_key("misses"))
        except redis.RedisError as error:
            self._mark_unavailable("reset_stats", error)


def _build_redis_client() -> redis.Redis:
    # La conexión es "perezosa": no se conecta hasta la primera operación.
    return redis.Redis.from_url(
        settings.REDIS_URL,
        socket_connect_timeout=settings.REDIS_TIMEOUT_SECONDS,
        socket_timeout=settings.REDIS_TIMEOUT_SECONDS,
        decode_responses=True,
    )


inventory_cache = InventoryCache(
    client=_build_redis_client(),
    warehouse_id=settings.WAREHOUSE_ID,
    ttl_seconds=settings.CACHE_TTL_SECONDS,
    enabled=settings.CACHE_ENABLED,
    retry_after_seconds=settings.REDIS_RETRY_AFTER_SECONDS,
)


def get_inventory_cache() -> InventoryCache:
    """Dependencia de FastAPI (en las pruebas se reemplaza por un Redis falso)."""
    return inventory_cache
