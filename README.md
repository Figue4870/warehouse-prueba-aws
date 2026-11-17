# Warehouse Cloud Platform

Plataforma de bodegas del curso **Computación en la Nube e IoT**, construida por fases.

| Fase | Qué agrega | Documento |
|---|---|---|
| 1-2 | API de bodega + PostgreSQL + caché Redis | `docs/fases/FASE_1_2.md` |
| 3 | Eventos sin pérdida (outbox), RabbitMQ, workers, reportes asíncronos | `docs/fases/FASE_3.md` |
| 4-5 | 3 bodegas independientes, balanceador con 3 APIs, Terraform (AWS) | `docs/fases/FASE_4_5.md` |
| 6 | Transferencias entre bodegas con una Saga orquestada (compensaciones, idempotencia, timeouts) | `docs/fases/FASE_6.md` |
| 7 | Plataforma central (CQRS), sincronización sin pérdida, central en Terraform | `docs/fases/FASE_7.md` |
| — | Reportes con Athena (S3 + Glue + Athena con Terraform) y tablero de la central | `docs/fases/REPORTES_ATHENA.md` |

La Fase 7 (central) se construyó antes que la 6 porque la Saga la orquesta la central.

## Estructura

```text
warehouse-cloud-platform/
├── basic-docker-compose.yml      ← UNA bodega (WH-001) con un solo comando
├── manage.ps1 / manage_warehouse.sh  ← levantar / apagar bodegas (Windows / Linux-EC2)
├── docker/
│   ├── composes/compose-warehouse.yml   ← plantilla ÚNICA de una bodega (15 contenedores)
│   ├── composes/compose-central.yml     ← plataforma central (8 contenedores, Fases 6 y 7)
│   ├── components/wh_00X/.env    ← lo que distingue a cada bodega (puertos, credenciales de desarrollo)
│   ├── components/central/.env   ← puertos y credenciales de desarrollo de la central
│   └── nginx/lb.conf             ← balanceador de carga (Fase 5)
├── db/
│   ├── schema.sql                ← tablas de una bodega (Docker lo ejecuta solo la 1.ª vez)
│   ├── central_schema.sql        ← modelo de lectura (Fase 7) + transferencias (Fase 6) de la central
│   ├── migrations/               ← cambios de esquema para bases ya creadas
│   └── seed.sql                  ← datos de ejemplo (opcional, se carga a mano)
├── src/common/platform_common/   ← librería compartida: eventos y RabbitMQ confiable (Fase 3)
├── src/backend/app/              ← API FastAPI + workers (relay, cache, notification, report, audit, central_forwarder, command_worker)
├── src/central/                  ← plataforma central: API global, proyector, reconciliador, Saga, exportador a S3, tablero
├── src/frontend/app/             ← página web (Nginx)
├── scripts/                      ← dlq.py, new_warehouse.py, demo_fase4_5.py, demo_fase6.py, demo_fase7.py, athena_report.py
├── terraform/                    ← AWS Learner Lab: una EC2 por bodega + la central + S3/Glue/Athena
├── .github/workflows/ci.yml      ← integración continua (pruebas en cada push)
├── tests/
│   ├── unit/                     ← pruebas sin Docker (SQLite + Redis falso)
│   ├── central/                  ← pruebas de la central sin Docker
│   ├── integration/              ← pruebas contra la API real
│   ├── http/                     ← colección .http (VS Code REST Client) y Postman
│   └── load/                     ← benchmark con/sin Redis y locustfile
└── pytest.ini
```

## Levantar la bodega

Requisito: Docker Desktop encendido. Desde esta carpeta:

```powershell
docker network create warehouse_platform     # solo la primera vez (red compartida con la central, Fase 7)
docker compose -f basic-docker-compose.yml up -d --build
```

(Sin la central la bodega funciona igual: sus eventos para la central esperan en la cola local
`wh.central-forward`.)

| Qué | URL |
|---|---|
| Página web | http://localhost:8081 |
| API (balanceador → 3 instancias) + Swagger | http://localhost:8001/docs |
| Salud | http://localhost:8001/health |
| PostgreSQL (DBeaver/pgAdmin) | localhost:5433 · usuario `postgres` · clave `postgres` · BD `warehouse_db` |
| Redis | localhost:6380 |
| Consola de RabbitMQ | http://localhost:15673 · usuario `warehouse` · clave `warehouse` |

Datos de ejemplo (opcional):

```powershell
docker exec db_warehouse_WH-001 psql -U postgres -d warehouse_db -v wh=WH-001 -f /db/seed.sql
```

Apagar: `docker compose -f basic-docker-compose.yml down`
Apagar **y borrar los datos** (necesario si cambia `schema.sql`): `docker compose -f basic-docker-compose.yml down -v`

### Varias bodegas (Fase 4)

Cada bodega es un proyecto de Docker independiente creado desde la misma plantilla:

```powershell
.\manage.ps1 start all          # central + WH-001, WH-002 y WH-003   (Linux/EC2: ./manage_warehouse.sh start all)
.\manage.ps1 start central      # solo la central
.\manage.ps1 stop wh_002        # apagar solo una
.\manage.ps1 status
python scripts/new_warehouse.py WH-004    # agregar otra bodega
python scripts/demo_fase4_5.py            # experimentos de las Fases 4 y 5, verificados
```

| Bodega | Web | API | PostgreSQL | Redis | RabbitMQ |
|---|---|---|---|---|---|
| WH-001 | :8081 | :8001 | :5433 | :6380 | :15673 |
| WH-002 | :8082 | :8002 | :5434 | :6381 | :15674 |
| WH-003 | :8083 | :8003 | :5435 | :6382 | :15675 |

No mezcles `basic-docker-compose.yml` con los scripts para WH-001: usan los mismos nombres de contenedor.

### Plataforma central (Fase 7)

| Qué | URL |
|---|---|
| **Tablero de la central** (bodegas, inventario global, transferencias, reportes) | http://localhost:8090/dashboard |
| API central + Swagger | http://localhost:8090/docs |
| Estado de las bodegas (ONLINE/OFFLINE, eventos faltantes) | http://localhost:8090/warehouses |
| Inventario global de un SKU | http://localhost:8090/global/inventory/P-001 |
| Consola del RabbitMQ central | http://localhost:15680 · usuario `platform` · clave `platform` |
| PostgreSQL central | localhost:5440 · usuario `postgres` · clave `postgres` · BD `central_db` |

Endpoints de la central y los experimentos de "no pérdida" (`python scripts/demo_fase7.py`): ver
`docs/fases/FASE_7.md`. Transferencias (`POST /transfers`) y sus experimentos (`python scripts/demo_fase6.py`):
`docs/fases/FASE_6.md`. Reportes con Athena: `docs/fases/REPORTES_ATHENA.md`. Colección de peticiones:
`tests/http/central-api.http`.

### En AWS (Learner Lab)

Ver `terraform/README.md`: `terraform apply` crea una EC2 por bodega, otra para la central y el bucket
S3 + Glue + Athena de los reportes, y deja todo corriendo.

## Endpoints

| Método | Ruta | Para qué |
|---|---|---|
| GET | `/health` | Estado de la API, PostgreSQL y Redis |
| POST | `/products` | Crear producto (su inventario nace en 0) |
| GET | `/products` | Listar (`?include_inactive=true` incluye dados de baja) |
| GET | `/products/{id}` | Consultar uno |
| PUT | `/products/{id}` | Actualizar datos (el SKU no cambia) |
| DELETE | `/products/{id}` | Dar de baja (borrado lógico, solo sin existencias) |
| GET | `/inventory/{product_id}` | Saldo — **usa Redis**, cabecera `X-Cache: HIT/MISS/BYPASS` |
| POST | `/inventory/movements` | Entrada, salida, ajuste, reserva, liberación… |
| GET | `/inventory/{product_id}/movements` | Historial (kardex) |
| GET | `/alerts` | Alertas automáticas: `OUT_OF_STOCK`, `DISCREPANCY` |
| GET | `/metrics` | Hits/misses del caché y consultas SQL |
| POST | `/metrics/reset` | Poner contadores en cero |
| POST | `/reports` | Pedir un reporte: **202 Accepted** (lo genera el report-worker) |
| GET | `/reports/jobs/{job_id}` | Estado y resultado del reporte |
| GET | `/reports/jobs/{job_id}/download` | Descargar el reporte (CSV o JSON) |
| GET | `/inventory/by-sku/{sku}` | Saldo por SKU, en vivo (Fase 7) |
| GET | `/sync/events?after_sequence=N` | Eventos posteriores a N, para que la central recupere huecos (Fase 7) |
| GET | `/sync/snapshot` | Foto consistente de productos e inventario (Fase 7) |
| GET | `/transfer-operations` | Qué hizo esta bodega en cada paso de las transferencias (Fase 6) |

`TRANSFER_IN` y `TRANSFER_OUT` ya no se aceptan en `POST /inventory/movements` (409): las
transferencias se piden en la central con `POST /transfers` (Fase 6).

## Pruebas

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r src/backend/app/requirements-dev.txt

pytest tests/unit tests/central   # no necesitan Docker
pytest tests/integration          # necesita la bodega corriendo (y la central para sus pruebas)
```

## Experimento Fase 2 (con Redis vs solo PostgreSQL)

```powershell
python tests/load/benchmark_cache.py --label con-redis

$env:CACHE_ENABLED="false"; docker compose -f basic-docker-compose.yml up -d
python tests/load/benchmark_cache.py --label solo-postgres

$env:CACHE_ENABLED="true"; docker compose -f basic-docker-compose.yml up -d   # volver a encenderlo
```

## Variables de entorno de la API

| Variable | Por defecto | Qué hace |
|---|---|---|
| `WAREHOUSE_ID` | `WH-001` | Identificador de la bodega |
| `INSTANCE_NAME` | hostname | Nombre de esta copia de la API (cabecera `X-Instance`, Fase 5) |
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/warehouse_db` | Conexión a PostgreSQL |
| `AUTO_CREATE_TABLES` | `true` (en Docker `false`) | Crear tablas al arrancar si no hay `schema.sql` |
| `REDIS_URL` | `redis://localhost:6379/0` | Conexión a Redis |
| `CACHE_ENABLED` | `true` | `false` = solo PostgreSQL (experimento) |
| `CACHE_TTL_SECONDS` | `30` | Vida de cada dato en caché |
| `REDIS_TIMEOUT_SECONDS` | `0.5` | Tiempo máximo esperando a Redis |
| `REDIS_RETRY_AFTER_SECONDS` | `5` | Pausa antes de reintentar Redis tras un fallo |
| `CACHE_INVALIDATION_MODE` | `both` | `sync` (API), `async` (cache-worker) o `both` |
| `AMQP_URL` | `amqp://warehouse:warehouse@localhost:5672/%2F` | RabbitMQ de la bodega |
| `WORKER_MAX_RETRIES` / `WORKER_RETRY_DELAY_MS` | `5` / `5000` | Reintentos antes de la DLQ |
| `OUTBOX_RETENTION_DAYS` | `30` | Días que se guardan los eventos ya publicados |
| `NOTIFICATION_WEBHOOK_URL` | vacío | Webhook opcional para avisar alertas al administrador |
| `CENTRAL_AMQP_URL` | (en Docker) `amqp://platform:platform@central_rabbitmq:5672/%2F` | RabbitMQ de la central (Fase 7); vacío = bodega sin central |
| `ADVERTISED_API_URL` | `http://lb_warehouse_<WH_ID>:80` | URL con la que la central llama a esta bodega |
| `HEARTBEAT_SECONDS` | `15` | Cada cuánto la bodega avisa a la central que está viva |

Las contraseñas de desarrollo (`postgres/postgres`) son **solo para desarrollo**. En un ambiente real van en un gestor de secretos y nunca en el repositorio.

Decisiones de diseño: `docs/adr/`.
