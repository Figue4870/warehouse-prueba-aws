# Fases 4 y 5 — Bodegas independientes, balanceo de carga y Terraform

**Commit 2 · Persona 2**
**Objetivo:**
- **Fase 4:** varias bodegas (WH-001, WH-002, WH-003), cada una con su propia API, PostgreSQL, Redis y RabbitMQ, de modo que la caída de una no afecte a las demás.
- **Fase 5:** varias copias de la API de una misma bodega detrás de un balanceador.
- **Terraform:** automatizar en AWS lo que en el laboratorio se hizo a mano.

---

## 1. Lo que había y el problema

Antes de este commit había **cuatro** archivos de Compose casi iguales (`wh_001`, `wh_002`, una plantilla y la del laboratorio). Cada cambio (por ejemplo, agregar RabbitMQ en la Fase 3) había que copiarlo en todos, y agregar una bodega significaba copiar y editar a mano un archivo de 200 líneas. Con 100 bodegas (la pregunta final de la guía) eso es inmanejable.

**Solución:** una sola plantilla, `docker/composes/compose-warehouse.yml` (la misma que usó el laboratorio en AWS), y un `.env` por bodega. Lo único que distingue a WH-002 de WH-001 es su `.env`:

```text
docker/components/
  wh_001/.env   WH_ID=WH-001  BE_PORT=8001  FE_PORT=8081 ...
  wh_002/.env   WH_ID=WH-002  BE_PORT=8002  FE_PORT=8082 ...
  wh_003/.env   WH_ID=WH-003  BE_PORT=8003  FE_PORT=8083 ...   <- NUEVA
```

```powershell
python scripts/new_warehouse.py WH-004    # crea wh_004/.env con los puertos siguientes
.\manage.ps1 start wh_004                 # y ya existe una cuarta bodega
```

**¿Y con 100 bodegas?** Una prueba (`tests/unit/test_new_warehouse.py`) crea WH-004 … WH-100 y encontró
un error real del primer diseño: si cada puerto solo suma 1 por bodega, la API de WH-081 caería en
8081, el puerto **web** de WH-001. Ahora el script revisa los puertos que ya usa cualquier componente
de `docker/components/` y, si el siguiente está ocupado, toma el próximo libre. Resultado: 100
bodegas × 6 puertos = 600 puertos, ninguno repetido. (En AWS no importa: cada bodega tiene su propia
máquina; importa cuando varias corren en el mismo PC.)

---

## 2. Arquitectura de UNA bodega (Fases 1 a 5)

```text
                       navegador (FE_PORT)          clientes / scripts (BE_PORT)
                              |                                |
                         frontend (Nginx) ---- /api ---->  lb (Nginx, balanceador)
                                                               |
                                         +---------------------+---------------------+
                                         |                     |                     |
                                       api_1                 api_2                 api_3
                                         +----------+----------+----------+----------+
                                                    |                     |
                                              PostgreSQL               Redis (UNO, compartido)
                                           (inventario + outbox)
                                                    |
                                               outbox_relay --> RabbitMQ --> cache / notification / report / audit workers
```

13 contenedores por bodega. Cada bodega es un **proyecto de Docker independiente** (`-p wh-001`), con su propia red y sus propios volúmenes: **no comparten nada**.

---

## 3. Fase 4 — Múltiples bodegas

### 3.1 Cómo levantarlas

```powershell
.\manage.ps1 start all        # Windows   (o: ./manage_warehouse.sh start all)
.\manage.ps1 status
.\manage.ps1 stop wh_002      # apaga SOLO WH-002 (sin borrar sus datos)
.\manage.ps1 start wh_002
.\manage.ps1 reset wh_003     # apaga WH-003 y BORRA sus datos
.\manage.ps1 logs wh_001 api_2
```

| Bodega | Web | API (balanceador) | PostgreSQL | Redis | RabbitMQ (consola) |
|---|---|---|---|---|---|
| WH-001 | :8081 | :8001 | :5433 | :6380 | :15673 |
| WH-002 | :8082 | :8002 | :5434 | :6381 | :15674 |
| WH-003 | :8083 | :8003 | :5435 | :6382 | :15675 |

`basic-docker-compose.yml` sigue existiendo para levantar solo WH-001 con un comando. No lo mezclen con los scripts para WH-001, porque usan los mismos nombres de contenedor.

> 💡 Con las tres bodegas encendidas son 39 contenedores (≈2 GB de RAM). Si el PC se queda corto, levanten solo las que necesiten.

### 3.2 ¿Por qué una base de datos por bodega? (preguntas de la guía)

| Pregunta | Respuesta |
|---|---|
| ¿Por qué cada bodega tiene su propia base? | **Autonomía:** cada bodega opera aunque las demás estén caídas. **Aislamiento:** un error, un bloqueo o una consulta pesada en WH-002 no frena a WH-001. **Escala independiente:** a cada base se le dan los recursos que su bodega necesita. |
| ¿Por qué no una única base central? | Sería un punto único de falla (si cae, caen todas las bodegas), un cuello de botella (2.000 req/s de WH-004 frenarían a las demás) y borraría los problemas distribuidos que el curso quiere estudiar. La visión global se construye en la Fase 7 con un modelo de lectura, sin compartir la base operacional. |
| ¿Por qué un Redis por bodega? | Porque cada bodega tiene su propia carga: WH-002 (800 req/s de lectura) puede necesitar más memoria de caché que WH-003 (20 req/s). Además, si el Redis de WH-002 se llena o cae, no afecta la caché de las otras. |
| ¿Qué pasa si una bodega deja de responder? | Las demás siguen normalmente (experimento 4.1). Lo que estaba en su outbox se enviará cuando vuelva (Fase 3), y en la Fase 7 la central mostrará su información marcada como desactualizada. |
| ¿Cómo escalaría una sola bodega? | Más instancias de la API detrás de su balanceador (Fase 5), una máquina más grande para esa bodega, o más workers de la cola que se atrase. Ninguno de esos cambios toca a las otras bodegas. |
| ¿Por qué no obligar a todas a escalar igual? | Porque se pagaría para WH-003 (20 req/s) la misma capacidad que necesita WH-002 (800 req/s). Escalar por bodega pone los recursos donde está la carga (experimento 4.2). |

### 3.3 Escalar SOLO la bodega que lo necesita

| Dónde | Cómo |
|---|---|
| Docker (un PC) | Más instancias de API detrás del balanceador de ESA bodega (Fase 5) |
| AWS (Terraform) | Cada bodega del mapa `warehouses` puede tener su propio `instance_type`. Por defecto **WH-002 usa `t3.medium`** (la de 800 req/s) y las demás `t3.small`. Cambiarlo es una línea de `terraform.tfvars` y solo se recrea esa máquina |

```hcl
warehouses = {
  wh_001 = { wh_id = "WH-001", api_port = 8001, web_port = 8081, rabbit_mgmt_port = 15673 }
  wh_002 = { wh_id = "WH-002", api_port = 8002, web_port = 8082, rabbit_mgmt_port = 15674, instance_type = "t3.large" }
  wh_003 = { wh_id = "WH-003", api_port = 8003, web_port = 8083, rabbit_mgmt_port = 15675 }
}
```

### 3.4 Experimentos de la Fase 4 (hechos con procesos reales)

**4.1 · WH-002 se cae y las demás siguen.** Se consultaron las tres bodegas 10 veces por segundo durante 20 s, y a los 6 s se apagó WH-002 por completo (API, workers y su base de datos):

```text
t=  5s  WH-001: ok=10 fallos=0  | WH-002: ok=9  fallos=1   | WH-003: ok=10 fallos=0
t=  6s  WH-001: ok=10 fallos=0  | WH-002: ok=0  fallos=10  | WH-003: ok=10 fallos=0
...
=== Totales ===
WH-001: 200/200 correctas (100.0 %)
WH-002:  59/200 correctas (29.5 %)     <- solo mientras estuvo encendida
WH-003: 200/200 correctas (100.0 %)
```

Repetirlo:

```powershell
python tests/load/availability_probe.py --target WH-001=http://localhost:8001 --target WH-002=http://localhost:8002 --target WH-003=http://localhost:8003 --seconds 40
.\manage.ps1 stop wh_002        # en otra terminal, mientras corre la sonda
```

**4.2 · Carga distinta por bodega (50 / 800 / 20 req/s del README).** Cada bodega recibe solo su carga:

| Bodega | Pedido (req/s) | Logrado (req/s) | P50 | P95 | Errores |
|---|---:|---:|---:|---:|---:|
| WH-001 | 50 | **50,1** | 6,1 ms | 24,0 ms | 0 |
| WH-002 | 800 | **596** | 54,2 ms | 78,1 ms | 0 |
| WH-003 | 20 | **20,1** | 6,4 ms | 21,6 ms | 0 |

WH-001 y WH-003 recibieron exactamente lo que pedían. **WH-002 no alcanzó las 800 req/s** (en esta prueba tenía una sola instancia): es la bodega que debe escalar, y hacerlo no exige tocar las otras dos. Ese es el argumento de la Fase 4.

```powershell
python tests/load/multi_warehouse_load.py --seconds 30 --target WH-001=http://localhost:8001@50 --target WH-002=http://localhost:8002@800 --target WH-003=http://localhost:8003@20
```

---

## 4. Fase 5 — Balanceo de carga y API stateless

### 4.1 El balanceador (`docker/nginx/lb.conf`)

- Reparte las peticiones **por turnos** (*round-robin*) entre `api_1`, `api_2` y `api_3`.
- **Si una instancia falla,** Nginx reintenta esa petición en otra (`proxy_next_upstream`) y deja de enviarle tráfico durante 10 s después de 2 fallos (`max_fails=2 fail_timeout=10s`).
- **Con `resolve` y el DNS interno de Docker,** si una instancia se reinicia con otra IP, el balanceador la vuelve a encontrar solo.
- **¿Puede un reintento duplicar un movimiento?** No. Nginx **nunca** reintenta un `POST` que ya llegó a una API; solo lo reintenta si la conexión ni siquiera se pudo abrir. En el experimento 5.3, 30 entradas con una instancia caída dieron exactamente 30 unidades.
- **Salud:** `GET /lb-health` (el balanceador) y `GET /health` (cada API; responde 503 si su PostgreSQL está caído). Docker revisa ambos con `healthcheck` y reinicia lo que falle (`restart: unless-stopped`).
- **Para ver quién respondió:** cada respuesta trae `X-Instance` (por ejemplo, `WH-001-api-2`) y `X-Upstream` (la IP interna). La página web muestra **"Atendió: WH-001-api-2"**; al recargar, va cambiando.

### 4.2 ¿Qué significa que la API sea stateless?

Que **ninguna copia guarda en su memoria algo que las otras necesiten**. Cualquier petición puede llegar a cualquier instancia y el resultado es el mismo. En este proyecto:

| Estado | Dónde vive | ¿Compartido por las 3 APIs? |
|---|---|---|
| Inventario, productos, historial, alertas, outbox, reportes | PostgreSQL | Sí |
| Caché de saldos y contadores de hits/misses | Redis | Sí |
| Mensajes pendientes | RabbitMQ | Sí |
| Contador de consultas SQL de `/metrics` | Memoria de cada instancia | No, y está bien: es una métrica **de esa instancia**, no un dato del negocio |

Por eso los reportes (Fase 3) guardan su resultado en la base de datos y no en un archivo del contenedor: el reporte que generó un worker lo puede servir cualquier API.

### 4.3 Preguntas de la guía de la Fase 5

**¿Qué ocurre si Redis es local de cada instancia?** Cada API tendría su propia copia del caché. Si `api_1` registra una salida y borra **su** clave, `api_2` y `api_3` seguirían mostrando el saldo viejo hasta que expire su TTL. El usuario vería números distintos según qué instancia le respondiera.

**¿Por qué es problemático?** Por tres razones: datos inconsistentes entre instancias, una tasa de aciertos menor (cada caché se llena por separado, así que hay 3 veces más MISS) y el triple de memoria. Por eso hay **un Redis por bodega, compartido** por sus tres APIs.

**¿Dónde debería vivir el estado compartido?** En servicios externos a la API, hechos para eso: PostgreSQL (la verdad), Redis (copias rápidas) y RabbitMQ (trabajo pendiente).

### 4.4 Experimentos de la Fase 5 (balanceador Nginx 1.27 real + 3 instancias)

**5.1 · Una instancia contra tres** (4.000 lecturas, 20 usuarios simultáneos, a través del balanceador; promedio de 2 rondas):

| Instancias | Peticiones/s | P50 | P95 | Reparto |
|---|---:|---:|---:|---|
| 1 | 628 | 30,6 ms | 45,6 ms | api-1: 4000 |
| 3 | **786** | **22,0 ms** | 51,5 ms | api-1: 1333 · api-2: 1333 · api-3: 1334 |

El reparto fue perfecto y el throughput subió un 25 %. ¿Por qué no se triplicó? Porque la prueba corrió en una máquina de **2 núcleos** compartidos entre el generador de carga, Nginx, las APIs, PostgreSQL y Redis. Escalar horizontalmente solo ayuda si hay CPU libre para las nuevas copias. En AWS, la forma de aprovecharlo es darle a la bodega más vCPU (`instance_type`) o repartir sus APIs en varias máquinas.

```powershell
python tests/load/benchmark_cache.py --url http://localhost:8001 --label 3-instancias
docker stop warehouse_api_2_WH-001 warehouse_api_3_WH-001
python tests/load/benchmark_cache.py --url http://localhost:8001 --label 1-instancia
docker start warehouse_api_2_WH-001 warehouse_api_3_WH-001
```

**5.2 · Apagar una instancia en pleno tráfico** (20 req/s durante 26 s; `api_2` apagada a los 8 s y encendida a los 18 s):

```text
t=  7s  WH-001: ok=20 fallos=0  [api-1:7, api-2:7, api-3:6]
t=  8s  WH-001: ok=20 fallos=0  [api-1:10, api-3:10]        <- api_2 apagada
...
t= 19s  WH-001: ok=20 fallos=0  [api-1:7, api-2:5, api-3:8] <- api_2 volvió sola
=== Totales ===
WH-001: 520/520 correctas (100.0 %)
```

**Ninguna petición falló.** Las otras dos instancias absorbieron el tráfico y `api_2` se reincorporó sola.

```powershell
python tests/load/availability_probe.py --target WH-001=http://localhost:8001 --seconds 40
docker stop warehouse_api_2_WH-001     # en otra terminal; luego docker start ...
```

**5.3 · Escrituras con una instancia caída.** Con `api_3` apagada se enviaron 30 entradas de 1 unidad: 30 respuestas `201` y saldo final exacto de **30**. Ningún movimiento se perdió ni se duplicó.

---

## 5. Repetir TODOS los experimentos con Docker (un solo comando)

Con las tres bodegas levantadas (`.\manage.ps1 start all`):

```powershell
python scripts/demo_fase4_5.py
```

Hace, con `docker stop/start` y verificando cada resultado, los 7 experimentos de esta fase: WH-002
caída sin afectar a las otras, datos separados por bodega, carga distinta por bodega, reparto del
balanceador, 1 vs 3 instancias, una instancia apagada con tráfico (0 errores) y escrituras con una
instancia caída (saldo exacto). Ejemplo de salida (procesos reales, 2 núcleos):

```text
3) FASE 4 — carga distinta por bodega (50 / 200 / 20 req/s, 8 s)
     bodega   pedida  lograda  p95 (ms)  errores
     WH-001      50     50.0       6.0        0
     WH-002     200    200.0       4.4        0
     WH-003      20     20.0       5.4        0
4) FASE 5 — el balanceador reparte las peticiones (cabecera X-Instance)
   ✔ 60 peticiones a WH-001: WH-001-api-1 = 20, WH-001-api-2 = 20, WH-001-api-3 = 20
5) FASE 5 — 1 instancia vs 3 instancias (16 clientes simultáneos, 8 s cada medición)
     instancias   req/s    p50 ms   p95 ms   errores
              1   599.2     26.2     37.8        0
              3   833.1     17.0     42.7        0
6) FASE 5 — se apaga una instancia mientras hay tráfico (40 req/s durante 15 s)
   ✔ 600 peticiones y 0 errores: el balanceador reintentó en otra instancia
7) FASE 5 — escrituras con una instancia caída (API stateless)
   ✔ 30 de 30 entradas aceptadas (201) y el saldo final es exactamente 30
TODO OK en 68 s.
```

`--alta 800` usa para WH-002 la carga exacta del enunciado (necesita un PC con varios núcleos).

---

## 6. Terraform: el laboratorio, automatizado

En el laboratorio se crearon a mano 3 EC2, 3 security groups y se instaló todo por SSH. Ahora:

```powershell
cd terraform
copy terraform.tfvars.example terraform.tfvars   # poner la URL del repositorio
terraform init
terraform apply
terraform output
```

| Archivo | Qué hace |
|---|---|
| `terraform/main.tf` | VPC por defecto, AMI Ubuntu 24.04, módulos |
| `terraform/modules/security/` | Security group: web y API abiertas; SSH y consola de RabbitMQ solo para `admin_cidr`. PostgreSQL, Redis y AMQP **no** se abren a Internet |
| `terraform/modules/warehouse_node/` | Una EC2 por bodega (`for_each` sobre el mapa `warehouses`), cada una con su propio tamaño si se indica (§3.3) |
| `terraform/templates/warehouse_user_data.sh.tftpl` | Script de arranque: instala Docker y Git, clona el repo, escribe `runtime.env` y ejecuta `manage_warehouse.sh start wh_00X` |
| `terraform/variables.tf` | Ajustado al **Learner Lab**: `us-east-1`, llave `vockey`, perfil `LabInstanceProfile` (no se pueden crear roles IAM), `t3.small` |

Los pasos detallados, con las credenciales del Learner Lab, están en `terraform/README.md`.

> ⚠️ El código pasó `tofu validate` (OpenTofu, compatible con Terraform) contra el esquema real del proveedor AWS 5.75, y el script de arranque se generó y se verificó como Bash válido. El primer `terraform apply` debe hacerse en su Learner Lab, porque desde el entorno de desarrollo no hay acceso a una cuenta de AWS.

`t3.small` en vez del `t3.micro` del laboratorio: una bodega ahora tiene 13 contenedores (3 APIs, RabbitMQ, 5 workers…) y 1 GB de RAM no alcanza.

---

## 7. Integración continua (GitHub Actions)

Con 4 personas subiendo cambios, hace falta saber de inmediato si alguien rompió algo.
`.github/workflows/ci.yml` corre en **cada push** en una máquina limpia de GitHub:

| Trabajo | Qué verifica |
|---|---|
| Pruebas unitarias | `pytest tests/unit` con Python 3.11 y las versiones exactas de `requirements.txt` |
| Configuración | Compose de cada bodega, `nginx -t` del balanceador, `terraform fmt` + `terraform validate` |
| Bodega real | Construye las imágenes, levanta WH-001 completa (`docker compose up --wait`) y corre `pytest tests/integration` contra ella |

El resultado se ve en la pestaña **Actions** del repositorio (✓ verde o ✗ roja en cada commit).

## 8. Pruebas

| Archivo | Qué comprueba |
|---|---|
| `tests/unit/test_instance.py` (2) | `X-Instance` y `instance` en `/health`; la API no guarda estado del negocio en memoria |
| `tests/unit/test_new_warehouse.py` (5) | Agregar bodegas: puertos movidos, errores claros, puertos ocupados se saltan, **100 bodegas sin ningún puerto repetido** |
| `scripts/demo_fase4_5.py` | Los 7 experimentos de las Fases 4 y 5 contra Docker, verificados |
| `tests/integration/test_load_balancer_integration.py` (2) | Con Docker: el balanceador reparte entre instancias y todas ven los mismos datos |
| `tests/load/availability_probe.py` | Sonda de disponibilidad segundo a segundo (experimentos 4.1 y 5.2) |
| `tests/load/multi_warehouse_load.py` | Carga distinta por bodega (experimento 4.2) |
| `tests/load/benchmark_cache.py` | Ahora también muestra el reparto por instancia (experimento 5.1) |

```powershell
pytest tests/unit                                        # 76 pruebas
pytest tests/integration                                 # contra WH-001 (http://localhost:8001)
$env:API_URL="http://localhost:8002"; pytest tests/integration   # contra WH-002
```

---

## 9. Archivos de este commit

| Archivo | Cambio |
|---|---|
| `docker/composes/compose-warehouse.yml` | **Plantilla única** de bodega: 3 APIs + balanceador + todo lo anterior |
| `docker/nginx/lb.conf` | **Nuevo.** Balanceador de carga |
| `docker/components/wh_003/.env` | **Nuevo.** Tercera bodega |
| `docker/components/wh_00X/compose-warehouse.yml`, `compose-base-warehouse.yml`, `docker-compose.yml` | **Eliminados** (reemplazados por la plantilla única) |
| `basic-docker-compose.yml` | Ahora incluye la plantilla única con el `.env` de WH-001 |
| `manage_warehouse.sh` | **Nuevo** (basado en el del laboratorio): `init`, `start`/`stop`/`restart`/`reset` (una o `all`), `status`, `logs`, `list` |
| `manage.ps1` | **Nuevo.** Los mismos comandos para Windows |
| `scripts/new_warehouse.py` | **Nuevo.** Agrega una bodega con sus puertos (sin chocar con puertos ya usados) |
| `scripts/demo_fase4_5.py` | **Nuevo.** Los experimentos de las Fases 4 y 5 con Docker |
| `terraform/` | **Nuevo.** Infraestructura como código para el Learner Lab |
| `src/backend/app/app/config.py`, `main.py`, `health_controller.py` | `INSTANCE_NAME`, cabecera `X-Instance`, `instance` en `/health` |
| `src/frontend/app/*` | Indicador "Atendió: WH-00X-api-N" |
| `tests/load/*`, `tests/unit/test_instance.py`, `tests/unit/test_new_warehouse.py`, `tests/integration/test_load_balancer_integration.py` | Experimentos y pruebas nuevas |
| `.github/workflows/ci.yml` | **Nuevo.** Integración continua |
| `.gitattributes` | **Nuevo.** Los `.sh`, `.tf` y configuraciones siempre con fin de línea LF (un `.sh` editado en Windows con CRLF falla en la EC2) |
| `docs/adr/ADR-005…`, `ADR-006…` | Decisiones de arquitectura |
