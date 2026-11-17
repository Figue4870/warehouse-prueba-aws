# Fase 3 — RabbitMQ, workers y eventos sin pérdida

**Commit 1 · Persona 1**
**Objetivo:** sacar de la petición HTTP lo que no tiene que ocurrir ahí (reportes, avisos, invalidar caché, auditoría) **sin perder nunca un evento**, aunque RabbitMQ, un worker o la red fallen.

---

## 1. El problema

Hasta la Fase 2 todo pasaba **dentro** de la petición HTTP. Eso tiene dos problemas:

1. **Lo pesado bloquea la API.** Un reporte de un mes de movimientos puede tardar segundos, y mientras tanto el usuario espera.
2. **Los efectos secundarios se pueden perder.** Si después de guardar un movimiento la API intentara "avisar" a otro sistema y ese sistema estuviera caído, ¿qué pasa con el aviso?

La solución del README es un **broker de mensajes (RabbitMQ)** con **workers** que hacen ese trabajo aparte. Pero introducir RabbitMQ crea un problema nuevo y muy importante:

> Si la API guarda el movimiento en PostgreSQL y **luego** publica el evento en RabbitMQ, ¿qué pasa si RabbitMQ está caído justo en ese momento? El inventario cambió, pero el evento se perdió.

A esto se le llama el problema de la **doble escritura** (*dual write*): escribir en dos sistemas distintos no puede ser atómico. Esta fase lo resuelve con el patrón **Transactional Outbox**.

---

## 2. La arquitectura de la Fase 3

```text
                       Petición HTTP
                            |
                            v
        +-------------- API de bodega --------------+
        |   UNA transacción de PostgreSQL:           |
        |     UPDATE inventory                       |
        |     INSERT inventory_movements             |
        |     INSERT outbox_events   <-- el evento   |
        |   COMMIT                                   |
        +--------------------------------------------+
                            |
                  (la API ya respondió 201)
                            |
                     outbox_events (PostgreSQL)
                            |
                            v
                     OUTBOX RELAY  ---- publisher confirms ---->  RabbitMQ
                                                                     |
                                                       exchange "warehouse.events"
                                                                     |
          +-------------------------+--------------------+----------+----------------+
          v                         v                    v                           v
 wh.cache-invalidation       wh.notifications        wh.reports                  wh.audit
     cache-worker          notification-worker      report-worker               audit-worker
   (borra clave Redis)    (avisa al administrador)  (genera reportes)      (rastro de cada evento)
```

**La API no habla con RabbitMQ.** Solo escribe en su base de datos. Por eso, si RabbitMQ se cae, la bodega **sigue funcionando** con normalidad.

---

## 3. Transactional Outbox, paso a paso

### 3.1 La API escribe el evento en la misma transacción

En `inventory_service.py`, el movimiento, la alerta y el evento se guardan **juntos**:

```python
self.repo.update_stock(...)                       # UPDATE inventory
movement = self.repo.register_movement(...)        # INSERT inventory_movements
self.outbox.add("INVENTORY_MOVEMENT_RECORDED", ...)# INSERT outbox_events   <- NUEVO
self.db.commit()                                   # TODO o NADA
```

- Si el `COMMIT` funciona, el cambio **y** su evento existen.
- Si algo falla y hay `ROLLBACK`, **ninguno** de los dos existe. Nunca queda un evento de algo que no pasó, ni un cambio sin su evento.

Lo mismo pasa al crear, editar o dar de baja un producto, al generar alertas y al pedir un reporte.

### 3.2 El relay lleva los eventos a RabbitMQ

`app/workers/relay.py` es un proceso aparte que repite este ciclo cada medio segundo:

1. Toma los eventos pendientes (`published_at IS NULL`).
2. Les asigna un número de **secuencia** 1, 2, 3… sin huecos (la central lo usará en la Fase 7 para saber si le falta alguno).
3. Los publica en RabbitMQ **con confirmación** (*publisher confirms*): espera a que RabbitMQ responda "lo guardé en disco".
4. **Solo entonces** marca `published_at`.

¿Qué pasa en cada falla?

| Falla | Qué ocurre | ¿Se pierde algo? |
|---|---|---|
| RabbitMQ caído | El relay no puede publicar; los eventos siguen en `outbox_events` y reintenta con espera creciente (1, 2, 4… hasta 30 s) | **No** |
| El relay se cae después de publicar y antes de marcar | Al volver publica ese evento otra vez | **No** (puede llegar duplicado; los consumidores lo toleran, ver 4.3) |
| Dos relays corriendo a la vez | Un *advisory lock* de PostgreSQL deja publicar solo a uno; el otro queda de reserva | **No** |
| PostgreSQL caído | La API responde 503 y no acepta cambios, así que no hay nada que perder | **No** |

> 💡 **"Al menos una vez" (at-least-once).** Es imposible garantizar "exactamente una vez" en la red. Lo que se garantiza es *al menos una vez*: un mensaje puede llegar repetido, pero nunca se pierde. Sumado a consumidores **idempotentes** (que ignoran repetidos), el efecto final es como si hubiera llegado una sola vez.

### 3.3 Limpieza

Los eventos ya publicados se borran después de `OUTBOX_RETENTION_DAYS` (30 días). **Los pendientes nunca se borran.**

---

## 4. RabbitMQ confiable

Todo está en `src/common/platform_common/messaging.py`, una librería **compartida** que en la Fase 7 también usará la plataforma central.

### 4.1 Mensajes y colas durables

- Los mensajes se publican como **persistentes** (`delivery_mode=2`) y las colas son **durables**: sobreviven a un reinicio de RabbitMQ.
- RabbitMQ guarda sus datos en un volumen de Docker (`rabbitdata`) y tiene un `hostname` fijo, para que los mensajes también sobrevivan a un `docker compose down`/`up`.
- Los workers usan **ack manual**: avisan "terminé" solo al final. Si un worker se cae a la mitad, RabbitMQ entrega el mensaje de nuevo.

### 4.2 Reintentos y Dead Letter Queue (DLQ)

Cada cola tiene dos compañeras:

```text
wh.reports  --error-->  wh.reports.retry (espera 5 s)  --vuelve-->  wh.reports
     |
     +-- después de 5 fallos (o si el error es permanente) -->  wh.reports.dlq
```

- **Error transitorio** (la base de datos ocupada, Redis caído, un webhook que no responde): se reintenta.
- **Error permanente** (`PermanentError`: un mensaje que no es JSON válido, un trabajo que no existe): va **directo** a la DLQ, porque reintentar no lo arreglaría.
- **Nada se bota.** La DLQ guarda el mensaje con la causa del error (cabecera `x-error`). Se revisa y se reprocesa con:

```powershell
pip install pika==1.3.2
python scripts/dlq.py list   --queue wh.reports
python scripts/dlq.py replay --queue wh.reports     # después de arreglar la causa
```

### 4.3 Idempotencia (mensajes repetidos)

Como un mensaje puede llegar dos veces, cada worker está hecho para que procesarlo dos veces dé el mismo resultado que una:

| Worker | Cómo ignora un repetido |
|---|---|
| report-worker | Si el trabajo ya está `COMPLETED` o `FAILED`, no hace nada |
| notification-worker | Si la alerta ya tiene `notified_at`, no vuelve a avisar |
| cache-worker | Borrar una clave que ya no existe no hace daño |
| audit-worker | Solo escribe un log (el `event_id` permite detectar repetidos al analizarlo) |

---

## 5. Los cuatro workers

| Worker | Cola | Escucha | Qué hace |
|---|---|---|---|
| **cache-worker** | `wh.cache-invalidation` | `inventory.movement.recorded`, `product.deactivated` | Borra la clave del producto en Redis (Estrategia B del README) |
| **notification-worker** | `wh.notifications` | `stock.depleted`, `stock.discrepancy` | Avisa al administrador (log `admin_notified` y, si se configura, un POST a `NOTIFICATION_WEBHOOK_URL`) y marca `notified_at` |
| **report-worker** | `wh.reports` | `report.requested` | Genera el reporte y guarda el resultado en la tabla `report_jobs` |
| **audit-worker** | `wh.audit` | `product.#`, `inventory.#`, `stock.#`, `report.#` | Escribe una línea de auditoría por evento |

Todos usan **la misma imagen** que la API, con otro comando (`python -m app.workers.<nombre>`). Cada uno es un contenedor aparte, así que se puede apagar, reiniciar o escalar por separado.

### Formato del evento (contrato, README §27)

```json
{
  "event_id": "7b1c0a8e-…",
  "event_type": "INVENTORY_MOVEMENT_RECORDED",
  "event_version": 1,
  "occurred_at": "2026-11-17T03:45:24Z",
  "correlation_id": "COR-20261117-54A54C22",
  "source": "warehouse-WH-001",
  "warehouse_id": "WH-001",
  "sequence": 12,
  "payload": { "sku": "EPP-CASCO-001", "type": "SHIPMENT", "quantity": 4,
               "quantity_after": 46, "inventory_version": 7, "...": "..." }
}
```

| Evento | Routing key | Cuándo |
|---|---|---|
| `PRODUCT_CREATED` / `PRODUCT_UPDATED` / `PRODUCT_DEACTIVATED` | `product.*` | Crear, editar o dar de baja |
| `INVENTORY_MOVEMENT_RECORDED` | `inventory.movement.recorded` | Cada movimiento aceptado |
| `STOCK_DEPLETED` | `stock.depleted` | El disponible llegó a 0 |
| `STOCK_DISCREPANCY` | `stock.discrepancy` | Un ajuste por desfase |
| `REPORT_REQUESTED` | `report.requested` | `POST /reports` |

Los productos y el inventario ahora tienen una columna **`version`** que sube con cada cambio. Viaja en el evento (`inventory_version`, `product_version`) para que en la Fase 7 la central nunca reemplace un dato nuevo con uno viejo.

---

## 6. API asíncrona de reportes (README §10.3)

```http
POST /reports
{ "type": "STOCK_VALUATION" }

HTTP/1.1 202 Accepted
Location: /reports/jobs/JOB-20261117-1A2B3C4D
{ "job_id": "JOB-20261117-1A2B3C4D", "status": "PROCESSING", "status_url": "/reports/jobs/JOB-…" }
```

```http
GET /reports/jobs/JOB-20261117-1A2B3C4D            -> PENDING | PROCESSING | COMPLETED | FAILED (+ resultado)
GET /reports/jobs/JOB-20261117-1A2B3C4D/download   -> CSV (o ?format=json)
GET /reports/jobs                                   -> últimos trabajos
```

| Tipo | Contenido |
|---|---|
| `STOCK_VALUATION` | Inventario valorizado: disponible, reservado, valor a costo del proveedor y a precio de tienda (campos del PDF) |
| `MOVEMENTS_SUMMARY` | Entradas y salidas por producto en un rango de fechas (`date_from`, `date_to`) y valor de las ventas |
| `LOW_STOCK` | Productos con disponible menor o igual a `threshold` |

- **¿Por qué 202 y no 200?** 202 significa "recibí tu pedido, lo estoy procesando". El cliente no se queda esperando: consulta el estado después.
- **¿Por qué el resultado va a la base de datos y no a un archivo?** Porque en la Fase 5 habrá varias copias de la API: cualquiera debe poder responder `GET /reports/jobs/{id}` (API *stateless*).
- **¿Se pierde el pedido si RabbitMQ está caído?** No. El trabajo y su evento `REPORT_REQUESTED` se guardan en la misma transacción. El reporte se genera cuando RabbitMQ vuelva.

La página web tiene un panel **Reportes** que pide el reporte, muestra cómo pasa de PENDING a COMPLETED y ofrece el CSV.

---

## 7. Invalidación del caché: Estrategia A vs B

El README pide comparar las dos estrategias. Ahora se pueden probar ambas con `CACHE_INVALIDATION_MODE` en el `.env` de la bodega:

| Modo | Quién borra la clave de Redis |
|---|---|
| `sync` | La API, justo después del COMMIT (Estrategia A, Fase 2) |
| `async` | Solo el cache-worker, al recibir el evento (Estrategia B) |
| `both` (por defecto) | Los dos: la API de inmediato y el worker como respaldo |

| | A: síncrona | B: asíncrona |
|---|---|---|
| Ventana de dato viejo | Casi cero | Lo que tarde la cadena outbox → relay → RabbitMQ → worker (en las pruebas, menos de 1 s) |
| Si Redis falla al borrar | Queda viejo hasta el TTL | Se reintenta hasta lograrlo |
| Si RabbitMQ está caído | No afecta | El dato viejo dura hasta que vuelva o expire el TTL (30 s) |
| Costo para la API | +1 llamada a Redis | Ninguno |

**Decisión:** `both`. Combina lo inmediato de A con el reintento de B, y el TTL sigue siendo la red de seguridad.

---

## 8. correlation_id de punta a punta

Cada petición trae (o recibe) una cabecera `X-Correlation-ID` (`COR-20261117-1A2B3C4D`). Ese valor:

- se devuelve en la respuesta,
- se guarda en el evento del outbox,
- viaja en la propiedad `correlation_id` del mensaje de RabbitMQ,
- y aparece en **todos** los logs JSON de la API y de los workers.

Así, buscando un solo valor en los logs, se ve todo lo que provocó una petición: `docker logs report_worker_WH-001 | findstr COR-20261117-1A2B3C4D`.

---

## 9. Cómo correrlo

```powershell
docker compose -f basic-docker-compose.yml up -d --build
docker compose -f basic-docker-compose.yml ps     # 10 contenedores
```

| Qué | Dónde |
|---|---|
| Página web | http://localhost:8081 |
| API (Swagger) | http://localhost:8001/docs |
| Consola de RabbitMQ (colas, mensajes, consumidores) | http://localhost:15673 · usuario `warehouse` / clave `warehouse` |
| Eventos pendientes | `GET http://localhost:8001/metrics` → bloque `outbox` |

Si ya tenían una base de la Fase 2 con datos, migren en vez de borrar:

```powershell
docker exec db_warehouse_WH-001 psql -U postgres -d warehouse_db -f /db/migrations/001_fase3_outbox_y_reportes.sql
```

---

## 10. Experimentos (todos se hicieron con RabbitMQ y PostgreSQL reales)

| # | Experimento | Cómo hacerlo | Resultado obtenido |
|---|---|---|---|
| 1 | **RabbitMQ caído** | `docker stop rabbitmq_warehouse_WH-001`, hacer movimientos y pedir un reporte | Los 5 movimientos dieron 201 y quedaron 6 eventos esperando en el outbox. `/health` siguió en `ok`. Al hacer `docker start` el outbox se vació solo, el reporte se completó y las secuencias quedaron sin huecos |
| 2 | **Worker detenido** | `docker stop report_worker_WH-001` y pedir un reporte | El mensaje esperó en `wh.reports` (visible en la consola de RabbitMQ). Con `docker start`, el reporte se completó |
| 3 | **Mensaje duplicado** | Republicar un `REPORT_REQUESTED` ya procesado | Se ignoró (`attempts` siguió en 1) |
| 4 | **Mensaje corrupto** | Publicar un texto que no es JSON | Fue a `wh.reports.dlq` con su causa; `scripts/dlq.py list` lo muestra |
| 5 | **Ningún evento perdido** | Comparar los `event_id` del outbox con los que registró el audit-worker | Los 11 eventos llegaron |
| 6 | **Error en un worker** | Un error real de configuración en el notification-worker | Lo reintentó 3 veces y lo dejó en la DLQ: **no se perdió**. Se corrigió el error (importar todos los modelos, `app/all_models.py`) y el mensaje se puede reprocesar con `dlq.py replay` |

---

## 11. Pruebas

| Archivo | Qué comprueba |
|---|---|
| `tests/unit/test_outbox_events.py` (9) | Cada cambio deja su evento; un movimiento rechazado o revertido no deja eventos; versiones; correlation_id |
| `tests/unit/test_relay.py` (5) | Secuencias sin huecos; con RabbitMQ caído no se pierde ni se desordena nada; lotes; la limpieza nunca borra pendientes |
| `tests/unit/test_workers.py` (14) | Ack, reintento, DLQ y errores permanentes; idempotencia de reportes y notificaciones; cache-worker |
| `tests/unit/test_reports_api.py` (3) | 202 + Location, validaciones, CSV solo cuando está listo |
| `tests/integration/test_events_integration.py` (4) | Con la bodega en Docker: reporte de punta a punta, outbox vaciado, alerta notificada, correlation_id |

```powershell
pytest tests/unit                 # 69 pruebas, sin Docker
pytest tests/integration          # con basic-docker-compose levantado
```

---

## 12. Respuestas para la sustentación

**¿Por qué usar un broker?** Para desacoplar: la API publica un hecho ("hubo un movimiento") y no necesita saber cuántos sistemas reaccionan ni si están encendidos. Agregar un consumidor nuevo (por ejemplo, la central en la Fase 7) no obliga a cambiar la API.

**¿Diferencia entre síncrono y asíncrono?** En lo síncrono el cliente espera a que todo termine (`POST /inventory/movements` → 201). En lo asíncrono la API acepta el pedido y responde enseguida (`POST /reports` → 202); el trabajo ocurre después y el cliente consulta el resultado.

**¿Qué pasa si un worker falla?** El mensaje no tuvo ack, así que RabbitMQ lo vuelve a entregar. Si sigue fallando se reintenta con espera, y después de 5 intentos queda en la DLQ, sin perderse.

**¿Qué es una DLQ?** Una cola donde quedan los mensajes que no se pudieron procesar, con la causa del error, para revisarlos y reprocesarlos.

**¿Cómo evitar mensajes duplicados?** No se pueden evitar del todo (*at-least-once*). Se hace que los consumidores sean **idempotentes**: procesar un mensaje dos veces da el mismo resultado que una.

**¿Por qué no publicar directo a RabbitMQ desde la API?** Porque si RabbitMQ estuviera caído después del COMMIT, el evento se perdería (doble escritura). Con el outbox, el evento queda guardado en la misma transacción que el cambio.

---

## 13. Archivos de este commit

| Archivo | Qué es |
|---|---|
| `src/common/platform_common/` | **Nuevo.** Librería compartida: contrato de eventos, correlation_id, RabbitMQ confiable |
| `src/backend/app/app/events/` | **Nuevo.** Outbox (modelo, escritor, payloads), tipos de evento, topología |
| `src/backend/app/app/workers/` | **Nuevo.** Relay y los 4 workers |
| `src/backend/app/app/modules/reports/` | **Nuevo.** API de reportes asíncronos y cálculo de cada reporte |
| `src/backend/app/app/all_models.py` | **Nuevo.** Registra todos los modelos para los workers |
| `src/backend/app/app/modules/inventory/*`, `products/*` | Escriben sus eventos en el outbox; columna `version` |
| `src/backend/app/app/modules/alerts/*` | `notified_at` y `notification_channel` |
| `src/backend/app/main.py` | Middleware de correlation_id y router de reportes |
| `src/backend/app/app/modules/health/health_controller.py` | `outbox_pending` en `/health`; bloque `outbox` en `/metrics` |
| `src/backend/app/Dockerfile`, `src/.dockerignore` | El contexto de build ahora es `src/` (para incluir la librería compartida) |
| `db/schema.sql`, `db/migrations/001_…sql` | Tablas `outbox_events` y `report_jobs`; columnas nuevas |
| `docker/components/wh_00X/*`, `docker/composes/compose-base-warehouse.yml` | RabbitMQ, relay y 4 workers por bodega |
| `scripts/dlq.py` | **Nuevo.** Revisar y reprocesar la DLQ |
| `src/frontend/app/*` | Panel de reportes; alertas muestran si fueron notificadas |
| `tests/unit/*`, `tests/integration/*` | Pruebas nuevas (ver sección 11) |
| `docs/adr/ADR-004-transactional-outbox.md` | Decisión de arquitectura |
