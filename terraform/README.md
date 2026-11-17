# Terraform — despliegue en AWS (Learner Lab)

Automatiza lo que en el laboratorio se hizo a mano: crear una EC2 por bodega (y, desde la Fase 7,
una para la plataforma central), abrir sus puertos y levantar los contenedores. Un `terraform apply`
crea todo; un `terraform destroy` lo borra.

## Qué crea

| Recurso | Para qué |
|---|---|
| `aws_security_group.warehouses` | Firewall de las bodegas: web y API abiertas a `allowed_cidr`; SSH y consola de RabbitMQ solo a `admin_cidr`; la API también acepta a la central |
| `aws_security_group.central` | Firewall de la central: API (8090) abierta a `allowed_cidr`; **AMQP (5680) solo desde las bodegas**; consola de RabbitMQ (15680) y SSH solo `admin_cidr` |
| `aws_instance` `CENTRAL` (Fase 7) | Plataforma central. Se crea **primero**: las bodegas necesitan su IP privada |
| `aws_instance` × 3 (`WH-001`, `WH-002`, `WH-003`) | Una máquina Ubuntu 24.04 por bodega. Al arrancar instala Docker, clona el repositorio y ejecuta `./manage_warehouse.sh start wh_00X` |
| `module.analytics` (si `enable_analytics = true`) | Reportes: bucket S3 (privado, cifrado), base y 3 tablas de Glue con *partition projection*, workgroup de Athena (máx. 1 GB por consulta) y las 5 consultas guardadas. Ver `docs/fases/REPORTES_ATHENA.md` |

Todas las EC2 usan el mismo módulo (`modules/ec2_node`) y la misma plantilla de arranque
(`templates/node_user_data.sh.tftpl`). Cada máquina recibe un `runtime.env` con sus valores propios:

| Máquina | `runtime.env` |
|---|---|
| Bodega | `CENTRAL_AMQP_URL=amqp://platform:<clave>@<IP privada de la central>:5680/%2F` y `ADVERTISED_API_URL=http://<su IP privada>:<puerto API>` (la averigua al arrancar con IMDSv2) |
| Central | Puertos y clave del RabbitMQ central (`central_rabbit_password`); bucket, base y workgroup de Athena (`EXPORT_S3_BUCKET`, `ATHENA_DATABASE`, `ATHENA_WORKGROUP`) |

Las EC2 tienen `http_put_response_hop_limit = 2` (IMDSv2): así los **contenedores** de la central
obtienen las credenciales temporales de LabRole para S3 y Athena, sin guardar llaves en ningún lado.

El tráfico bodega ↔ central va por la red **privada** de la VPC.

Usa la VPC por defecto, la llave `vockey` y el perfil `LabInstanceProfile` que el Learner Lab ya trae
(el Learner Lab **no permite crear roles IAM**).

## Pasos (Windows)

1. **Instalar Terraform:** `winget install Hashicorp.Terraform` (o descargarlo de terraform.io).
2. **Iniciar el Learner Lab** (botón *Start Lab*, esperar el círculo verde).
3. **Credenciales:** en *AWS Details* → *AWS CLI* → *Show*, copiar el bloque completo en
   `C:\Users\<tu-usuario>\.aws\credentials`. Caducan cada 4 horas: si Terraform dice
   `ExpiredToken`, vuelve a copiarlas.
4. **Llave SSH:** en *AWS Details* descargar `labsuser.pem` (para entrar a las máquinas).
5. **Variables:** copiar `terraform.tfvars.example` como `terraform.tfvars` y poner la URL del repositorio
   (debe ser **público**, porque cada EC2 lo clona).
6. **Crear todo:**

```powershell
cd terraform
terraform init      # descarga el proveedor de AWS
terraform plan      # muestra lo que va a crear, sin crear nada
terraform apply     # crea (escribir "yes")
terraform output    # URLs de cada bodega y de la central
```

7. **Esperar ~5 minutos:** cada máquina instala Docker y construye las imágenes. Para ver el avance:
   `ssh -i labsuser.pem ubuntu@<ip> "sudo tail -f /var/log/warehouse-bootstrap.log"` (termina con
   `WH-00X lista`; la central, `CENTRAL lista`). Luego: `terraform output central` → `api_docs`
   (Swagger de la central) y `http://<ip-central>:8090/warehouses` para ver las 3 bodegas `ONLINE`.
8. **Al terminar la práctica:** `terraform destroy` (el Learner Lab tiene presupuesto limitado).

## Cambiar algo

- **Otra bodega:** agregar `wh_004` al mapa `warehouses` en `terraform.tfvars` (y su carpeta con
  `python scripts/new_warehouse.py WH-004` en el repositorio).
- **Máquinas más grandes:** `instance_type = "t3.medium"` (bodegas) o `central_instance_type` (central).
- **Solo UNA bodega más grande** (escalamiento independiente, Fase 4): `instance_type` dentro de esa
  bodega en el mapa `warehouses`. Por defecto WH-002, la de alta demanda, usa `t3.medium`.
- **Clave del RabbitMQ central:** `central_rabbit_password = "..."` (no la dejen en `platform`).
- **Sin Athena:** `enable_analytics = false` (solo las EC2).
- **Reportes:** `terraform output analytics` y `python scripts/athena_report.py --list --central http://<ip-central>:8090`.
- **Nueva versión del código:** `terraform apply -replace='module.warehouse["wh_001"].aws_instance.this'`
  recrea esa máquina con lo último del repositorio (sus datos se pierden; es un entorno de pruebas).

## Validación

Este código (bodegas, central de la Fase 7 y módulo de Athena) se validó con `tofu validate` y
`tofu fmt -check` (OpenTofu 1.8, compatible con Terraform) contra el esquema real del proveedor
`hashicorp/aws` 5.75. Además se ejecutó un **plan simulado** (proveedor de AWS falso) que comprobó
las conexiones: cada bodega recibe la IP privada de la central, la central recibe el bucket y el
workgroup, se crean 3 tablas y 5 consultas guardadas, y con `enable_analytics = false` no se crea
Athena. No se pudo hacer `apply` real desde el entorno de desarrollo (no tiene cuenta de AWS): el
primer `terraform apply` lo hace el equipo en su Learner Lab.

Si el Learner Lab de su curso no permitiera Glue o Athena, usen `enable_analytics = false`: todo lo
demás funciona igual y los datos para Athena quedan en la carpeta `exports/` de la central.
