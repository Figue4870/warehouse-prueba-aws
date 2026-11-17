variable "project_name" { type = string }

variable "tables_file" {
  description = "tables.json: columnas de cada tabla (la MISMA fuente que usa el exportador de la central)"
  type        = string
}

variable "sql_dir" {
  description = "Carpeta con los reportes .sql (los mismos que ejecuta la API central)"
  type        = string
}

variable "default_days" {
  description = "Días que usan las consultas guardadas (named queries) de la consola de Athena"
  type        = number
  default     = 30
}

variable "bytes_scanned_cutoff" {
  description = "Máximo de bytes que puede leer una consulta (protege el presupuesto del Learner Lab)"
  type        = number
  default     = 1073741824 # 1 GB
}
