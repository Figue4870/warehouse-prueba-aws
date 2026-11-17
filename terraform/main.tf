# =====================================================================
# Infraestructura en AWS (Learner Lab)
#
#   VPC por defecto
#     |
#     +-- Security Group "warehouses"  (web/API públicos, SSH y consolas solo admin)
#     |
#     +-- Security Group "central"     (API pública; AMQP solo desde las bodegas)
#     |
#     +-- EC2 CENTRAL (Fase 7)  <---- eventos (AMQP, red privada) ----+
#     |                                                                |
#     +-- EC2 WH-001  --+                                              |
#     +-- EC2 WH-002    |-- cada una clona el repo y corre --------------+
#     +-- EC2 WH-003  --+   ./manage_warehouse.sh start wh_00X
# =====================================================================

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "availability-zone"
    values = [var.availability_zone]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

# Ubuntu 24.04 LTS oficial (Canonical)
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720108477"]

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

module "security" {
  source                   = "./modules/security"
  project_name             = var.project_name
  vpc_id                   = data.aws_vpc.default.id
  allowed_cidr             = var.allowed_cidr
  admin_cidr               = var.admin_cidr
  warehouses               = var.warehouses
  central_port             = var.central_port
  central_rabbit_port      = var.central_rabbit_port
  central_rabbit_mgmt_port = var.central_rabbit_mgmt_port
}

# Reportes: bucket S3 + catálogo Glue + workgroup de Athena (antes que la central: ella recibe sus nombres)
module "analytics" {
  source = "./modules/analytics"
  count  = var.enable_analytics ? 1 : 0

  project_name = var.project_name
  tables_file  = "${path.root}/../src/central/central/analytics/tables.json"
  sql_dir      = "${path.root}/../src/central/central/analytics/sql"
}

# Fase 7: la plataforma central se crea PRIMERO (las bodegas necesitan su IP privada)
module "central" {
  source = "./modules/ec2_node"

  name                  = "${var.project_name}-central"
  node_label            = "CENTRAL"
  component             = "central"
  ami_id                = data.aws_ami.ubuntu.id
  instance_type         = var.central_instance_type
  subnet_id             = data.aws_subnets.default.ids[0]
  security_group_ids    = [module.security.central_sg_id]
  key_name              = var.key_name
  instance_profile_name = var.instance_profile_name
  root_volume_size_gb   = var.root_volume_size_gb
  repo_url              = var.repo_url
  repo_branch           = var.repo_branch
  runtime_env = merge({
    CENTRAL_PORT             = var.central_port
    CENTRAL_RABBIT_PORT      = var.central_rabbit_port
    CENTRAL_RABBIT_MGMT_PORT = var.central_rabbit_mgmt_port
    CENTRAL_RABBIT_PASSWORD  = var.central_rabbit_password
    AWS_DEFAULT_REGION       = var.aws_region
    }, var.enable_analytics ? {
    # El exportador sube los datos a S3 y la API ejecuta los reportes en Athena (con LabRole, sin llaves)
    EXPORT_S3_BUCKET = module.analytics[0].bucket
    ATHENA_DATABASE  = module.analytics[0].database
    ATHENA_WORKGROUP = module.analytics[0].workgroup
  } : {})
}

module "warehouse" {
  source   = "./modules/ec2_node"
  for_each = var.warehouses

  name                  = "${var.project_name}-${each.value.wh_id}"
  node_label            = each.value.wh_id
  component             = each.key
  ami_id                = data.aws_ami.ubuntu.id
  instance_type         = coalesce(each.value.instance_type, var.instance_type)
  subnet_id             = data.aws_subnets.default.ids[0]
  security_group_ids    = [module.security.warehouse_sg_id]
  key_name              = var.key_name
  instance_profile_name = var.instance_profile_name
  root_volume_size_gb   = var.root_volume_size_gb
  repo_url              = var.repo_url
  repo_branch           = var.repo_branch
  advertise_port        = each.value.api_port
  runtime_env = {
    # Los eventos de la bodega viajan al RabbitMQ central por la red privada de la VPC
    CENTRAL_AMQP_URL = "amqp://platform:${var.central_rabbit_password}@${module.central.private_ip}:${var.central_rabbit_port}/%2F"
  }
}
