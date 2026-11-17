output "bucket" {
  value = local.bucket
}

output "database" {
  value = aws_glue_catalog_database.this.name
}

output "workgroup" {
  value = aws_athena_workgroup.this.name
}

output "tables" {
  value = keys(local.tables)
}

output "named_queries" {
  value = keys(local.reports)
}
