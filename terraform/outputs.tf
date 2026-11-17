output "warehouses" {
  description = "Direcciones de cada bodega"
  value = {
    for key, wh in var.warehouses : wh.wh_id => {
      public_ip      = module.warehouse[key].public_ip
      instance_type  = coalesce(wh.instance_type, var.instance_type)
      web            = "http://${module.warehouse[key].public_ip}:${wh.web_port}"
      api_docs       = "http://${module.warehouse[key].public_ip}:${wh.api_port}/docs"
      rabbitmq       = "http://${module.warehouse[key].public_ip}:${wh.rabbit_mgmt_port}"
      ssh            = "ssh -i labsuser.pem ubuntu@${module.warehouse[key].public_ip}"
      bootstrap_logs = "ssh -i labsuser.pem ubuntu@${module.warehouse[key].public_ip} 'tail -f /var/log/warehouse-bootstrap.log'"
    }
  }
}

output "central" {
  description = "Plataforma central (Fase 7)"
  value = {
    public_ip  = module.central.public_ip
    private_ip = module.central.private_ip
    api_docs   = "http://${module.central.public_ip}:${var.central_port}/docs"
    rabbitmq   = "http://${module.central.public_ip}:${var.central_rabbit_mgmt_port}"
    ssh        = "ssh -i labsuser.pem ubuntu@${module.central.public_ip}"
  }
}

output "analytics" {
  description = "Reportes con Athena"
  value = var.enable_analytics ? {
    bucket        = module.analytics[0].bucket
    database      = module.analytics[0].database
    workgroup     = module.analytics[0].workgroup
    tables        = module.analytics[0].tables
    named_queries = module.analytics[0].named_queries
    console       = "https://${var.aws_region}.console.aws.amazon.com/athena/home?region=${var.aws_region}#/query-editor"
  } : null
}
