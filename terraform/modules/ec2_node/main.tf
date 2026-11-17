# Una EC2 = una bodega completa (o la plataforma central), como en el laboratorio.
resource "aws_instance" "this" {
  ami                    = var.ami_id
  instance_type          = var.instance_type
  subnet_id              = var.subnet_id
  vpc_security_group_ids = var.security_group_ids
  key_name               = var.key_name
  iam_instance_profile   = var.instance_profile_name

  # Lo que en el laboratorio se hizo a mano por SSH, aquí lo hace la máquina sola al arrancar
  user_data = templatefile("${path.module}/../../templates/node_user_data.sh.tftpl", {
    node_label     = var.node_label
    component      = var.component
    repo_url       = var.repo_url
    repo_branch    = var.repo_branch
    runtime_env    = var.runtime_env
    advertise_port = var.advertise_port
  })
  user_data_replace_on_change = true

  root_block_device {
    volume_size = var.root_volume_size_gb
    volume_type = "gp3"
  }

  metadata_options {
    http_tokens = "required" # IMDSv2 (más seguro)
    # 2 saltos: los CONTENEDORES también pueden pedir las credenciales temporales de LabRole
    # (las usa la central para S3 y Athena). Con 1, solo el sistema operativo podría.
    http_put_response_hop_limit = 2
  }

  tags = {
    Name = var.node_label
    Role = var.component
  }
}
