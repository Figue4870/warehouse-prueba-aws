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
resource "aws_s3_bucket" "data" {
  bucket_prefix = "${var.project_name}-datos-" # AWS completa el nombre (único en el mundo)
  force_destroy = true                         # terraform destroy borra también los archivos (entorno de práctica)
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    id     = "borrar-resultados-viejos-de-athena"
    status = "Enabled"
    filter {
      prefix = "athena-results/"
    }
    expiration {
      days = 7
    }
  }
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
    "storage.location.template"   = "s3://${aws_s3_bucket.data.bucket}/${each.key}/dt=$${dt}/"
  }

  partition_keys {
    name = "dt"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.data.bucket}/${each.key}/"
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
      output_location = "s3://${aws_s3_bucket.data.bucket}/athena-results/"
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
