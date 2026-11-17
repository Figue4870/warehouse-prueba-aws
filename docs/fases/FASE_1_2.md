# Fase 1 y Fase 2 · Warehouse Cloud Platform (commit 0)

**Proyecto:** Warehouse Cloud Platform (Computación en la Nube e IoT)
**Alcance de este trabajo:** Fase 1 (API + PostgreSQL) y Fase 2 (Redis + Cache-Aside)
**Basado en:** `README.md`, guías de `docs/` y el PDF **Bodegas 1** (requerimientos del equipo)
**Commit:** 0 (base del repositorio, antes del trabajo de las 4 personas)

---

## 0. Resumen en 1 minuto

- **Fase 1 terminada.** La API de una bodega crea, consulta, actualiza y da de baja productos (con los campos del PDF: proveedor, lote, costo, precio de tienda y precio mayorista). También registra entradas, salidas, ajustes y reservas, y guarda el historial completo. El inventario **nunca** queda negativo, ni siquiera con muchas personas sacando el mismo producto al mismo tiempo. Cada movimiento es una **transacción**: o se guarda todo, o no se guarda nada.
- **Fase 2 terminada.** Las consultas de saldo (`GET /inventory/{id}`) pasan primero por **Redis** con el patrón **Cache-Aside**, un TTL de 30 s y borrado de la clave después de cada movimiento. Si Redis se cae, la API sigue funcionando con PostgreSQL.
- **Resultado del experimento:** con Redis, 5.000 lecturas hicieron **0 consultas SQL** (sin Redis fueron 5.000). Con 20 a 50 usuarios simultáneos, la API atendió **entre 2,5 y 3 veces más peticiones por segundo**.
- **Pruebas:** 43 automáticas (38 unitarias y 5 de integración contra PostgreSQL y Redis reales), todas pasando. Las 27 peticiones de la colección HTTP también se ejecutaron y dieron el resultado esperado.

> ⚠️ **Lo único que no pude verificar:** construir las imágenes con `docker compose up --build`. Mi entorno de pruebas no tiene acceso a Docker Hub ni a PyPI. Sí validé todos los archivos Compose con `docker compose config` y probé la API con PostgreSQL 16 y Redis 7 reales. **Lo primero que deben hacer ustedes es levantarlo con Docker** (sección 3) y correr las pruebas de integración.

---

## 1. Qué encontré al revisar el proyecto (antes de cambiar nada)

### 1.1 Lo que ya estaba bien hecho (y se conservó)

| Qué | Por qué está bien |
|---|---|
| Estructura modular `controller → service → repository` | Cada capa tiene una sola responsabilidad. El controlador no toca la BD, el repositorio no decide reglas. |
| `SELECT ... FOR UPDATE` al procesar movimientos | Es la técnica correcta contra salidas concurrentes (ver 4.6). |
| Commit y rollback centralizados en el servicio | La transacción se controla en un solo lugar. |
| Producto + inventario inicial creados en la misma transacción | No puede existir un producto sin su fila de inventario. |
| `ProductService` usa el **servicio** de inventario, no su repositorio | Buen desacoplamiento entre módulos. |

### 1.2 Lo que faltaba para cumplir la Fase 1 y la Fase 2

- `PUT /products/{id}` y `DELETE /products/{id}` (están en el README, sección 7).
- `schema.sql` (es un entregable): las tablas se creaban "por magia" al importar la app.
- Pruebas unitarias, pruebas de integración y colección de pruebas HTTP (entregables de la Fase 1).
- El tipo `ADJUSTMENT` estaba en la lista del README, pero el código lo **rechazaba** ("Tipo de movimiento inválido").
- Los campos de producto que pide el PDF: proveedor, lote, costo del proveedor, precio de tienda y precio mayorista.
- El log automático cuando un producto se agota (PDF).
- Toda la Fase 2: no había Redis.

### 1.3 Errores encontrados

| # | Error | Dónde | Consecuencia |
|---|---|---|---|
| 1 | `WAREHOUSE_ID=warehouse` (fijo) | compose de wh_001 y wh_002 | **Todas** las bodegas guardaban su inventario como `"warehouse"` en lugar de `WH-001` / `WH-002`. |
| 2 | `basic-docker-compose.yml` no arrancaba | usaba una red y un volumen que no estaban declarados | `docker compose` respondía: *refers to undefined network warehouse_net_WH-001*. |
| 3 | Versiones antiguas en `requirements.txt` | SQLAlchemy 2.0.28, pydantic 2.6.4, psycopg2-binary 2.9.9 | En Python 3.13 SQLAlchemy 2.0.28 **falla al importarse** (lo comprobé), y pydantic 2.6.4 (por su componente `pydantic-core`) y psycopg2-binary 2.9.9 no traen instalador para esa versión de Python. Correr las pruebas en un PC con Python reciente era imposible. |
| 4 | Sin restricciones en la base de datos | tablas | Nada en PostgreSQL impedía un saldo negativo si el código fallaba. `inventory` no tenía llave foránea hacia `products`. |
| 5 | Errores internos expuestos | `detail=str(e)` | Un error 500 podía mostrarle al usuario el SQL interno. Un SKU repetido devolvía el texto crudo del error de la BD. |
| 6 | El frontend insertaba datos como HTML (`innerHTML`) | `app.js` | Un producto llamado `<img src=x onerror=...>` podía ejecutar código en el navegador (ataque XSS). |
| 7 | Healthcheck de PostgreSQL sin `-h` | compose | Podía decir "listo" mientras PostgreSQL aún estaba creando las tablas, y la API arrancaba antes de tiempo. |
| 8 | El título del frontend decía "Warehouse Central Node" | `index.html` | Confunde: es una bodega, no la plataforma central. |

### 1.4 Observación sobre la carpeta `docs/` (no la modifiqué)

Los nombres de archivo no coinciden con su contenido: `01-fase-1...md` es una copia del README, la guía de la **Fase 1** está en `02-fase-2...md`, la de la **Fase 2** está en `03-fase-3...md`, y no existe una guía separada de la Fase 3 (está solo en el README, sección 10). Conviene renombrarlos para que nadie del equipo se confunda.

---

## 2. Mapa de archivos: qué cambió

Rutas relativas a la raíz del repositorio.

### Archivos nuevos

| Archivo | Para qué sirve |
|---|---|
| `db/schema.sql` | Crea las 4 tablas con sus restricciones. Docker lo ejecuta solo la primera vez. |
| `db/seed.sql` | 5 productos de ejemplo con stock. Opcional; se puede correr varias veces sin duplicar. |
| `docker/components/wh_00X/.env` (ampliado) | Variables de cada bodega: credenciales de desarrollo, puertos, Redis y caché. |
| `src/backend/app/app/cache.py` | **Fase 2:** toda la lógica de Redis (Cache-Aside, TTL, invalidación, fallback, métricas). |
| `src/backend/app/app/logging_config.py` | Logs en formato JSON (estructurados). |
| `src/backend/app/app/modules/inventory/inventory_rules.py` | La regla "nunca negativo" como función pura, fácil de probar. |
| `src/backend/app/app/modules/alerts/*` | Módulo de alertas automáticas (agotado y desfase) + `GET /alerts`. |
| `src/backend/app/app/modules/shared/errors.py` | `BusinessRuleError`: error de regla de negocio (se responde con 409). |
| `src/backend/app/requirements-dev.txt` | pytest + httpx (solo para pruebas). |
| `src/backend/app/.dockerignore` | Evita copiar basura (`__pycache__`, `.env`) a la imagen. |
| `pytest.ini` | Configuración de pytest. |
| `tests/unit/*` | 38 pruebas unitarias (no necesitan Docker). |
| `tests/integration/*` | 5 pruebas contra la API real (incluye la prueba de concurrencia). |
| `tests/http/warehouse-api.http` y `.postman_collection.json` | Colección de 27 peticiones (REST Client de VS Code y Postman). |
| `tests/load/benchmark_cache.py` y `locustfile.py` | Experimento con y sin Redis. |
| `README.md` | Cómo levantar y probar (versión corta de este informe). |
| `docs/adr/ADR-001..003` | Decisiones de arquitectura en el formato que pide la guía de entrega. |

### Archivos modificados

| Archivo | Cambio principal |
|---|---|
| `main.py` | Crea las tablas al arrancar (no al importar), configura los logs, responde 503 si PostgreSQL está caído, oculta los errores internos (500) y agrega el router de alertas. |
| `app/config.py` | Variables nuevas: Redis, caché, TTL, `AUTO_CREATE_TABLES`. |
| `app/database.py` | Contador de consultas SQL (para el experimento de la Fase 2). |
| `modules/products/*` | Campos del PDF, `PUT`, `DELETE` (borrado lógico), SKU duplicado → 409. |
| `modules/inventory/*` | `ADJUSTMENT`, tipos validados con un `Enum`, saldo después de cada movimiento, alertas, caché, 409/404/422 claros. |
| `modules/health/health_controller.py` | `/health` revisa PostgreSQL y Redis. Nuevos `/metrics` y `/metrics/reset`. |
| `requirements.txt` | Versiones compatibles con Python 3.11 a 3.13, más `redis`. |
| `Dockerfile` (backend) | Logs sin buffer (`PYTHONUNBUFFERED`). |
| `docker/components/wh_00X/compose-warehouse.yml` | + Redis, + `schema.sql`, + volumen persistente, `WAREHOUSE_ID` corregido, healthchecks. Las variables nuevas tienen valor por defecto (`${REDIS_PORT:-6380}`), así que funcionan con los `.env` que ya tenían. |
| `basic-docker-compose.yml` | Arreglado: ahora levanta una sola bodega (WH-001) completa. |
| `docker-compose.yml` | Sigue levantando 2 bodegas, ahora cada una con su propio Redis. |
| `docker/composes/compose-base-warehouse.yml` | Plantilla genérica actualizada (con Redis) y válida. |
| `docker/composes/compose-warehouse.yml` | Solo se le agregó un comentario: **nada lo usa**, se puede borrar. |
| `src/frontend/app/*` | Campos nuevos, indicador HIT/MISS por fila, panel de caché, panel de alertas, baja de productos, corrección del XSS, versión para celular. |

### Código de referencia

El código original (antes de las fases 1 y 2) es el que se usó en el laboratorio de AWS (repositorio del laboratorio). Este repositorio arranca ya con las fases 1 y 2 aplicadas.

---

## 3. Cómo correrlo en tu PC (paso a paso)

Necesitas Docker Desktop abierto. En PowerShell:

```powershell
cd warehouse-cloud-platform   # la carpeta donde clonaste el repositorio

# 1. Levantar la bodega WH-001 (PostgreSQL + Redis + API + página web)
docker compose -f basic-docker-compose.yml up -d --build

# 2. Ver que todo esté arriba (los 4 contenedores deben estar "running"/"healthy")
docker compose -f basic-docker-compose.yml ps

# 3. (Opcional) cargar los 5 productos de ejemplo
docker exec db_warehouse_WH-001 psql -U postgres -d warehouse_db -v wh=WH-001 -f /db/seed.sql
```

Luego abre:

- **http://localhost:8081**: la página web de la bodega.
- **http://localhost:8001/docs**: Swagger, para probar cada endpoint desde el navegador.
- **http://localhost:8001/health**: debe responder `{"status":"ok","warehouse_id":"WH-001","database":"ok","redis":"ok"}`.

Para correr las pruebas:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r src/backend/app/requirements-dev.txt
pytest tests/unit          # no necesita Docker
pytest tests/integration   # necesita el paso 1
```

> 💡 **Si ya tenían la base de datos creada con el código viejo**, bórrenla una vez: `docker compose -f basic-docker-compose.yml down -v` (el `-v` borra el volumen). El esquema cambió y `schema.sql` solo se ejecuta cuando el volumen está vacío.

---

## 4. Fase 1 explicada: una bodega con API + PostgreSQL

### 4.1 La arquitectura

```text
Navegador (http://localhost:8081)
     │  /api/...
     ▼
Nginx (frontend) ──► API FastAPI (puerto 8001) ──► PostgreSQL (fuente de verdad)
```

Dentro de la API, cada petición pasa por tres capas:

```text
Controller  →  recibe el HTTP, valida con Pydantic, responde.
Service     →  aplica las reglas de negocio y controla la transacción.
Repository  →  habla con la base de datos (SQL vía SQLAlchemy).
```

### 4.2 El modelo de datos (4 tablas)

```text
products ──1:1── inventory            (saldo actual de cada producto en esta bodega)
    │
    ├──1:N── inventory_movements      (historial: cada entrada, salida, ajuste...)
    │
    └──1:N── inventory_alerts ──► inventory_movements   (qué movimiento causó la alerta)
```

**`products`.** Además de lo que pedía el README (sku, name, description, category, unit), tiene lo del PDF:

| Campo | Qué es (según el PDF) |
|---|---|
| `sku` | ID / código universal, único |
| `name` | Nombre universal |
| `supplier` | Proveedor asociado |
| `lot` | Lote |
| `supplier_cost` | Costo / precio del proveedor |
| `retail_price` | Precio de venta en tienda |
| `wholesale_price` | Precio para mayoristas / terceros |
| `is_active` | `false` = dado de baja (ver 4.9) |

> 💡 **¿Por qué el dinero es `NUMERIC` y no `float`?** Porque `float` redondea en binario: `0.1 + 0.2 = 0.30000000000000004`. Con dinero eso se nota. `NUMERIC(14,2)` guarda exactamente 2 decimales. Por eso en el JSON los precios llegan como texto: `"32000.00"`.

> 💡 **¿Y la "cantidad en stock" del PDF?** No va en `products`, va en `inventory`. Así, en la Fase 4 el mismo producto puede tener un saldo distinto en cada bodega.

**`inventory`.** Hay dos números:
- `quantity` = unidades **disponibles** (se pueden vender).
- `reserved_quantity` = unidades **apartadas** (reservadas para un pedido).
- Total físico en la estantería = `quantity + reserved_quantity`.

**`inventory_movements`** es el *kardex*: nunca se edita ni se borra. Cada fila guarda el tipo, la cantidad, la referencia (factura, guía o motivo), el lote y **el saldo que quedó después** (`quantity_after`, `reserved_after`). Con eso se pueden hacer reportes de compras y ventas sin recalcular nada, como pide el PDF.

### 4.3 Los endpoints

| Método | Ruta | Respuestas posibles |
|---|---|---|
| GET | `/health` | 200 ok · 503 si PostgreSQL está caído |
| POST | `/products` | 201 creado · 409 SKU repetido · 422 datos inválidos |
| GET | `/products` | 200 (`?include_inactive=true` muestra también los dados de baja) |
| GET | `/products/{id}` | 200 · 404 |
| PUT | `/products/{id}` | 200 · 404 · 409 si está dado de baja · 422 si se intenta cambiar el SKU |
| DELETE | `/products/{id}` | 204 · 409 si tiene existencias |
| GET | `/inventory/{product_id}` | 200 + cabecera `X-Cache` · 404 |
| POST | `/inventory/movements` | 201 · 404 · 409 regla violada (stock insuficiente) · 422 datos inválidos |
| GET | `/inventory/{product_id}/movements` | 200 (el más reciente primero; `?limit=&offset=`) · 404 |
| GET | `/alerts` | 200 (`?type=OUT_OF_STOCK` o `DISCREPANCY`, `?product_id=`) |
| GET | `/metrics` · POST `/metrics/reset` | 200 |

**Qué significa cada código HTTP:**

| Código | Significado | Ejemplo en este proyecto |
|---|---|---|
| 200 | OK | Consultar un saldo |
| 201 | Creado | Registrar un movimiento |
| 204 | Hecho, sin contenido que devolver | Dar de baja un producto |
| 404 | No existe | Producto 9999 |
| 409 | **Conflicto:** la petición está bien escrita, pero choca con el estado actual | Sacar 15 cuando hay 6 |
| 422 | **Datos inválidos:** la petición está mal escrita | Cantidad 0, precio negativo |
| 500 | Error interno (se registra en el log, nunca se muestra el detalle) | Un fallo inesperado |
| 503 | Servicio no disponible | PostgreSQL caído (`/health` o cualquier operación que necesite la BD) |

### 4.4 La regla de oro: el inventario nunca queda negativo

Se protege con **cuatro capas**, como las cerraduras de una puerta: si una falla, la siguiente detiene el problema.

| Capa | Dónde | Qué detiene |
|---|---|---|
| 1. Validación | `schemas.py` (Pydantic) | Cantidad 0 o negativa (salvo en ajustes) → **422** |
| 2. Regla de negocio | `inventory_rules.py` → `calculate_new_balance()` | Sacar más de lo disponible → **409** |
| 3. Bloqueo de fila | `SELECT ... FOR UPDATE` | Dos salidas simultáneas que "ven" el mismo saldo (4.6) |
| 4. Base de datos | `CHECK (quantity >= 0)` en `schema.sql` | Cualquier error de programación futuro |

Tabla de tipos de movimiento (`q` = cantidad):

| Tipo | Disponible | Reservado | Se rechaza si… |
|---|---|---|---|
| `RECEIPT` (entrada/compra) | + q | — | — |
| `SHIPMENT` (salida/venta) | − q | — | q > disponible |
| `ADJUSTMENT` (desfase) | ± q (con signo) | — | el resultado < 0. **Genera una alerta DISCREPANCY.** |
| `RESERVATION` | − q | + q | q > disponible |
| `RELEASE` | + q | − q | q > reservado |
| `RETURN` (devolución) | + q | — | — |
| `TRANSFER_IN` | + q | — | — (en la Fase 6 lo hará la Saga) |
| `TRANSFER_OUT` | − q | — | q > disponible (en la Fase 6 lo hará la Saga) |

> 💡 Las unidades **reservadas no se pueden vender**. Si hay 3 disponibles y 7 reservadas, una venta de 5 se rechaza.

### 4.5 La transacción: "todo o nada"

Una salida tiene que cambiar el saldo **y** dejar el registro en el historial. Si solo pasara una de las dos cosas, el sistema mentiría (por ejemplo, un saldo que bajó sin ningún movimiento que lo explique). Así se ve en `inventory_service.py`, simplificado:

```python
try:
    inventario = repo.get_by_product_for_update(id)      # BEGIN + SELECT ... FOR UPDATE
    nuevo, reservado = calculate_new_balance(...)         # regla de negocio
    repo.update_stock(inventario, nuevo, reservado)       # UPDATE inventory
    mov = repo.register_movement(...)                     # INSERT inventory_movements
    if se_agotó: alerts.register(...)                     # INSERT inventory_alerts
    db.commit()                                           # COMMIT: TODO se guarda junto
except ...:
    db.rollback()                                         # ROLLBACK: NADA se guarda
cache.invalidate(id)                                      # Fase 2: después del COMMIT
```

**¿Cómo sé que el ROLLBACK funciona?** La prueba `test_rollback_si_algo_falla_a_mitad_de_la_transaccion` provoca a propósito un error *después* de actualizar el saldo y crear el movimiento. Resultado: el saldo sigue igual, el movimiento no existe y la API responde 500 sin mostrar detalles internos.

### 4.6 Dos salidas al mismo tiempo: la prueba de concurrencia

El problema se llama **condición de carrera** (*race condition*):

```text
Stock = 1
Cajero A: lee stock → 1 ✓        Cajero B: lee stock → 1 ✓   (los dos leen antes de que el otro escriba)
Cajero A: vende 1 → guarda 0     Cajero B: vende 1 → guarda 0
Resultado: se vendieron 2 unidades que no existían.
```

Con `SELECT ... FOR UPDATE`, el cajero B **espera** a que A termine su transacción. Cuando por fin lee, ya ve 0 y su venta se rechaza.

**Experimento real que hice.** Stock = 10 y **25 salidas de 1 unidad lanzadas al mismo tiempo**:

| Versión | Salidas aceptadas (debían ser 10) |
|---|---|
| **Con** `FOR UPDATE` (código entregado) | **10** ✅ y 15 rechazadas con 409 |
| **Sin** `FOR UPDATE` (lo quité a propósito para probar) | **22, 23 y 13** en tres intentos ❌ |

Sin el bloqueo se "vendieron" hasta 23 unidades teniendo 10. Esta prueba quedó automatizada en `tests/integration/test_api_integration.py::test_salidas_concurrentes_nunca_dejan_negativo`.

### 4.7 Alertas automáticas (requisitos del PDF)

- **"Generar logs automáticos cuando se agota el inventario":** cuando el disponible pasa de más de 0 a exactamente 0, se crea una alerta `OUT_OF_STOCK` y se escribe un log JSON con `"event": "out_of_stock"`.
- **"Gestionar desfase... registrar el log de la incongruencia":** cada `ADJUSTMENT` crea una alerta `DISCREPANCY` con lo que había, el ajuste y el motivo. Por ejemplo: *"el sistema tenía 50, se ajustó −2 (ahora 48). Motivo: Conteo físico estante A3"*.
- La alerta se guarda **en la misma transacción** que el movimiento: si el movimiento se revierte, la alerta también. Nunca queda una alerta de algo que no pasó.
- La respuesta del movimiento trae `"alerts_generated": ["OUT_OF_STOCK"]`, y la página web muestra un aviso.
- **Lo que falta (Fase 3):** el PDF dice que la notificación al administrador de bodega es **asíncrona**. Eso se hará publicando estas alertas en RabbitMQ.

### 4.8 Logs estructurados

Los logs importantes salen en JSON, una línea por evento:

```json
{"timestamp": "2026-10-26T17:32:31Z", "level": "WARNING", "service": "warehouse-api",
 "warehouse_id": "WH-001", "event": "out_of_stock", "product_id": 3, "quantity_after": 0}
```

Así se puede filtrar por campo (por ejemplo, todos los `out_of_stock` de WH-001). Es la base para la observabilidad de la Fase 11. Para verlos: `docker logs warehouse_api_node_WH-001`.

### 4.9 Dar de baja un producto (borrado lógico)

`DELETE /products/{id}` **no borra la fila**: marca `is_active = false`. El motivo es que sus movimientos (el historial que el PDF exige para reportes) apuntan a ese producto. Si se borrara, el historial quedaría huérfano. Reglas:

- Solo se permite si tiene 0 disponibles y 0 reservadas (si no, 409).
- Un producto dado de baja no admite movimientos (409) y no sale en `GET /products`.
- Su historial sigue consultable.

### 4.10 Respuestas a las preguntas de la guía de la Fase 1

**¿Qué información es estado?** El catálogo de productos, el saldo de cada producto (disponible y reservado), el historial de movimientos y las alertas. Todo vive en PostgreSQL. La API **no guarda estado propio en memoria**: es *stateless*, y por eso en la Fase 5 se podrán poner varias copias detrás de un balanceador.

**¿Qué información debe persistir?** Todo lo anterior. Lo que **no** necesita persistir es el caché de Redis (es una copia que se puede reconstruir, por eso Redis corre sin guardar en disco).

**¿Por qué una salida necesita una transacción?** Porque toca dos o tres tablas (saldo, historial y quizás alerta), y deben cambiar juntas o no cambiar. Sin transacción, un fallo a mitad de camino deja el saldo y el historial contándose historias distintas.

**¿Qué sucede con dos salidas concurrentes?** Sin bloqueo, ambas pueden leer el mismo saldo y vender unidades que no existen (lo comprobé: 23 ventas con 10 unidades). Con `FOR UPDATE` la segunda espera a la primera y luego ve el saldo real. Además, el `CHECK` de PostgreSQL impediría un negativo como última defensa.

---

## 5. Fase 2 explicada: Redis y Cache-Aside

### 5.1 El problema

Consultar un saldo es mucho más frecuente que cambiarlo. Si cada `GET /inventory/5` va a PostgreSQL, la base trabaja de más respondiendo siempre lo mismo. Redis es una base de datos **en memoria** (RAM), muy rápida, que usamos como copia temporal.

### 5.2 Cache-Aside paso a paso

```text
GET /inventory/5
      │
      ▼
 ¿Está "warehouse:WH-001:inventory:5" en Redis?
      │
   ┌──┴───┐
  SÍ      NO
 (HIT)  (MISS)
   │      │
   │      ▼
   │   Leer PostgreSQL
   │      │
   │      ▼
   │   Guardar en Redis por 30 s
   │      │
   └──────┴──► Responder (con cabecera X-Cache: HIT o MISS)
```

Se llama *Cache-**Aside*** ("al lado") porque Redis no sabe nada de PostgreSQL: la **API** decide cuándo leer, cuándo guardar y cuándo borrar. El código está en `app/cache.py` y en `InventoryService.get_stock()`.

### 5.3 La clave

```text
warehouse:WH-001:inventory:5
   │        │        │     └── id del producto
   │        │        └──────── qué tipo de dato es
   │        └───────────────── la bodega (sale de WAREHOUSE_ID)
   └────────────────────────── prefijo del sistema
```

Incluir la bodega evita que el producto 5 de WH-001 y el producto 5 de WH-002 choquen si algún día compartieran un mismo Redis.

### 5.4 TTL (tiempo de vida)

Cada clave se guarda con `ex=30`: Redis la **borra sola** a los 30 segundos. Es la red de seguridad: pase lo que pase, ningún dato viejo vive más de 30 s. Se cambia con `CACHE_TTL_SECONDS`.

### 5.5 Invalidación: que el caché no mienta

El problema (está en el README):

```text
Redis = 100, PostgreSQL = 100  →  salida de 10  →  PostgreSQL = 90 … pero Redis sigue diciendo 100
```

**Implementé la Estrategia A (invalidación síncrona):** después de cada movimiento exitoso, la API borra la clave. La próxima lectura será MISS y traerá el 90.

**¿Por qué borrar DESPUÉS del COMMIT y no antes?**
- Si se borra **antes**, otra petición puede leer en ese instante el valor viejo de PostgreSQL (todavía sin confirmar) y volver a guardarlo en Redis.
- Si la transacción falla (ROLLBACK), no tiene sentido haber borrado nada. Por eso un movimiento **rechazado** no toca el caché (hay una prueba para eso).

**Comparación con la Estrategia B** (el README pide compararlas; la B necesita RabbitMQ, que llega en la Fase 3):

| | A: síncrona (implementada) | B: asíncrona con RabbitMQ (Fase 3) |
|---|---|---|
| Cómo | La API borra la clave al terminar | La API publica un evento y un *Cache Worker* borra la clave |
| Rapidez de la escritura | Un poco más lenta (+1 llamada a Redis, menos de 1 ms) | Más rápida (solo publica el evento) |
| Ventana de dato viejo | Casi cero | Lo que tarde el worker (milisegundos o más si hay cola) |
| Si Redis falla | La escritura sigue; el TTL limpia el dato | El evento espera en la cola y se reintenta |
| Complejidad | Muy baja | Mayor (broker, worker, reintentos, duplicados) |
| Ventaja extra | — | Un mismo evento alimenta caché, auditoría y analítica |

**Caso raro que hay que conocer** (útil para la sustentación):

```text
t1  Lector:   GET → MISS → lee PostgreSQL (100)
t2  Escritor: UPDATE a 90 → COMMIT → borra la clave
t3  Lector:   guarda 100 en Redis   ← llegó tarde, guardó el valor viejo
```

Redis queda con 100 hasta que el TTL lo borre (máximo 30 s) o llegue otro movimiento. Es poco probable y está acotado por el TTL. Lo importante es que **las escrituras nunca leen de Redis**: siempre leen PostgreSQL con bloqueo. Un dato viejo en caché puede mostrar un número desactualizado unos segundos, pero **jamás** permite vender lo que no hay.

### 5.6 ¿Y si Redis se cae?

PostgreSQL es la fuente de verdad. Redis solo acelera, así que:

1. Si Redis no responde en 0,5 s, se ignora y se lee PostgreSQL. La respuesta lleva `X-Cache: BYPASS`.
2. Tras un fallo, la API **deja de intentar Redis durante 5 segundos**, para no hacer esperar 0,5 s a cada petición.
3. `/health` sigue diciendo `"status":"ok"` (la bodega funciona), pero con `"redis":"down"`.
4. Cuando Redis vuelve, la API lo retoma sola (no hay que reiniciar nada).
5. En `docker-compose`, la API depende de Redis solo con `service_started`, no `service_healthy`: Redis es opcional.

**Prueba real que hice** (apagando Redis con la API corriendo):

| Momento | Resultado |
|---|---|
| Redis arriba | `MISS` (12 ms) y luego `HIT` (2 ms) |
| Redis apagado: lecturas | `200` con `X-Cache: BYPASS` en unos 4–6 ms, sin errores |
| Redis apagado: una entrada de 5 unidades | `201` creada correctamente |
| Redis apagado: `/health` | `{"status":"ok", "database":"ok", "redis":"down"}` |
| Redis encendido otra vez | `/health` → `redis: ok`, y de nuevo `MISS` → `HIT` |

Para repetirlo ustedes: `docker stop redis_warehouse_WH-001`, prueben la página web y luego `docker start redis_warehouse_WH-001`.

**¿Y si el que se cae es PostgreSQL?** También lo probé:

| Petición con PostgreSQL apagado | Resultado |
|---|---|
| `GET /inventory/1` (estaba en Redis) | `200` con `X-Cache: HIT`: **sigue respondiendo** gracias al caché, como máximo 30 s |
| `GET /inventory/2` (no estaba en Redis) | `503` "Base de datos no disponible, intenta más tarde" |
| `POST /inventory/movements` y `POST /products` | `503`: no se puede escribir sin la fuente de verdad (es lo correcto) |
| `/health` | `503` con `"database":"down"` (en la Fase 5 el balanceador dejaría de enviarle tráfico) |
| PostgreSQL encendido otra vez | Todo vuelve a funcionar solo, sin reiniciar la API |

Es un efecto secundario útil de la Fase 2: el caché da algo de disponibilidad de **lectura** aunque la base esté caída.

### 5.7 Cómo "ver" el caché

- **Cabecera `X-Cache`:** en Swagger, en la colección `.http`, o en el navegador con F12 → pestaña *Network* → la petición `inventory/...` → *Headers*.
- **Página web:** la columna *Caché* de la tabla muestra HIT, MISS o BYPASS en cada producto. Pulsa "Actualizar" dos veces y verás cómo pasan de MISS a HIT.
- **`GET /metrics`:** hits, misses, hit ratio, claves guardadas y consultas SQL que hizo la API.
- **Dentro de Redis:** `docker exec -it redis_warehouse_WH-001 redis-cli`, luego `KEYS warehouse:*` y `TTL warehouse:WH-001:inventory:1`.

> 💡 Los contadores de hits/misses se guardan **en Redis**, no en la memoria de la API. Así, en la Fase 5, con 3 copias de la API, los contadores serán los de toda la bodega y no los de una sola copia (principio *stateless*).

### 5.8 El experimento: solo PostgreSQL vs PostgreSQL + Redis

La guía pide comparar latencia, peticiones por segundo, CPU, consultas SQL y hit ratio. Corrí `tests/load/benchmark_cache.py`: 5.000 lecturas de `GET /inventory/1`, con 1, 20 y 50 usuarios simultáneos, **dos rondas** (los números son el promedio).

| Usuarios simultáneos | Modo | Peticiones/s | P50 | P95 | P99 | Errores |
|---|---|---:|---:|---:|---:|---:|
| 1 | Solo PostgreSQL | 283 | 3,4 ms | 4,2 ms | 5,4 ms | 0 |
| 1 | **Con Redis** | **482** | **2,0 ms** | **2,5 ms** | **3,4 ms** | 0 |
| 20 | Solo PostgreSQL | 317 | 61 ms | 91 ms | 111 ms | 0 |
| 20 | **Con Redis** | **781** | **25 ms** | **34 ms** | **42 ms** | 0 |
| 50 | Solo PostgreSQL | 244 | 200 ms | 330 ms | 426 ms | 0 |
| 50 | **Con Redis** | **748** | **63 ms** | **79 ms** | **105 ms** | 0 |

| Carga sobre la base de datos | Solo PostgreSQL | Con Redis |
|---|---:|---:|
| Consultas SQL de la API (5.000 lecturas) | 5.000 | **0** |
| Transacciones atendidas por PostgreSQL (3.000 lecturas) | 6.087 | **5** |
| Filas leídas por PostgreSQL (3.000 lecturas) | 311.183 | **731** |
| CPU usada por la API (5.000 lecturas) | 13–22 s | **6–7 s** |
| Hit ratio | — | **100 %** |

**Cómo leer estos números:**

- **P50, P95, P99:** si ordenas los tiempos de respuesta de menor a mayor, el P95 es el tiempo bajo el cual cayó el 95 % de las peticiones. El P99 muestra la "cola lenta", lo que sufren los usuarios con peor suerte.
- **Con 1 usuario**, Redis baja la latencia un ~40 % (de 3,4 a 2,0 ms).
- **Con muchos usuarios** la diferencia crece: 2,5 a 3 veces más peticiones por segundo y un P95 hasta 4 veces menor. Sin caché, cada petición necesita una conexión del *pool* de PostgreSQL (máximo 15) y trabajo del ORM, y se forman filas de espera.
- **La API también gasta menos CPU con Redis**, porque se ahorra el trabajo de SQLAlchemy (armar la consulta y convertir filas en objetos).
- **El techo de unas 780 peticiones/s con Redis** es de la propia API: un solo proceso de Python. Para subirlo se agregan más copias de la API, que es justo la **Fase 5** (balanceo de carga).
- **El resultado más importante no depende de la máquina:** PostgreSQL pasó de 6.087 transacciones a 5. Esa capacidad queda libre para las escrituras, que son las que sí necesitan la base.

> ⚠️ **Advertencia honesta:** medí en mi entorno de pruebas, con todo en la misma máquina, sin red de por medio y con un driver de PostgreSQL en Python puro (más lento que el `psycopg2` de su Docker). En su PC los números absolutos serán distintos y probablemente la diferencia sea **menor**, porque su driver es más rápido. Repítanlo y usen **sus** números en la entrega.

**Cómo repetirlo** (con la bodega corriendo):

```powershell
# Con Redis
python tests/load/benchmark_cache.py --label con-redis --concurrency 20 --json con-redis.json

# Sin Redis: reiniciar la API con el caché apagado
$env:CACHE_ENABLED="false"; docker compose -f basic-docker-compose.yml up -d
python tests/load/benchmark_cache.py --label solo-postgres --concurrency 20 --json solo-postgres.json

# Volver a encender el caché
$env:CACHE_ENABLED="true"; docker compose -f basic-docker-compose.yml up -d
```

Mientras corre, abran otra terminal con `docker stats` para ver la CPU de la API y de PostgreSQL (la guía pide CPU). Si prefieren una interfaz con gráficas: `pip install locust` y `locust -f tests/load/locustfile.py --host http://localhost:8001`.

### 5.9 Respuestas a las preguntas de la guía de la Fase 2

**¿Qué ocurre si Redis se cae?** La bodega sigue funcionando al 100 % en lecturas y escrituras, solo más lenta, porque todo va a PostgreSQL (BYPASS). Probado en 5.6.

**¿Qué ocurre si el caché está obsoleto?** Una consulta puede mostrar un saldo viejo por pocos segundos (máximo el TTL, 30 s). Se mitiga con invalidación después de cada COMMIT más el TTL. Nunca afecta una venta, porque las escrituras leen PostgreSQL con bloqueo.

**¿Qué ocurre si dos requests actualizan el mismo producto?** El `FOR UPDATE` los ordena: el segundo espera al primero. Cada uno borra la clave después de su COMMIT. La siguiente lectura trae el saldo final correcto.

**¿Qué información nunca debería depender únicamente de Redis?** El saldo real para decidir una venta o una reserva, el historial de movimientos, las alertas y cualquier dato que no se pueda reconstruir. Redis es desechable: corre sin guardar en disco y, si se llena, expulsa claves (`allkeys-lru`).

---

## 6. Las pruebas

| Archivo | Qué comprueba | Necesita Docker |
|---|---|---|
| `tests/unit/test_inventory_rules.py` (14) | Cada tipo de movimiento; nunca negativo; reservadas no vendibles; ejemplos del README (10 − 4 = 6; 15 se rechaza) | No |
| `tests/unit/test_products_api.py` (9) | Crear con campos del PDF, SKU duplicado, validaciones, PUT, SKU inmutable, baja lógica, historial conservado | No |
| `tests/unit/test_inventory_api.py` (7) | Flujo del README, historial, **ROLLBACK**, alertas de agotado y desfase, reservas | No |
| `tests/unit/test_cache.py` (8) | MISS → HIT, formato de clave, TTL 30, invalidación, Redis caído, pausa de 5 s, caché apagado, ahorro de SQL | No |
| `tests/integration/test_api_integration.py` (5) | Health, flujo real, **concurrencia (25 salidas)**, Cache-Aside real, clave y TTL dentro de Redis | Sí |

> 💡 **¿Cómo corren las unitarias sin Docker?** `tests/unit/conftest.py` reemplaza PostgreSQL por SQLite en memoria y Redis por `FakeRedis` (un diccionario de Python que puede "caerse" a voluntad con `down = True`). Así cada prueba empieza con una base vacía y corre en milisegundos.

Resultado en mi entorno: **43 de 43 pasaron.** Además ejecuté las 27 peticiones de `tests/http/warehouse-api.http` en orden y todas dieron el código esperado (incluidos los errores esperados 409 y 422). También probé la página web en un navegador automatizado: no hubo errores de JavaScript, no se ejecutó el código del nombre "malicioso" y en celular no hay desbordamiento horizontal.

---

## 7. Requisitos del PDF: qué quedó cubierto y qué falta

| Requisito (PDF Bodegas 1) | Estado | Fase |
|---|---|---|
| Productos con categoría, descripción, nombre universal, ID, proveedor, lote, costo, precio de tienda y mayorista | ✅ Hecho | 1 |
| Stock por producto | ✅ Hecho (`inventory`) | 1 |
| Entradas, salidas, reservas y ajustes autónomos por bodega | ✅ Hecho | 1 |
| Historial de transacciones (compras/ventas) para reportes | ✅ Hecho (`inventory_movements` con saldo después) | 1 |
| Logs automáticos cuando se agota un producto | ✅ Hecho (`OUT_OF_STOCK` + log JSON) | 1 |
| Alertas por desfases o incongruencias (asíncronas) | 🟡 Se **registran**; la **notificación asíncrona** al administrador llega con RabbitMQ | 1 → 3 |
| Consultas frecuentes rápidas con Redis | ✅ Hecho | 2 |
| Consistencia estricta dentro de la bodega | ✅ Transacción + `FOR UPDATE` + `CHECK` | 1 |
| Autonomía y tolerancia a fallos | 🟡 Base lista: cada bodega con su BD y su Redis; si Redis cae, la bodega sigue | 2 → 4 |
| Observabilidad | 🟡 Logs JSON, `/health` con dependencias, `/metrics`. Faltan *correlation IDs* | 11 |
| Docker | ✅ Compose por bodega | 1–2 |
| Registro de bodegas (NIT, nombre, ubicación, dirección, administrador) | ⏳ Pendiente: lo hará la plataforma central. Hoy cada bodega solo tiene `WAREHOUSE_ID` | 4 / 7 |
| Transferencias entre bodegas (Saga) | ⏳ Pendiente. Existen `TRANSFER_IN/OUT` manuales | 6 |
| Procesamiento asíncrono (RabbitMQ y workers) | ⏳ Pendiente | 3 |
| Contratos digitales de proveedores en S3 | ⏳ Pendiente | 8 |
| Active Directory y roles (Admin Global, Líder, Admin Bodega, Registrador); sección de solicitudes | ⏳ Pendiente. **Hoy todos los endpoints están abiertos** | Seguridad (fase a definir por el equipo) |
| AWS y Terraform | ⏳ Pendiente | Nube |

---

## 8. Cosas a tener en cuenta

1. **No pude construir las imágenes Docker** (mi entorno no llega a Docker Hub ni a PyPI). Validé los Compose con `docker compose config` y probé la API real con PostgreSQL 16 y Redis 7. Si al hacer `up --build` algo falla, casi seguro es por una versión de `requirements.txt`. Pásenme el error.
2. **Versiones:** probé con exactamente las versiones de `requirements.txt` para FastAPI, Starlette, SQLAlchemy, pydantic-settings, Redis, pytest y httpx. Dos diferencias: en mi entorno pydantic era la 2.13 (ustedes instalarán la 2.10.4) y el driver de PostgreSQL era `psycopg` 3 en lugar de `psycopg2-binary`. Ninguna de las dos cambia la lógica del código.
3. **Requiere Python 3.11 o superior** para correr las pruebas en su PC (la imagen Docker ya usa 3.11).
4. **Base de datos vieja:** si ya tenían volúmenes con el esquema anterior, hagan `down -v` una vez.
5. **No levanten `basic-docker-compose.yml` y `docker-compose.yml` al mismo tiempo:** usan los mismos nombres de contenedor.
6. **Credenciales:** las de desarrollo (`postgres/postgres`, en los `.env` de cada bodega) son solo para eso, desarrollo. En la nube irán en un gestor de secretos (por ejemplo, AWS Secrets Manager), nunca en el repositorio.
7. **El frontend hace una petición por producto** para leer los saldos (problema "N+1"). Con pocos productos no importa, y de hecho sirve para ver el caché en acción. Con miles de productos convendría un endpoint de saldos en lote.

---

## 9. Mini glosario

| Término | En pocas palabras |
|---|---|
| **Transacción** | Grupo de operaciones que se guardan todas juntas (COMMIT) o ninguna (ROLLBACK). |
| **Atomicidad** | La "A" de ACID: todo o nada. |
| **Bloqueo de fila (`FOR UPDATE`)** | "Reservo esta fila hasta que termine; los demás esperen." |
| **Condición de carrera** | Error que aparece cuando dos procesos usan el mismo dato al mismo tiempo y el resultado depende de quién llegue primero. |
| **Caché** | Copia rápida de un dato que es caro de obtener. |
| **HIT / MISS / BYPASS** | El dato estaba en caché / no estaba y se fue a la BD / no se usó el caché. |
| **TTL** | *Time To Live*: segundos que vive un dato en el caché antes de borrarse solo. |
| **Invalidar** | Borrar del caché un dato que ya no es correcto. |
| **Cache-Aside** | Patrón en el que la aplicación consulta el caché y, si falla, la BD, y luego llena el caché. |
| **Fallback** | Plan B automático (si Redis falla, usar PostgreSQL). |
| **Stateless** | La API no guarda nada importante en su memoria: cualquier copia puede atender cualquier petición. |
| **Borrado lógico** | Marcar un registro como inactivo en vez de eliminarlo. |
| **Kardex** | Historial de entradas y salidas con el saldo después de cada una. |
| **Throughput** | Peticiones atendidas por segundo. |
| **P50 / P95 / P99** | Percentiles de latencia: el 50 %, 95 % o 99 % de las peticiones tardó menos que ese valor. |
| **Idempotente** | Que se puede repetir sin cambiar el resultado (`seed.sql` lo es). |
| **ADR** | *Architecture Decision Record*: documento corto con contexto, alternativas, decisión y consecuencias (ver `docs/adr/`). |

---

## 10. Siguiente paso: Fase 3 (RabbitMQ y workers)

El código quedó preparado para ella:

- Las alertas `OUT_OF_STOCK` y `DISCREPANCY` ya se crean en un solo lugar (`AlertService`). En la Fase 3 se publicarán como eventos en RabbitMQ y un *Notification Worker* avisará al administrador de bodega, como pide el PDF.
- La invalidación del caché está en un solo método (`cache.invalidate`). Será fácil implementar la Estrategia B (un *Cache Worker*) y compararla con la A.
- Cada movimiento ya tiene un `id` único que servirá como base del `event_id` para la idempotencia.
