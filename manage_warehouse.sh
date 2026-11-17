#!/usr/bin/env bash
# =====================================================================
# Gestión de bodegas (Linux / Mac / EC2). Basado en el script del laboratorio de AWS.
# En Windows usa .\manage.ps1 (mismos comandos).
#
#   ./manage_warehouse.sh init                 -> instala Docker y Git (Ubuntu / EC2)
#   ./manage_warehouse.sh start central        -> levanta la plataforma central (Fase 7)
#   ./manage_warehouse.sh start wh_001         -> levanta una bodega ("all" = central + todas)
#   ./manage_warehouse.sh stop wh_002          -> la apaga SIN borrar datos (o "all")
#   ./manage_warehouse.sh restart wh_001
#   ./manage_warehouse.sh reset wh_001         -> la apaga y BORRA sus datos (volúmenes)
#   ./manage_warehouse.sh status               -> contenedores activos
#   ./manage_warehouse.sh logs wh_001 api_1    -> logs de un servicio
#   ./manage_warehouse.sh list                 -> bodegas configuradas
#
# Cada bodega es un proyecto de Docker independiente (-p wh-001): su propia red,
# base de datos, caché y broker. Por eso apagar una no afecta a las demás (Fase 4).
# =====================================================================
set -euo pipefail
cd "$(dirname "$0")"

ACTION=${1:-help}
TARGET=${2:-}
SERVICE=${3:-}
WAREHOUSE_COMPOSE="docker/composes/compose-warehouse.yml"
CENTRAL_COMPOSE="docker/composes/compose-central.yml"
PLATFORM_NETWORK="warehouse_platform"

# En EC2 el usuario puede no estar en el grupo docker todavía: usar sudo si hace falta
DOCKER="docker"
if ! docker info >/dev/null 2>&1; then DOCKER="sudo docker"; fi

show_help() { sed -n '3,18p' "$0" | sed 's/^# \{0,1\}//'; }

# Red compartida entre la central y las bodegas (Fase 7)
ensure_network() { $DOCKER network inspect "$PLATFORM_NETWORK" >/dev/null 2>&1 || $DOCKER network create "$PLATFORM_NETWORK" >/dev/null; }

warehouses() { for d in docker/components/wh_*/; do basename "$d"; done; }

compose_wh() {
    local wh=$1; shift
    if [ "$wh" = "central" ]; then compose_central "$@"; return; fi
    local env_file="docker/components/${wh}/.env"
    [ -f "$env_file" ] || { echo "Error: no existe $env_file (bodegas: $(warehouses | tr '\n' ' '))"; exit 1; }
    local args=(compose --env-file "$env_file")
    # runtime.env (opcional, NO va al repositorio): valores de la máquina donde corre,
    # por ejemplo los que escribe Terraform en EC2. Sobrescriben a los del .env.
    [ -f "docker/components/${wh}/runtime.env" ] && args+=(--env-file "docker/components/${wh}/runtime.env")
    $DOCKER "${args[@]}" -f "$WAREHOUSE_COMPOSE" -p "${wh//_/-}" "$@"
}

compose_central() {
    local args=(compose --env-file docker/components/central/.env)
    [ -f docker/components/central/runtime.env ] && args+=(--env-file docker/components/central/runtime.env)
    $DOCKER "${args[@]}" -f "$CENTRAL_COMPOSE" -p central "$@"
}

for_targets() {
    local fn=$1; shift
    [ -n "$TARGET" ] || { echo "Error: indica la bodega (ej: wh_001) o 'all'"; show_help; exit 1; }
    if [ "$TARGET" = "all" ]; then
        for wh in central $(warehouses); do "$fn" "$wh" "$@"; done
    else
        "$fn" "$TARGET" "$@"
    fi
}

start_one()   { echo "==> Iniciando $1";  ensure_network; compose_wh "$1" up -d --build; }
stop_one()    { echo "==> Apagando $1";   compose_wh "$1" stop; }
restart_one() { echo "==> Reiniciando $1"; compose_wh "$1" restart; }
reset_one()   { echo "==> Borrando $1 y sus datos"; compose_wh "$1" down -v; }

case "$ACTION" in
    init)
        echo "Instalando Docker, Docker Compose y Git..."
        sudo apt-get update -y
        sudo apt-get install -y docker.io docker-compose-v2 git
        sudo systemctl enable --now docker
        sudo usermod -aG docker "$USER" || true
        echo "Listo. (Cierra y abre la sesión para usar docker sin sudo.)"
        ;;
    start)   for_targets start_one ;;
    stop)    for_targets stop_one ;;
    restart) for_targets restart_one ;;
    reset)   for_targets reset_one ;;
    status)  $DOCKER ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" ;;
    logs)
        [ -n "$TARGET" ] || { echo "Uso: $0 logs wh_001 [servicio]"; exit 1; }
        compose_wh "$TARGET" logs --tail 100 -f ${SERVICE:+"$SERVICE"}
        ;;
    list) echo central; warehouses ;;
    *) show_help; exit 1 ;;
esac
