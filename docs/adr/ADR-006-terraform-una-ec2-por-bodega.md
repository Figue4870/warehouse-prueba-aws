# ADR-006 — Terraform con una EC2 por bodega (Learner Lab)

## Contexto

En el laboratorio se desplegaron 3 bodegas a mano (crear EC2, security groups, instalar Docker,
clonar y levantar). Hay que hacerlo reproducible y compatible con el AWS Academy Learner Lab, que
no permite crear roles IAM, da credenciales temporales (4 h) y tiene presupuesto limitado.

## Alternativas

1. Seguir a mano (no reproducible y propenso a errores).
2. ECS/EKS con imágenes en ECR: más cercano a producción, pero mucho más complejo y costoso para el lab.
3. **Terraform: una EC2 Ubuntu por bodega con `user_data` que clona el repo y ejecuta `manage_warehouse.sh`.**

## Decisión

Opción 3. Módulos `security` (security group mínimo) y `warehouse_node` (`for_each` sobre un mapa de
bodegas). Se usan la VPC por defecto, `LabInstanceProfile` y la llave `vockey`. Los valores propios de
cada máquina van en `runtime.env` (no en el repositorio).

## Consecuencias

- `terraform apply` / `destroy` crea y borra todo en minutos; agregar una bodega es una línea en el mapa.
- Cada máquina construye sus imágenes al arrancar (~5 min). Con ECR se construirían una sola vez.
- Los datos viven en el disco de cada EC2: recrear la máquina los borra (aceptable en un entorno de
  pruebas; en producción, RDS o volúmenes EBS separados).
- El repositorio debe ser público (o usar un token de solo lectura) para que las EC2 lo clonen.

## Actualización (Fase 7)

El módulo `warehouse_node` se generalizó a `ec2_node` para crear también la EC2 de la plataforma
central con el mismo `user_data` (ver ADR-007 y `terraform/README.md`).
