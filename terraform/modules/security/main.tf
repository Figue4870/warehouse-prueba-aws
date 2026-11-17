# Security Group de las bodegas: el "firewall" de cada EC2.
# Solo se abre lo necesario. PostgreSQL, Redis y el AMQP de RabbitMQ NO se abren
# a Internet: solo los usan los contenedores dentro de la misma máquina.
resource "aws_security_group" "warehouses" {
  name        = "${var.project_name}-warehouses"
  description = "Bodegas: web y API publicas; SSH y consolas solo administradores"
  vpc_id      = var.vpc_id

  tags = { Name = "${var.project_name}-warehouses" }
}

resource "aws_vpc_security_group_ingress_rule" "web" {
  for_each          = var.warehouses
  security_group_id = aws_security_group.warehouses.id
  description       = "Pagina web ${each.value.wh_id}"
  cidr_ipv4         = var.allowed_cidr
  ip_protocol       = "tcp"
  from_port         = each.value.web_port
  to_port           = each.value.web_port
}

resource "aws_vpc_security_group_ingress_rule" "api" {
  for_each          = var.warehouses
  security_group_id = aws_security_group.warehouses.id
  description       = "API (balanceador) ${each.value.wh_id}"
  cidr_ipv4         = var.allowed_cidr
  ip_protocol       = "tcp"
  from_port         = each.value.api_port
  to_port           = each.value.api_port
}

resource "aws_vpc_security_group_ingress_rule" "rabbit_console" {
  for_each          = var.warehouses
  security_group_id = aws_security_group.warehouses.id
  description       = "Consola RabbitMQ ${each.value.wh_id} (solo admin)"
  cidr_ipv4         = var.admin_cidr
  ip_protocol       = "tcp"
  from_port         = each.value.rabbit_mgmt_port
  to_port           = each.value.rabbit_mgmt_port
}

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  security_group_id = aws_security_group.warehouses.id
  description       = "SSH (solo admin)"
  cidr_ipv4         = var.admin_cidr
  ip_protocol       = "tcp"
  from_port         = 22
  to_port           = 22
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.warehouses.id
  description       = "Salida libre (apt, git clone, imagenes Docker)"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# ---------------------------------------------------------------- central (Fase 7)
resource "aws_security_group" "central" {
  name        = "${var.project_name}-central"
  description = "Central: API publica; AMQP solo desde las bodegas; SSH y consola solo admin"
  vpc_id      = var.vpc_id

  tags = { Name = "${var.project_name}-central" }
}

resource "aws_vpc_security_group_ingress_rule" "central_api" {
  security_group_id = aws_security_group.central.id
  description       = "API central"
  cidr_ipv4         = var.allowed_cidr
  ip_protocol       = "tcp"
  from_port         = var.central_port
  to_port           = var.central_port
}

# El RabbitMQ central SOLO acepta conexiones de las bodegas (sus forwarders), no de Internet
resource "aws_vpc_security_group_ingress_rule" "central_amqp_from_warehouses" {
  security_group_id            = aws_security_group.central.id
  description                  = "AMQP desde las bodegas"
  referenced_security_group_id = aws_security_group.warehouses.id
  ip_protocol                  = "tcp"
  from_port                    = var.central_rabbit_port
  to_port                      = var.central_rabbit_port
}

resource "aws_vpc_security_group_ingress_rule" "central_rabbit_console" {
  security_group_id = aws_security_group.central.id
  description       = "Consola RabbitMQ central (solo admin)"
  cidr_ipv4         = var.admin_cidr
  ip_protocol       = "tcp"
  from_port         = var.central_rabbit_mgmt_port
  to_port           = var.central_rabbit_mgmt_port
}

resource "aws_vpc_security_group_ingress_rule" "central_ssh" {
  security_group_id = aws_security_group.central.id
  description       = "SSH (solo admin)"
  cidr_ipv4         = var.admin_cidr
  ip_protocol       = "tcp"
  from_port         = 22
  to_port           = 22
}

resource "aws_vpc_security_group_egress_rule" "central_all" {
  security_group_id = aws_security_group.central.id
  description       = "Salida libre"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# La central llama a la API de cada bodega (/sync y consultas en vivo) por la red privada
resource "aws_vpc_security_group_ingress_rule" "api_from_central" {
  for_each                     = var.warehouses
  security_group_id            = aws_security_group.warehouses.id
  description                  = "API ${each.value.wh_id} desde la central"
  referenced_security_group_id = aws_security_group.central.id
  ip_protocol                  = "tcp"
  from_port                    = each.value.api_port
  to_port                      = each.value.api_port
}
