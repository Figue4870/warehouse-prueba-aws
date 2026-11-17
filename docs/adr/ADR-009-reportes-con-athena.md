# ADR-009 — Reportes analíticos con S3 + Glue + Athena

## Contexto

Se pide un reporte sencillo con Athena. Las consultas históricas de toda la empresa no deben cargar
las bases de las bodegas ni la base central (que atiende el día a día). Se usa el AWS Academy
Learner Lab (sin crear roles IAM, presupuesto limitado).

## Alternativas

1. Consultas pesadas directamente sobre la base central.
2. Un data warehouse (Redshift): potente, pero costoso y lento de crear en el Learner Lab.
3. **Exportar el modelo de lectura a S3 y consultarlo con Athena.**

## Decisión

Opción 3:

- `central_exporter` sube lotes JSON por líneas a `s3://<bucket>/<tabla>/dt=YYYY-MM-DD/<lote>.json`.
  Cada lote se aparta antes de subirlo y tiene nombre fijo: reintentar no duplica.
- Tablas de Glue con *partition projection* sobre `dt` (sin crawlers ni `MSCK REPAIR`).
- Las columnas viven en un solo archivo (`tables.json`) que leen Terraform y el exportador; los
  reportes son archivos `.sql` que usan Terraform (consultas guardadas) y la API.
- Las consultas cuentan cada `event_id` una sola vez (`SELECT DISTINCT`), por si un lote se exportó
  dos veces tras reconstruir la base central.
- Workgroup con límite de 1 GB leído por consulta; resultados que expiran a los 7 días.
- Credenciales: LabRole de la EC2 (IMDSv2 con 2 saltos para que los contenedores lleguen).

## Consecuencias

- Sin servidores nuevos; costo casi nulo con estos volúmenes.
- Los datos de Athena van hasta 1 minuto atrás de la central (intervalo de exportación) y el stock se
  toma en fotos cada hora: es para análisis, no para operar.
- JSON es fácil de leer para estudiar; con mucho volumen convendría Parquet y archivos más grandes.
- Athena solo existe en AWS: localmente los archivos quedan en `exports/` y los reportes responden 503.
