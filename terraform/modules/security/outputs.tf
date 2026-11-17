output "warehouse_sg_id" {
  value = aws_security_group.warehouses.id
}

output "central_sg_id" {
  value = aws_security_group.central.id
}
