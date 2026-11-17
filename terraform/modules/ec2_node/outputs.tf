output "public_ip" {
  value = aws_instance.this.public_ip
}

output "private_ip" {
  value = aws_instance.this.private_ip
}

output "instance_id" {
  value = aws_instance.this.id
}

output "user_data" {
  description = "Script de arranque (lo revisa terraform test). Sensible: lleva la clave del RabbitMQ central"
  value       = aws_instance.this.user_data
  sensitive   = true
}
