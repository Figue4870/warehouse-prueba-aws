# =====================================================================
# REPORTES CON ATHENA
#
#   central_exporter --(JSON por líneas)--> S3: <bucket>/<tabla>/dt=YYYY-MM-DD/*.json
#                                                  |
#                     Glue Data Catalog (tablas) --+--> Athena (SQL) --> resultados en <bucket>/athena-results/
#
# Athena no necesita servidores ni cargar datos: lee los archivos de S3 cuando
# se consulta y cobra por lo leído. La partición "dt" con PARTITION PROJECTION
# hace que Athena calcule las carpetas de cada día sin tener que registrarlas
# (sin MSCK REPAIR TABLE ni crawlers).
# =====================================================================

locals {
  tables  = { for name, spec in jsondecode(file(var.tables_file)) : name => spec if !startswith(name, "_") }
  db_name = replace("${var.project_name}_analytics", "-", "_")
  reports = { for f in fileset(var.sql_dir, "*.sql") : trimsuffix(f, ".sql") => f }
}

# ---------------------------------------------------------------- S3
# El bucket NO lo crea Terraform: en el AWS Academy Learner Lab una política de
# la organización (SCP) prohíbe leer algunas configuraciones de S3
# (s3:GetBucketObjectLockConfiguration) y el recurso de bucket de Terraform
# siempre las lee después de crearlo ("AccessDenied ... explicit deny").
#
# Por eso el nombre se calcula aquí (único por cuenta) y lo crea el
# central_exporter al arrancar, con el rol LabRole de la EC2 (ver
# src/central/central/analytics/storage.py: S3Storage.ensure_bucket). S3 cifra
# y bloquea el acceso público por defecto en los buckets nuevos.
#
# OJO: terraform destroy NO borra este bucket. Al terminar la práctica:
# consola de AWS -> S3 -> el bucket -> Empty -> Delete.
data "aws_caller_identity" "current" {}

locals {
  bucket = "${var.project_name}-datos-${data.aws_caller_identity.current.account_id}"
}

# ---------------------------------------------------------------- Glue (catálogo de tablas)
resource "aws_glue_catalog_database" "this" {
  name        = local.db_name
  description = "Modelo de lectura de la plataforma central exportado a S3"
}

resource "aws_glue_catalog_table" "this" {
  for_each      = local.tables
  name          = each.key
  database_name = aws_glue_catalog_database.this.name
  description   = each.value.description
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    "classification"              = "json"
    "EXTERNAL"                    = "TRUE"
    "projection.enabled"          = "true"
    "projection.dt.type"          = "date"
    "projection.dt.format"        = "yyyy-MM-dd"
    "projection.dt.range"         = "2026-01-01,NOW"
    "projection.dt.interval"      = "1"
    "projection.dt.interval.unit" = "DAYS"
    "storage.location.template"   = "s3://${local.bucket}/${each.key}/dt=$${dt}/"
  }

  partition_keys {
    name = "dt"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${local.bucket}/${each.key}/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters = {
        "ignore.malformed.json" = "true"
      }
    }

    dynamic "columns" {
      for_each = each.value.columns
      content {
        name    = columns.value.name
        type    = columns.value.type
        comment = columns.value.comment
      }
    }
  }
}

# ---------------------------------------------------------------- Athena
resource "aws_athena_workgroup" "this" {
  name          = "${var.project_name}-reportes"
  force_destroy = true

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = false
    bytes_scanned_cutoff_per_query     = var.bytes_scanned_cutoff

    result_configuration {
      output_location = "s3://${local.bucket}/athena-results/"
      encryption_configuration {
        encryption_option = "SSE_S3"
      }
    }
  }
}

# Los mismos .sql que ejecuta la API central, guardados en la consola de Athena ("Saved queries")
resource "aws_athena_named_query" "report" {
  for_each    = local.reports
  name        = each.key
  description = "Reporte ${each.key} (últimos ${var.default_days} días)"
  workgroup   = aws_athena_workgroup.this.id
  database    = aws_glue_catalog_database.this.name
  query       = templatefile("${var.sql_dir}/${each.value}", { database = aws_glue_catalog_database.this.name, days = var.default_days })
}
