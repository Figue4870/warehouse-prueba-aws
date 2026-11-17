# ---------------------------------------------------------------- generales
variable "aws_region" {
  description = "Región de AWS. El Learner Lab trabaja en us-east-1 (o us-west-2)."
  type        = string
  default     = "us-east-1"
}

variable "availability_zone" {
  description = "Zona donde se crean las máquinas (us-east-1e no tiene instancias t3)."
  type        = string
  default     = "us-east-1a"
}

variable "project_name" {
  description = "Prefijo de nombres y etiquetas."
  type        = string
  default     = "warehouse-platform"
}

# ---------------------------------------------------------------- código
variable "repo_url" {
  description = "URL HTTPS del repositorio que cada EC2 clona (debe ser público o incluir credenciales de solo lectura)."
  type        = string
}

variable "repo_branch" {
  description = "Rama a desplegar."
  type        = string
  default     = "main"
}

# ---------------------------------------------------------------- máquinas
variable "instance_type" {
  description = "Tipo de EC2 por bodega. t3.small (2 GB) alcanza para los 13 contenedores de una bodega; t3.micro (1 GB) se queda corto."
  type        = string
  default     = "t3.small"
}

variable "root_volume_size_gb" {
  description = "Disco de cada máquina (imágenes Docker + datos)."
  type        = number
  default     = 20
}

variable "key_name" {
  description = "Par de llaves para SSH. En el Learner Lab ya existe 'vockey' (labsuser.pem)."
  type        = string
  default     = "vockey"
}

variable "instance_profile_name" {
  description = "Perfil IAM de las EC2. El Learner Lab NO permite crear roles: se usa el que ya trae (LabInstanceProfile)."
  type        = string
  default     = "LabInstanceProfile"
}

# ---------------------------------------------------------------- red
variable "allowed_cidr" {
  description = "Quién puede abrir las páginas web y las APIs. 0.0.0.0/0 = todo Internet (como en el laboratorio); mejor tu IP/32."
  type        = string
  default     = "0.0.0.0/0"
}

variable "admin_cidr" {
  description = "Quién puede entrar por SSH y a las consolas de RabbitMQ. Recomendado: tu IP/32."
  type        = string
  default     = "0.0.0.0/0"
}

# ---------------------------------------------------------------- central (Fase 7)
variable "central_instance_type" {
  description = "Tipo de EC2 de la plataforma central."
  type        = string
  default     = "t3.small"
}

variable "central_port" {
  description = "Puerto de la API central (Swagger en /docs)."
  type        = number
  default     = 8090
}

variable "central_rabbit_port" {
  description = "Puerto AMQP del RabbitMQ central: solo lo pueden usar las bodegas (security group)."
  type        = number
  default     = 5680
}

variable "central_rabbit_mgmt_port" {
  description = "Consola web del RabbitMQ central (solo admin_cidr)."
  type        = number
  default     = 15680
}

variable "central_rabbit_password" {
  description = "Clave del usuario 'platform' del RabbitMQ central. Cámbiala en terraform.tfvars."
  type        = string
  default     = "platform"
  sensitive   = true

  validation {
    # Va dentro de una URL amqp://usuario:clave@host: caracteres como @ : / la romperían
    condition     = can(regex("^[A-Za-z0-9_-]{8,}$", var.central_rabbit_password)) || var.central_rabbit_password == "platform"
    error_message = "Usa al menos 8 letras, números, '_' o '-' (sin @ : / ni espacios)."
  }
}

# ---------------------------------------------------------------- reportes con Athena
variable "enable_analytics" {
  description = "Crear S3 + Glue + Athena para los reportes (false = solo EC2)."
  type        = bool
  default     = true
}

# ---------------------------------------------------------------- bodegas
variable "warehouses" {
  description = <<-EOT
    Bodegas a desplegar: una EC2 por bodega. La clave es la carpeta en docker/components/.
    instance_type (opcional) permite que CADA bodega tenga el tamaño que necesita (Fase 4:
    escalamiento independiente). Si no se pone, se usa var.instance_type.
  EOT
  type = map(object({
    wh_id            = string
    api_port         = number
    web_port         = number
    rabbit_mgmt_port = number
    instance_type    = optional(string)
  }))
  default = {
    wh_001 = { wh_id = "WH-001", api_port = 8001, web_port = 8081, rabbit_mgmt_port = 15673 }
    # WH-002 es la bodega de alta demanda del ejercicio (800 req/s): máquina más grande, solo ella
    wh_002 = { wh_id = "WH-002", api_port = 8002, web_port = 8082, rabbit_mgmt_port = 15674, instance_type = "t3.medium" }
    wh_003 = { wh_id = "WH-003", api_port = 8003, web_port = 8083, rabbit_mgmt_port = 15675 }
  }
}
