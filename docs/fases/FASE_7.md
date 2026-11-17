# Fase 7 — Plataforma central, CQRS y sincronización sin pérdida

**Commit 3 · Persona 3**
**Objetivo (guía 07 + PDF *Bodegas 1*):** construir una visión consolidada de todas las bodegas
(`GET /global/inventory/P-001`) **sin consultar todas las bases en cada request**, y garantizar que
**ninguna información se pierda**, aunque se caiga la central, una bodega o la red entre ellas.

---

## 1. El problema

```text
WH-001 = 100     WH-002 = 50     WH-003 = 80     (WH-004 = 20)
GET /global/inventory/P-001  ->  ¿cuánto hay en toda la empresa?
```

Cada bodega es autónoma (Fase 4): su propia base, su propio RabbitMQ. La central necesita
responder rápido y siempre, pero:

- si pregunta a las bodegas en cada request, depende de que **todas** estén arriba;
- si se alimenta de eventos, ¿qué pasa si la central está caída cuando ocurre una venta?, ¿y si un
  mensaje se pierde?, ¿y si una bodega queda aislada horas?

---

## 2. Arquitectura

```text
 BODEGA WH-00X (cada una igual, 14 contenedores)                  PLATAFORMA CENTRAL (5 contenedores)
 ------------------------------------------------                 ----------------------------------------------
  API --COMMIT--> PostgreSQL (inventario + OUTBOX)
                        |
                   outbox_relay  (secuencia 1,2,3... + heartbeat cada 15 s)
                        |
                 RabbitMQ local "warehouse.events"
                        |
          +-------------+--------------+------ cola wh.central-forward (durable, "#")
          |  workers de la Fase 3      |               |
                                              central_forwarder  ---AMQP--->  RabbitMQ central "central.events"
                                       (ack local SOLO cuando                       |  routing WH-001.inventory.movement.recorded
                                        la central confirmó)                         v
                                                                           cola central.projection (+retry +DLQ)
                                                                                    |
                                                                            central_projector  (idempotente)
                                                                                    |
          lb_warehouse_WH-00X  <---- GET /sync/events, /sync/snapshot ----  central_reconciler   PostgreSQL central
          (balanceador, Fase 5)      GET /inventory/by-sku (consulta en vivo)       |            (MODELO DE LECTURA)
                                                                                    v                  |
                                                                               central_api  <----------+
                                                                    /global/inventory, /warehouses, /reports, /sync
```

Las dos plataformas se comunican por la red Docker **`warehouse_platform`** (en AWS: la red privada de
la VPC, ver §8).

### CQRS en este proyecto

| Lado | Dónde | Qué hace |
|---|---|---|
| **Comandos** (escritura) | API de cada bodega → su PostgreSQL | Movimientos, productos. Es la **fuente de verdad** |
| **Consultas** (lectura) | Eventos → `central_projector` → PostgreSQL central → `central_api` | Inventario global, catálogo, alertas y reportes. Es una **copia derivada** que se puede reconstruir |

La central **nunca escribe** en una bodega (eso es la Fase 6, con comandos y Saga).

---

## 3. Las cuatro garantías contra la pérdida

### 3.1 Store-and-forward: si la central está caída, la bodega guarda y reenvía después

La bodega no publica directo a la central. El relay publica en su RabbitMQ **local** (como en la
Fase 3) y una cola durable `wh.central-forward` recibe una copia de todo (`#`). El
`central_forwarder` (`app/workers/central_forwarder.py`) la consume así:

1. toma un mensaje de la cola local (sin ack todavía);
2. lo publica en el RabbitMQ central con **publisher confirms** y `mandatory=True`;
3. **solo cuando la central confirmó**, hace ack en la cola local.

Si la central no responde, el forwarder **no hace ack**: cierra la conexión local, el mensaje
vuelve a la cola y reintenta con espera creciente (1, 2, 4… hasta 30 s). Los mensajes esperan en el
disco del RabbitMQ local todo lo que haga falta. La bodega **sigue vendiendo** sin enterarse: su API
solo depende de su propia base.

> Esta cola no tiene reintentos ni DLQ a propósito: un mensaje para la central nunca se "da por
> perdido", solo espera.

### 3.2 Idempotencia: un evento repetido se cuenta una sola vez

Con *at-least-once* habrá duplicados (reintentos, o el mismo evento por RabbitMQ y por `/sync`). La
central guarda cada `event_id` en `processed_events`; si ya está, lo ignora
(`central/projection.py → apply_event`).

### 3.3 Versiones: un dato viejo nunca pisa uno nuevo

Cada evento de movimiento trae el **saldo absoluto** (`quantity_after`) y la `inventory_version` (que
sube con cada movimiento en la bodega). La central solo aplica un saldo si su versión es **mayor** a
la guardada. Por eso, aunque los eventos lleguen desordenados o falten algunos, en cuanto llega el
siguiente evento el saldo queda correcto.

### 3.4 Secuencias y reconciliación: la central detecta lo que le falta y lo pide

Cada evento tiene una secuencia por bodega sin huecos (Fase 3). La central lleva por bodega:

| Campo | Qué es |
|---|---|
| `reported_sequence` | La última secuencia que la bodega dice haber publicado (viene en el heartbeat) |
| `max_received_sequence` | La mayor secuencia que llegó |
| `watermark_sequence` | Hasta dónde la central tiene **todo** sin huecos |

Si llegan 1, 2, 4 el watermark se queda en 2. El `central_reconciler` (cada 15 s) revisa las
bodegas: si el hueco no se llenó solo en `GAP_GRACE_SECONDS` (20 s; a veces solo es un mensaje que
viene en camino), hace **catch-up**: `GET /sync/events?after_sequence=2` a la bodega y aplica lo que
falte con la **misma** función idempotente. Si la bodega ya borró esos eventos (limpieza de 30 días),
pide un **snapshot** (`GET /sync/snapshot`, foto consistente en `REPEATABLE READ`).

La primera vez que la central conoce una bodega también pide un snapshot: así llegan datos que
existían antes de que hubiera eventos (por ejemplo, `seed.sql`).

### 3.5 Heartbeats: saber si un dato puede estar viejo

El relay de cada bodega manda cada 15 s un `WAREHOUSE_HEARTBEAT` (última secuencia, eventos
pendientes en el outbox y la URL de su API). Expira a los 60 s (no tiene sentido entregar un
heartbeat viejo). Si la central no recibe uno en `ONLINE_AFTER_SECONDS` (45 s) marca la bodega
`OFFLINE` y **lo dice** en la respuesta:

```json
{
  "sku": "P-001",
  "total_quantity": 200,
  "consistency": "eventual",
  "warnings": ["WH-002: Sin contacto hace 52 s: sus datos pueden estar desactualizados"],
  "warehouses": [
    {"warehouse_id": "WH-001", "quantity": 90, "warehouse_status": "ONLINE",  "stale": false, "known_by_central_at": "..."},
    {"warehouse_id": "WH-002", "quantity": 40, "warehouse_status": "OFFLINE", "stale": true,  "known_by_central_at": "..."},
    {"warehouse_id": "WH-003", "quantity": 70, "warehouse_status": "ONLINE",  "stale": false, "known_by_central_at": "..."}
  ]
}
```

---

## 4. ¿Qué pasa si…? (resumen)

| Falla | Qué hace el sistema | ¿Se pierde algo? |
|---|---|---|
| Se cae el RabbitMQ de la bodega | Los eventos esperan en el **outbox** (Fase 3) | No |
| Se cae la **central** completa | Los eventos esperan en `wh.central-forward` de cada bodega; las bodegas siguen operando | No |
| La red bodega ↔ central se corta | Igual que el anterior; la central marca la bodega `OFFLINE` y avisa | No |
| Se cae el `central_projector` | Los mensajes esperan en `central.projection` | No |
| Se **pierden mensajes** (alguien vacía una cola, un bug) | El saldo se corrige con el siguiente evento (versión); el **historial** lo recupera el reconciliador por `/sync/events` | No |
| Se pierde la **base central** | Se reconstruye sola: heartbeat → snapshot → catch-up desde la secuencia 0 | No (los eventos de los últimos 30 días; lo anterior queda en el snapshot) |
| Llega un evento **duplicado** | `processed_events` lo ignora | — |
| Un evento **corrupto** | Va a `central.projection.dlq` y se reprocesa con `scripts/dlq.py` | No |
| Se cae una **bodega** | Sus datos en la central quedan como último valor conocido, marcados `OFFLINE`. Las demás siguen | No |

Reprocesar la DLQ de la central (después de corregir la causa):

```powershell
python scripts/dlq.py list   --queue central.projection --amqp amqp://platform:platform@localhost:5680/%2F
python scripts/dlq.py replay --queue central.projection --exchange central.events --amqp amqp://platform:platform@localhost:5680/%2F
```

---

## 5. Alternativa A vs B (ejercicio de la guía)

La central implementa **las dos** para poder medirlas:

- **A — consulta en vivo:** `GET /global/inventory/{sku}/live` pregunta en paralelo a
  `GET /inventory/by-sku/{sku}` de cada bodega (timeout 2 s).
- **B — modelo de lectura:** `GET /global/inventory/{sku}` lee la base central (alimentada por eventos).

### Medición (mediana de 20 consultas, 3 bodegas con sus balanceadores)

| Escenario | B: modelo de lectura | A: consulta en vivo |
|---|---|---|
| Las 3 bodegas arriba | **5.9 ms** · total 178 | 41.4 ms · total 178 |
| WH-002 sin responder | **6.3 ms** · total 178 (con advertencia `OFFLINE` en cuanto pasan 45 s) | 35.3 ms · total **141** · `complete: false` |

### Comparación

| Criterio | A: en vivo | B: modelo de lectura (CQRS) |
|---|---|---|
| **Latencia** | La de la bodega más lenta + la red (7× más lenta aquí; en AWS, entre regiones, mucho peor) | Una consulta local, constante |
| **Disponibilidad** | Si una bodega no responde, el total queda **incompleto** (o hay que fallar) | Responde siempre, aunque bodegas o la red estén caídas |
| **Consistencia** | Fuerte: el dato exacto de este instante | **Eventual**: puede ir unos segundos atrás; la respuesta dice cuándo supo cada dato y si puede estar viejo |
| **Complejidad** | Baja: un fan-out HTTP | Alta: eventos, proyector, versiones, watermark, reconciliador, heartbeats |
| **Dependencia de servicios** | La central depende de TODAS las bodegas en cada request | La central solo depende de su base; las bodegas no dependen de la central |
| **Carga sobre las bodegas** | Cada consulta global = N consultas a las bodegas | Cero (solo eventos que ya generan) |
| **Reportes históricos** | Imposible sin juntar N bases | Un `SELECT` sobre `movement_history` |

**Decisión:** B para todo lo que consulta la central (inventario global, reportes) y A solo como
herramienta de diagnóstico (comparar con la verdad). Ver `docs/adr/ADR-007-central-cqrs-y-sincronizacion.md`.

---

## 6. Experimentos (todos se hicieron con PostgreSQL 16, RabbitMQ 3.13 y Nginx reales)

Se levantaron las 3 bodegas (cada una con sus 3 APIs, balanceador y workers) y la central, cada pieza
como proceso real. Producto de prueba P-001 con WH-001=100, WH-002=50, WH-003=80.

| # | Experimento | Cómo | Resultado |
|---|---|---|---|
| 1 | **Inventario global** | 3 entradas, `GET /global/inventory/P-001` | **230** por B y por A; las 3 bodegas con watermark = secuencia reportada |
| 2 | **Central caída** | Apagar toda la central; 15 salidas de 2 unidades (5 por bodega) | Las 15 dieron **201**. **25 mensajes** quedaron esperando en las colas `wh.central-forward`. Al volver la central: total **200** (230 − 30) y watermarks al día |
| 3 | **Bodega aislada** | Cortar WH-002 de la central y hacer 3 ventas | La central siguió respondiendo, marcó WH-002 `OFFLINE` con advertencia y mostró el último dato conocido; la consulta en vivo mostraba el real. Al reconectar: **197** sin advertencias |
| 4 | **Mensajes perdidos** | Apagar el proyector, 4 salidas en WH-003, **vaciar** `central.projection` (purge) | El saldo se corrigió solo con el siguiente evento: **178** = la suma real. El reconciliador detectó el hueco y recuperó **4 eventos** por `/sync/events`: el historial central quedó **idéntico** al de las 3 bodegas |
| 5 | **Base central perdida** | Borrar toda la base central | Se reconstruyó sola: total **178** e historial completo (**26 movimientos**) |
| 6 | **Duplicados** | Publicar 3 veces el mismo evento en el RabbitMQ central | Total e historial sin cambios |
| 7 | **A vs B** | Medir con todo arriba y con la API de WH-002 caída | Tabla del §5 |

**Repetirlo ustedes con Docker:** con la plataforma levantada (`.\manage.ps1 start all`):

```powershell
python scripts/demo_fase7.py
```

Hace los 7 experimentos con `docker stop/start`, `rabbitmqctl purge_queue`, etc., y verifica cada
resultado (tarda 6-8 minutos porque espera heartbeats y reintentos reales). Crea su propio producto
`DEMO-xxxxxx`, así que no choca con sus datos.

---

## 7. Cómo correrlo

```powershell
.\manage.ps1 start all          # central + WH-001, WH-002, WH-003  (Linux/EC2: ./manage_warehouse.sh start all)
.\manage.ps1 status
```

Los scripts crean la red `warehouse_platform` si no existe. Con `basic-docker-compose.yml` hay que
crearla una vez a mano: `docker network create warehouse_platform`.

| Qué | URL |
|---|---|
| **Tablero de la central** (bodegas ONLINE/OFFLINE, inventario global, reportes; se actualiza cada 5 s) | http://localhost:8090/dashboard |
| API central + Swagger | http://localhost:8090/docs |
| Estado de las bodegas | http://localhost:8090/warehouses |
| Inventario global | http://localhost:8090/global/inventory/P-001 |
| Consola del RabbitMQ central | http://localhost:15680 · `platform` / `platform` |
| PostgreSQL central | localhost:5440 · `postgres` / `postgres` · BD `central_db` |
| Colección de peticiones | `tests/http/central-api.http` |

### Endpoints de la central

| Método | Ruta | Para qué |
|---|---|---|
| GET | `/health` | Base de datos y bodegas ONLINE |
| GET | `/warehouses`, `/warehouses/{id}` | Estado: ONLINE/OFFLINE, eventos faltantes, última sincronización |
| GET | `/global/inventory/{sku}` | Inventario global (B, modelo de lectura) con advertencias |
| GET | `/global/inventory/{sku}/live` | Inventario global preguntando en vivo (A) |
| GET | `/global/inventory` | Todos los SKU por bodega |
| GET | `/global/products`, `/global/alerts`, `/global/summary` | Catálogo, alertas y totales de la empresa |
| GET | `/reports/stock-by-warehouse`, `/sales-by-day`, `/top-products`, `/movements-by-type` | Reportes globales |
| GET | `/sync/runs` | Historial de catch-ups y snapshots |
| GET | `/dashboard` | Tablero web (usa estos mismos endpoints) |
| POST | `/sync/warehouses/{id}/catch-up`, `/snapshot` | Forzar una sincronización |

### Lo nuevo en cada bodega

| Método | Ruta | Para qué |
|---|---|---|
| GET | `/sync/events?after_sequence=N` | Eventos posteriores a N (catch-up) |
| GET | `/sync/snapshot` | Foto consistente de productos e inventario |
| GET | `/inventory/by-sku/{sku}` | Saldo por SKU (el id local cambia entre bodegas; el SKU no) |

### Variables nuevas

| Dónde | Variable | Por defecto | Qué hace |
|---|---|---|---|
| Bodega | `CENTRAL_AMQP_URL` | `amqp://platform:platform@central_rabbitmq:5672/%2F` | RabbitMQ central. Vacío = bodega sin central |
| Bodega | `ADVERTISED_API_URL` | `http://lb_warehouse_<WH_ID>:80` | Cómo la central llama a esta bodega |
| Bodega | `HEARTBEAT_SECONDS` | `15` | Frecuencia del heartbeat |
| Central | `ONLINE_AFTER_SECONDS` | `45` | Sin heartbeat por más de esto → `OFFLINE` |
| Central | `RECONCILE_SECONDS` / `GAP_GRACE_SECONDS` | `15` / `20` | Cada cuánto revisa huecos y cuánto espera antes de pedirlos |
| Central | `WAREHOUSE_API_URLS` | vacío | Opcional, `WH-001=http://...`; si no, se aprenden de los heartbeats |

---

## 8. Terraform: la central en AWS

`terraform apply` ahora crea **4 EC2**: la central primero (las bodegas necesitan su IP privada) y
después las bodegas. El módulo `warehouse_node` se generalizó a **`ec2_node`** (sirve para bodegas y
para la central).

| Qué | Cómo |
|---|---|
| Security group `central` | API central (8090) abierta a `allowed_cidr`; **AMQP (5680) SOLO desde el security group de las bodegas**; consola de RabbitMQ y SSH solo `admin_cidr` |
| Regla nueva en el SG de bodegas | La API de cada bodega acepta conexiones **desde el SG de la central** (para `/sync` y la consulta en vivo) |
| `runtime.env` de cada bodega | `CENTRAL_AMQP_URL=amqp://platform:<clave>@<IP privada de la central>:5680/%2F` |
| `user_data` | Al arrancar, cada bodega averigua su IP privada (IMDSv2) y escribe `ADVERTISED_API_URL=http://<IP privada>:<puerto>` |
| Variables nuevas | `central_instance_type`, `central_port`, `central_rabbit_port`, `central_rabbit_mgmt_port`, `central_rabbit_password` (sensible) |
| `terraform output central` | URL de Swagger, consola de RabbitMQ y comando SSH |

Todo el tráfico bodega ↔ central va por la **red privada** de la VPC.

---

## 9. Pruebas

| Archivo | Qué comprueba |
|---|---|
| `tests/central/test_central_projection.py` (9) | Saldo, historial y catálogo; duplicados; eventos desordenados; watermark que se detiene en el hueco; heartbeats; bajas; alertas; snapshot con versiones |
| `tests/central/test_central_sync.py` (5) | Catch-up que llena el hueco; bodega caída (queda registrado y se reintenta); espera de gracia; snapshot inicial + catch-up; eventos ya borrados → snapshot |
| `tests/central/test_central_api.py` (6) | Inventario global, bodega sin contacto marcada, 404, reportes, health, tablero |
| `tests/unit/test_sync_and_forwarder.py` (6) | `/sync/events`, `/sync/snapshot`, `/inventory/by-sku`, heartbeat; el forwarder hace ack **solo** después de que la central confirma y, si está caída, el mensaje se queda |
| `tests/integration/test_central_integration.py` (4) | Con Docker: un movimiento llega a la central, la bodega aparece ONLINE y al día, la consulta en vivo llega a la bodega |

```powershell
pytest tests/unit tests/central      # 102 pruebas, sin Docker
pytest tests/integration             # con la central y WH-001 levantadas
```

La integración continua (`.github/workflows/ci.yml`) ahora corre `tests/central`, valida
`compose-central.yml` y levanta la central junto a WH-001 para las pruebas de integración.

---

## 10. Respuestas para la sustentación

**¿Qué es CQRS?** Separar el modelo con el que se escribe (la base de cada bodega) del modelo con el
que se consulta (la base central, armada con eventos). Cada uno se diseña para su trabajo.

**¿Qué es consistencia eventual?** Que el modelo de lectura puede ir unos segundos atrás de la
realidad, pero si no hay cambios nuevos, termina igual a ella. Aquí además se **dice** cuándo un dato
puede estar viejo (`stale`, `warnings`, `known_by_central_at`).

**¿Por qué la central no consulta las bases de las bodegas directamente?** Porque acoplaría la central a
la disponibilidad y al esquema de cada bodega, y cada consulta global costaría N consultas remotas
(alternativa A).

**¿Cómo garantizan que no se pierde nada si se cae la central?** Las bodegas no dependen de ella para
operar. Sus eventos se guardan primero en el outbox (misma transacción que el cambio), después en una
cola durable local, y el forwarder solo los borra cuando la central confirmó que los guardó.

**¿Y si aun así se pierde un mensaje?** Las secuencias sin huecos lo delatan; el reconciliador pide lo
que falta a la bodega (`/sync/events`). El saldo además se autocorrige porque cada evento trae el
saldo absoluto con su versión.

**¿Por qué el saldo absoluto y no solo "+5 / −3"?** Con deltas, un evento perdido o repetido deja el
saldo mal para siempre. Con saldo absoluto + versión, el último evento siempre deja el dato correcto.

**¿Qué pasa con 100 bodegas?** Cada una manda sus eventos a la misma cola central; el proyector escala
con más instancias (el bloqueo por fila de bodega evita que dos avancen el mismo watermark a la vez) y
la consulta global sigue siendo una sola consulta local.

---

## 11. Archivos de este commit

| Archivo | Qué es |
|---|---|
| `src/central/` | **Nuevo.** Servicio central: API (`central/api.py`, `routers/`), modelo de lectura (`models.py`), proyección idempotente (`projection.py`), sincronización (`sync.py`), estado de bodegas (`status.py`), workers `projector` y `reconciler`, tablero web (`static/dashboard.html`), `Dockerfile` |
| `db/central_schema.sql` | **Nuevo.** Tablas del modelo de lectura |
| `src/common/platform_common/central_topology.py` | **Nuevo.** Exchanges y cola del RabbitMQ central |
| `src/backend/app/app/workers/central_forwarder.py` | **Nuevo.** Store-and-forward hacia la central |
| `src/backend/app/app/workers/relay.py` | Heartbeat cada `HEARTBEAT_SECONDS` |
| `src/backend/app/app/modules/sync/` | **Nuevo.** `/sync/events` y `/sync/snapshot` |
| `src/backend/app/app/modules/inventory/*` | `GET /inventory/by-sku/{sku}` |
| `src/backend/app/app/config.py`, `events/topology.py`, `events/types.py`, `main.py` | Configuración de la central, cola `wh.central-forward`, `WAREHOUSE_HEARTBEAT`, router de sync |
| `docker/composes/compose-central.yml`, `docker/components/central/.env` | **Nuevo.** La central en Docker |
| `docker/composes/compose-warehouse.yml`, `docker/components/wh_00X/.env` | Servicio `central_forwarder` y red `warehouse_platform` |
| `manage.ps1`, `manage_warehouse.sh` | Objetivo `central`; `start all` levanta la central primero; crean la red |
| `basic-docker-compose.yml` | Nota sobre la red `warehouse_platform` |
| `terraform/` | EC2 y security group de la central; módulo `ec2_node`; `runtime.env` y `ADVERTISED_API_URL` |
| `scripts/demo_fase7.py` | **Nuevo.** Los 7 experimentos contra Docker |
| `tests/central/`, `tests/unit/test_sync_and_forwarder.py`, `tests/integration/test_central_integration.py` | Pruebas nuevas |
| `tests/http/central-api.http` | **Nuevo.** Colección de peticiones de la central |
| `.github/workflows/ci.yml`, `pytest.ini` | Pruebas de la central en CI |
| `docs/adr/ADR-007-central-cqrs-y-sincronizacion.md` | Decisión de arquitectura |
