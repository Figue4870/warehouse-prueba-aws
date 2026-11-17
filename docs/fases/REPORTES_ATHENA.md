# Reportes con Athena (sistema de reportes de la plataforma)

**Commit 4 · Persona 4** (junto con la Saga de la Fase 6)
**Objetivo:** un reporte sencillo con **Amazon Athena** sobre los datos de todas las bodegas, sin
cargar más a las bases de las bodegas ni a la base central.

---

## 1. Los tres niveles de reportes del proyecto

| Nivel | Dónde | Para qué | Fase |
|---|---|---|---|
| Reportes de UNA bodega | `POST /reports` en cada bodega (202 + report-worker) | Valorización, kardex de esa bodega | 3 |
| Reportes globales "en caliente" | `GET /reports/*` en la central (modelo de lectura en PostgreSQL) | Lo de hoy: stock por bodega, más vendidos | 7 |
| **Reportes analíticos con Athena** | `POST /reports/athena/{nombre}` en la central, o la consola de Athena | Históricos grandes sin tocar ninguna base de datos | **este commit** |

¿Por qué Athena si ya hay `/reports`? La base central está hecha para responder rápido al día a día;
consultas sobre meses de historia de todas las bodegas la cargarían. Athena lee **archivos en S3**:
no hay servidor que mantener, escala solo y se paga por lo que se lee.

---

## 2. Arquitectura

```text
 bodegas --eventos--> central (modelo de lectura, PostgreSQL)
                                  |
                          central_exporter (cada 60 s)
                                  |  JSON por líneas, un archivo por lote
                                  v
        S3: <bucket>/movements/dt=2026-11-16/<lote>.json
            <bucket>/transfers/dt=2026-11-16/<lote>.json
            <bucket>/inventory/dt=2026-11-16/snapshot-153000.json
                                  |
             Glue Data Catalog: base <proyecto>_analytics, tablas movements / transfers / inventory
                                  |
                       Athena (workgroup <proyecto>-reportes) --- resultados en <bucket>/athena-results/
                                  ^
         POST /reports/athena/ventas_por_dia   (la central usa el rol LabRole del Learner Lab: sin llaves)
         python scripts/athena_report.py ventas_por_dia --central http://<ip-central>:8090
         consola de Athena -> "Saved queries" (las mismas consultas, creadas por Terraform)
```

### Las tablas

Definidas una sola vez en `src/central/central/analytics/tables.json`: Terraform crea las tablas de
Glue con esas columnas y el exportador escribe exactamente esos campos (una prueba lo verifica).

| Tabla | Una fila es… | Columnas principales |
|---|---|---|
| `movements` | un movimiento de inventario de cualquier bodega | `warehouse_id, sku, product_name, type, quantity, unit_retail_price, reference, occurred_at` |
| `transfers` | una transferencia terminada (Saga) | `transfer_id, source/destination_warehouse_id, quantity, final_state, duration_seconds` |
| `inventory` | un SKU en una bodega en una foto (cada hora) | `snapshot_at, warehouse_id, sku, quantity, reserved_quantity, retail_price, warehouse_status` |

Todas están **particionadas por `dt`** (día, UTC) con *partition projection*: Athena calcula la
carpeta de cada día sin registrar particiones (sin `MSCK REPAIR TABLE` ni crawlers). Una consulta de
"los últimos 30 días" solo lee esas 30 carpetas.

### Sin duplicados (ni pérdidas) en la exportación

1. El exportador **aparta** un lote (cada fila recibe `export_batch_id`) y hace COMMIT.
2. Sube el lote a S3 con un **nombre fijo**: `<tabla>/dt=<día>/<lote>.json`.
3. Marca el lote como subido.

Si se cae entre 1 y 3 (o S3 no responde), la siguiente vuelta vuelve a subir **el mismo lote con el
mismo nombre**: el archivo se reemplaza, no se duplica. Y si la base central se reconstruye desde las
bodegas (Fase 7) y se exporta todo otra vez, las consultas usan `SELECT DISTINCT event_id …`: cada
movimiento se cuenta una sola vez aunque esté en dos archivos.

---

## 3. Los reportes

Están en `src/central/central/analytics/sql/` (SQL de Athena, motor Trino). Usan dos variables,
`${database}` y `${days}`, que llenan Terraform (consultas guardadas, 30 días) y la API (los días que
se pidan).

| Reporte | Pregunta que responde |
|---|---|
| `ventas_por_dia` | ¿Cuántas unidades y cuánto dinero vendió cada bodega cada día? |
| `top_productos` | ¿Cuáles son los 10 productos más vendidos de la empresa? |
| `stock_actual` | ¿Cuánto hay de cada producto en cada bodega y cuánto vale (última foto)? |
| `movimientos_por_tipo` | ¿Cuántas entradas, salidas, ajustes, reservas y transferencias hizo cada bodega? |
| `transferencias` | ¿Cuántas transferencias terminaron bien, cuántas se compensaron y cuánto tardan? |

Ejemplo (`ventas_por_dia.sql`):

```sql
WITH ventas AS (
    SELECT DISTINCT event_id, warehouse_id, quantity, unit_retail_price, occurred_at
    FROM "warehouse_platform_analytics"."movements"
    WHERE type = 'SHIPMENT'
      AND dt >= CAST(date_add('day', -30, current_date) AS varchar)   -- poda de particiones
)
SELECT date(from_iso8601_timestamp(occurred_at)) AS dia,
       warehouse_id AS bodega,
       sum(quantity) AS unidades,
       round(sum(quantity * CAST(coalesce(unit_retail_price, '0') AS decimal(14, 2))), 2) AS valor_ventas
FROM ventas
GROUP BY 1, 2
ORDER BY 1 DESC, 2
```

### Resultado de los reportes con los datos de los experimentos

Las consultas se ejecutaron sobre los archivos que generó el exportador durante las pruebas de las
Fases 6 y 7 (cargados en un motor SQL local que imita las funciones de Athena; Athena real solo
existe en AWS). Con **todos los lotes duplicados a propósito** los resultados fueron idénticos.

```text
== transferencias
resultado              transferencias  unidades  duracion_promedio_s  duracion_maxima_s  requieren_revision
RESERVATION_RELEASED   3               30        12.1                 22.397             0
COMPLETED              3               54        3.5                  5.738              0
FAILED                 1               1000      0.2                  0.155              0

== movimientos_por_tipo
bodega  tipo          movimientos  unidades
WH-001  RECEIPT       11           261
WH-001  RELEASE       3            30
WH-001  RESERVATION   6            84
WH-001  SHIPMENT      14           27
WH-001  TRANSFER_OUT  3            54
WH-002  RECEIPT       1            20
WH-002  TRANSFER_IN   3            54
```

(Se ve la Saga en los datos: 6 reservas, 3 liberadas por compensación y 3 despachadas —
`TRANSFER_OUT` en WH-001 = `TRANSFER_IN` en WH-002 = 54 unidades.)

---

## 4. Cómo usarlo

### En AWS (Learner Lab)

`terraform apply` (ver `terraform/README.md`) crea el bucket, la base y las tablas de Glue, el
workgroup de Athena y las 5 consultas guardadas, y le pasa sus nombres a la EC2 de la central. Unos
minutos después de que haya movimientos:

```powershell
terraform output analytics           # bucket, base, workgroup y enlace a la consola
python scripts/athena_report.py --list --central http://<ip-central>:8090
python scripts/athena_report.py ventas_por_dia --days 7 --central http://<ip-central>:8090
python scripts/athena_report.py top_productos --csv top.csv --central http://<ip-central>:8090
```

O desde el tablero (`http://<ip-central>:8090/dashboard` → *Reportes con Athena*), o en la consola de
Athena: elegir el workgroup `warehouse-platform-reportes` → *Saved queries*.

| Endpoint | Para qué |
|---|---|
| `GET /reports/athena` | Reportes disponibles y si Athena está configurado |
| `GET /reports/athena/{nombre}/sql?days=30` | Ver el SQL |
| `POST /reports/athena/{nombre}?days=30` | **202**: arranca la consulta (asíncrona, como los reportes de la Fase 3) |
| `GET /reports/athena/executions/{id}` | `QUEUED`/`RUNNING`/`SUCCEEDED`; al terminar, columnas, filas y bytes leídos |
| `GET /reports/athena/export-status` | Filas exportadas y pendientes de cada tabla |

### En tu PC (sin AWS)

El exportador escribe los mismos archivos en la carpeta `exports/` del repositorio (está en
`.gitignore`): pueden abrirlos para ver exactamente lo que leería Athena. `/reports/athena` responde
`configured: false` y los `POST` dan **503** con la explicación.

### Costos y límites (Learner Lab)

- Athena cobra por datos leídos (≈ USD 5 por TB): con estos volúmenes cada consulta lee kilobytes.
- El workgroup corta cualquier consulta que intente leer más de **1 GB** (`bytes_scanned_cutoff`).
- Los resultados de Athena se borran solos a los 7 días (regla de ciclo de vida del bucket).
- `terraform destroy` borra el bucket con sus archivos (`force_destroy`).
- La central usa las credenciales temporales de **LabRole** desde los contenedores (por eso las EC2
  tienen `http_put_response_hop_limit = 2`). No se crea ningún rol ni se guarda ninguna llave.

---

## 5. Pruebas

| Archivo | Qué comprueba |
|---|---|
| `tests/central/test_exporter_athena.py` (10) | Particiones por día; no reexporta; si S3 falla, el MISMO lote se sube después sin duplicar; solo transferencias terminadas; columnas = `tables.json`; `put_object` a S3 y el flujo completo de Athena (`start_query_execution` → `get_query_execution` → `get_query_results`) con **botocore Stubber**; 503 sin Athena; SQL de cada reporte; tablero |
| Terraform | `terraform validate` en CI. Las conexiones del plan (la central recibe bucket y workgroup; 3 tablas y 5 consultas guardadas; sin Athena con `enable_analytics = false`) se comprobaron con un plan simulado (proveedor de AWS falso) |

**Lo que NO se pudo probar desde el entorno de desarrollo:** el `terraform apply` real y Athena real
(no hay cuenta de AWS). Es lo primero que el equipo debe correr en su Learner Lab.

Ver `docs/adr/ADR-009-reportes-con-athena.md`.

---

## 6. Archivos de este commit (parte reportes)

| Archivo | Qué es |
|---|---|
| `src/central/central/analytics/tables.json` | **Nuevo.** Columnas de las tablas de Athena (fuente única) |
| `src/central/central/analytics/exporter.py`, `storage.py` | **Nuevos.** Exportación por lotes a S3 (o a `exports/`) |
| `src/central/central/analytics/athena.py`, `sql/*.sql` | **Nuevos.** Los 5 reportes y el cliente de Athena |
| `src/central/central/workers/exporter.py` | **Nuevo.** Contenedor `central_exporter` |
| `src/central/central/routers/athena.py` | **Nuevo.** `/reports/athena/*` |
| `terraform/modules/analytics/` | **Nuevo.** S3 + Glue + Athena + consultas guardadas |
| `terraform/main.tf`, `variables.tf`, `outputs.tf`, `modules/ec2_node/*` | Conexión con la central, `enable_analytics`, salto IMDS para contenedores |
| `scripts/athena_report.py` | **Nuevo.** Reporte desde la terminal |
| `tests/central/test_exporter_athena.py` | Pruebas |
